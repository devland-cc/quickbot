"""Quickbot tool proxy.

OpenAI-compatible proxy in front of mlx_vlm.server that gives the model a
web_search tool: it forwards /v1 requests upstream, intercepts tool calls in
the response, runs the search locally and feeds the result back, looping
until the model produces a final answer. Clients keep speaking plain OpenAI
chat completions and receive a normal (streamed) answer.

Tool calls normally arrive structured (mlx_vlm.server parses the model's XML
into `tool_calls`), but the upstream parser is not fully reliable across
rounds — so the proxy also detects raw `<tool_call>` XML in the content
stream, withholds it from the client, and executes it itself.

Configuration via environment (set by serverctl):
  QUICKBOT_UPSTREAM     upstream base URL   (default http://127.0.0.1:8080)
  QUICKBOT_PROXY_PORT   listen port         (default 8081)
  QUICKBOT_WEB_SEARCH   "1"/"0" default for requests that don't send the
                        boolean `web_search` field (default "1")
"""

import asyncio
import json
import os
import re
import time

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

import websearch

UPSTREAM = os.environ.get("QUICKBOT_UPSTREAM", "http://127.0.0.1:8080")
PORT = int(os.environ.get("QUICKBOT_PROXY_PORT", "8081"))
WEB_SEARCH_DEFAULT = os.environ.get("QUICKBOT_WEB_SEARCH", "1") not in ("0", "false")

MAX_ROUNDS = 4  # upstream inference calls per request (search rounds + final)

WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": ("Search the web for current information. Returns titles, "
                        "URLs, snippets and text extracts of the top results."),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
            },
            "required": ["query"],
        },
    },
}

SYSTEM_NUDGE = ("You can search the web with the web_search function. Whenever the "
                "user asks about current events, live data, prices, weather, news, "
                "or anything after your knowledge cutoff, call web_search "
                "immediately — never ask for permission and never claim you lack "
                "real-time access. Once results arrive, answer from them instead "
                "of repeating similar searches. Today is {date}.")

FINAL_NUDGE = {"role": "user",
               "content": ("(No more searches are available. Answer my question "
                           "now using the search results above.)")}

# Raw tool-call XML as emitted by the Qwen3.8 chat template.
SENTINEL = "<tool_call>"
TOOL_CALL_RE = re.compile(r"<tool_call>\s*<function=([\w.-]+)>(.*?)</function>\s*</tool_call>",
                          re.S)
PARAM_RE = re.compile(r"<parameter=([\w.-]+)>\n?(.*?)\n?</parameter>", re.S)

app = FastAPI()
client = httpx.AsyncClient(base_url=UPSTREAM,
                           timeout=httpx.Timeout(None, connect=10))


# --- Passthrough endpoints ------------------------------------------------


@app.get("/health")
async def health():
    resp = await client.get("/health")
    return Response(content=resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type"))


@app.get("/v1/models")
async def models():
    resp = await client.get("/v1/models")
    return Response(content=resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type"))


# --- Chat completions with the tool loop ----------------------------------


def _augment(body):
    """Add the web_search tool and a usage nudge to the request."""
    tools = body.get("tools") or []
    if not any(t.get("function", {}).get("name") == "web_search" for t in tools):
        tools = tools + [WEB_SEARCH_TOOL]
    body["tools"] = tools

    nudge = SYSTEM_NUDGE.format(date=time.strftime("%Y-%m-%d"))
    messages = list(body.get("messages") or [])
    if messages and messages[0].get("role") == "system":
        first = dict(messages[0])
        if isinstance(first.get("content"), str):
            first["content"] = (first["content"] + "\n\n" + nudge).strip()
        elif isinstance(first.get("content"), list):
            first["content"] = first["content"] + [{"type": "text", "text": nudge}]
        else:
            first["content"] = nudge
        messages[0] = first
    else:
        messages.insert(0, {"role": "system", "content": nudge})
    body["messages"] = messages
    return body


def _final_round(body):
    """Strip tools and push the model to answer instead of searching again."""
    body.pop("tools", None)
    body["messages"] = body["messages"] + [FINAL_NUDGE]


def _extract_xml_tool_calls(text):
    """Parse raw <tool_call> XML the upstream parser failed to catch."""
    calls = []
    for index, match in enumerate(TOOL_CALL_RE.finditer(text)):
        name, inner = match.group(1), match.group(2)
        params = {p.group(1): p.group(2).strip() for p in PARAM_RE.finditer(inner)}
        calls.append({"id": "xmltool_{}".format(index), "type": "function",
                      "function": {"name": name,
                                   "arguments": json.dumps(params, ensure_ascii=False)}})
    return calls


def _safe_cut(buf):
    """(chars of buf safe to relay, whether a raw tool call has started).

    Withholds any suffix that could be the beginning of a `<tool_call>` tag
    so partial tags never reach the client mid-stream.
    """
    start = buf.find(SENTINEL)
    if start != -1:
        return start, True
    for k in range(min(len(buf), len(SENTINEL) - 1), 0, -1):
        if SENTINEL.startswith(buf[-k:]):
            return len(buf) - k, False
    return len(buf), False


def _queries(tool_calls):
    """The web_search query of each call ('' for non-search calls)."""
    queries = []
    for call in tool_calls:
        function = call.get("function") or {}
        query = ""
        if function.get("name") == "web_search":
            try:
                arguments = json.loads(function.get("arguments") or "{}")
                query = str(arguments.get("query", "")).strip()
            except (ValueError, TypeError):
                pass
        queries.append(query)
    return queries


async def _run_tool_calls(tool_calls, cache):
    """Execute tool calls and return the messages to append."""
    appended = [{"role": "assistant", "content": "", "tool_calls": tool_calls}]
    for call, query in zip(tool_calls, _queries(tool_calls)):
        name = (call.get("function") or {}).get("name")
        if name == "web_search":
            if not query:
                result = "Invalid web_search arguments: a 'query' string is required."
            elif query in cache:
                print("toolproxy: web_search({!r}) served from cache".format(query),
                      flush=True)
                result = cache[query]
            else:
                print("toolproxy: web_search({!r})".format(query), flush=True)
                started = time.time()
                result = await asyncio.to_thread(websearch.web_search, query)
                print("toolproxy: web_search done in {:.1f}s, {} chars"
                      .format(time.time() - started, len(result)), flush=True)
                cache[query] = result
        else:
            result = "Unknown tool: {}".format(name)
        appended.append({"role": "tool",
                         "tool_call_id": call.get("id"),
                         "content": result})
    return appended


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    enabled = body.pop("web_search", None)
    if enabled is None:
        enabled = WEB_SEARCH_DEFAULT

    if not enabled:
        return await _forward(body)

    body = _augment(body)
    if body.get("stream"):
        return StreamingResponse(_stream_loop(body),
                                 media_type="text/event-stream")
    return await _json_loop(body)


async def _forward(body):
    if body.get("stream"):
        async def relay():
            async with client.stream("POST", "/v1/chat/completions",
                                     json=body) as resp:
                async for chunk in resp.aiter_bytes():
                    yield chunk
        return StreamingResponse(relay(), media_type="text/event-stream")
    resp = await client.post("/v1/chat/completions", json=body)
    return Response(content=resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type"))


async def _json_loop(body):
    cache = {}
    for round_index in range(MAX_ROUNDS):
        final = round_index == MAX_ROUNDS - 1
        if final:
            _final_round(body)
        resp = await client.post("/v1/chat/completions", json=body)
        if resp.status_code != 200:
            return Response(content=resp.content, status_code=resp.status_code,
                            media_type=resp.headers.get("content-type"))
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        tool_calls = message.get("tool_calls")
        if choice.get("finish_reason") != "tool_calls" or not tool_calls:
            tool_calls = None
            content = message.get("content")
            if isinstance(content, str) and SENTINEL in content:
                if not final:
                    tool_calls = _extract_xml_tool_calls(content) or None
                if tool_calls is None:
                    # Out of rounds (or malformed): hide the tool plumbing.
                    message["content"] = content.split(SENTINEL)[0].rstrip()
                    choice["message"] = message
                    return JSONResponse(data)
            else:
                return JSONResponse(data)
        if final:
            return JSONResponse(data)
        body["messages"] = body["messages"] + await _run_tool_calls(tool_calls, cache)
    return JSONResponse(data)


def _chunk(delta, finish_reason=None, skeleton=None):
    """Serialize one SSE chunk in OpenAI chat.completion.chunk shape."""
    chunk = dict(skeleton) if skeleton else {"object": "chat.completion.chunk"}
    chunk["choices"] = [{"index": 0, "delta": delta,
                         "finish_reason": finish_reason}]
    return "data: {}\n\n".format(json.dumps(chunk, ensure_ascii=False)).encode()


async def _stream_loop(body):
    """SSE generator: relays upstream deltas, silently resolving tool calls.

    Tool calls are caught both structured (finish_reason == "tool_calls") and
    as raw <tool_call> XML in the content stream; while a search runs the
    client sees a short status line instead of a mute spinner.
    """
    cache = {}
    relayed_any = False
    last_was_status = False
    for round_index in range(MAX_ROUNDS):
        final = round_index == MAX_ROUNDS - 1
        if final:
            _final_round(body)
        tool_calls = None
        buf = ""          # content accumulated this round
        sent = 0          # chars of buf already relayed
        capturing = False  # inside raw tool-call XML: withhold from client
        async with client.stream("POST", "/v1/chat/completions",
                                 json=body) as resp:
            if resp.status_code != 200:
                detail = (await resp.aread()).decode("utf-8", "replace")[:500]
                payload = {"error": {"message": "upstream error: " + detail}}
                yield "data: {}\n\n".format(json.dumps(payload)).encode()
                yield b"data: [DONE]\n\n"
                return
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                finish = choice.get("finish_reason")
                if finish == "tool_calls" and delta.get("tool_calls"):
                    tool_calls = delta["tool_calls"]
                    continue  # swallow: the client never sees tool plumbing
                piece = ""
                content = delta.get("content")
                if content:
                    buf += content
                    if not capturing:
                        cut, found = _safe_cut(buf)
                        capturing = capturing or found
                        if cut > sent:
                            piece = buf[sent:cut]
                            sent = cut
                out_delta = {k: v for k, v in delta.items() if k != "content"}
                if piece:
                    out_delta["content"] = piece
                out_finish = None if capturing else finish
                if out_delta or out_finish:
                    if piece:
                        relayed_any = True
                        last_was_status = False
                    skeleton = {k: v for k, v in chunk.items() if k != "choices"}
                    yield _chunk(out_delta, out_finish, skeleton)
        if tool_calls is None and capturing:
            tool_calls = _extract_xml_tool_calls(buf) or None
        if tool_calls is None:
            if sent < len(buf) and not capturing:
                # Flush a withheld tail that never became a tool-call tag.
                yield _chunk({"content": buf[sent:]})
            yield b"data: [DONE]\n\n"
            return
        if final:
            break  # out of rounds: drop the unusable tool call
        for query in filter(None, _queries(tool_calls)):
            status = "🔎 *Searching the web for “{}”*\n\n".format(query)
            if relayed_any and not last_was_status:
                status = "\n\n" + status
            relayed_any = True
            last_was_status = True
            yield _chunk({"content": status})
        body["messages"] = body["messages"] + await _run_tool_calls(tool_calls, cache)
    yield b"data: [DONE]\n\n"


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
