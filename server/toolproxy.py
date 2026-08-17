"""Quickbot tool proxy.

OpenAI-compatible proxy in front of mlx_vlm.server that gives the model a
web_search tool: it forwards /v1 requests upstream, intercepts tool calls in
the response, runs the search locally and feeds the result back, looping
until the model produces a final answer. Clients keep speaking plain OpenAI
chat completions and receive a normal (streamed) answer.

Configuration via environment (set by serverctl):
  QUICKBOT_UPSTREAM     upstream base URL   (default http://127.0.0.1:8080)
  QUICKBOT_PROXY_PORT   listen port         (default 8081)
  QUICKBOT_WEB_SEARCH   "1"/"0" default for requests that don't send the
                        boolean `web_search` field (default "1")
"""

import asyncio
import json
import os
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
                "real-time access. Today is {date}.")

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


async def _run_tool_calls(tool_calls):
    """Execute tool calls and return the messages to append."""
    appended = [{"role": "assistant", "content": "", "tool_calls": tool_calls}]
    for call in tool_calls:
        function = call.get("function") or {}
        name = function.get("name")
        if name == "web_search":
            try:
                arguments = json.loads(function.get("arguments") or "{}")
                query = str(arguments.get("query", "")).strip()
            except (ValueError, TypeError):
                query = ""
            if query:
                print("toolproxy: web_search({!r})".format(query), flush=True)
                started = time.time()
                result = await asyncio.to_thread(websearch.web_search, query)
                print("toolproxy: web_search done in {:.1f}s, {} chars"
                      .format(time.time() - started, len(result)), flush=True)
            else:
                result = "Invalid web_search arguments: a 'query' string is required."
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
    for round_index in range(MAX_ROUNDS):
        if round_index == MAX_ROUNDS - 1:
            body.pop("tools", None)  # force a final answer
        resp = await client.post("/v1/chat/completions", json=body)
        if resp.status_code != 200:
            return Response(content=resp.content, status_code=resp.status_code,
                            media_type=resp.headers.get("content-type"))
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        tool_calls = (choice.get("message") or {}).get("tool_calls")
        if choice.get("finish_reason") != "tool_calls" or not tool_calls:
            return JSONResponse(data)
        body["messages"] = body["messages"] + await _run_tool_calls(tool_calls)
    return JSONResponse(data)


async def _stream_loop(body):
    """SSE generator: relays upstream deltas, silently resolving tool calls."""
    for round_index in range(MAX_ROUNDS):
        if round_index == MAX_ROUNDS - 1:
            body.pop("tools", None)
        tool_calls = None
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
                if choice.get("finish_reason") == "tool_calls" and delta.get("tool_calls"):
                    tool_calls = delta["tool_calls"]
                    continue  # swallow: the client never sees tool plumbing
                yield "data: {}\n\n".format(payload).encode()
        if not tool_calls:
            yield b"data: [DONE]\n\n"
            return
        body["messages"] = body["messages"] + await _run_tool_calls(tool_calls)
    yield b"data: [DONE]\n\n"


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
