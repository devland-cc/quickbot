"""Web search for the Quickbot tool proxy.

Primary engine: Google, rendered by the `webkit-fetch` helper (a real WebKit
view — Google no longer serves results to plain HTTP clients). Fallback:
DuckDuckGo's static HTML endpoint. Both run from the user's machine; there is
no API key anywhere.
"""

import html
import json
import os
import re
import subprocess
import urllib.parse

import httpx

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
WEBKIT_FETCH = os.path.join(SERVER_DIR, "webkit-fetch")

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) "
              "Version/26.0 Safari/605.1.15")

MAX_RESULTS = 6
PAGE_FETCHES = 2          # top pages whose text is included
PAGE_EXTRACT_CHARS = 1800
TOTAL_CAP_CHARS = 7000    # keep prefill cheap on the local model

GOOGLE_EXTRACT_JS = """
(() => {
  const out = [];
  const seen = new Set();
  for (const h3 of document.querySelectorAll("a h3")) {
    const a = h3.closest("a");
    if (!a || !a.href || a.href.startsWith("https://www.google.")) continue;
    if (seen.has(a.href)) continue;
    seen.add(a.href);
    const block = h3.closest("[data-hveid]") || a.parentElement;
    let snippet = block ? block.innerText : "";
    snippet = snippet.replace(h3.innerText, "").replace(/\\s+/g, " ").trim().slice(0, 300);
    out.push({title: h3.innerText, url: a.href, snippet});
    if (out.length >= %d) break;
  }
  return JSON.stringify(out);
})()
""" % MAX_RESULTS


def google_search(query):
    """Google results via the WebKit helper. Raises on any failure."""
    url = "https://www.google.com/search?" + urllib.parse.urlencode({"q": query})
    proc = subprocess.run(
        [WEBKIT_FETCH, url, "--js", GOOGLE_EXTRACT_JS, "--timeout", "25"],
        capture_output=True, text=True, timeout=40,
    )
    if proc.returncode != 0:
        raise RuntimeError("webkit-fetch failed: " + proc.stderr.strip()[:200])
    results = json.loads(proc.stdout)
    if not results:
        raise RuntimeError("no results extracted (page layout changed?)")
    return results


def ddg_search(query):
    """DuckDuckGo's static HTML endpoint (no JS needed)."""
    resp = httpx.post(
        "https://html.duckduckgo.com/html/", data={"q": query},
        headers={"User-Agent": USER_AGENT}, timeout=15,
    )
    resp.raise_for_status()
    results = []
    for m in re.finditer(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'(?:class="result__snippet"[^>]*>(.*?)</a>)?',
        resp.text, re.S,
    ):
        href, title, snippet = m.groups()
        # DDG wraps targets in a redirect: /l/?uddg=<encoded-url>&...
        uddg = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query).get("uddg")
        target = uddg[0] if uddg else href
        results.append({
            "title": _strip_tags(title),
            "url": target,
            "snippet": _strip_tags(snippet or "")[:300],
        })
        if len(results) >= MAX_RESULTS:
            break
    if not results:
        raise RuntimeError("no results parsed from DuckDuckGo")
    return results


def _strip_tags(fragment):
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def fetch_page_text(url):
    """Readable-ish text of a page, truncated. Returns '' on any failure."""
    try:
        resp = httpx.get(
            url, headers={"User-Agent": USER_AGENT},
            follow_redirects=True, timeout=12,
        )
        if resp.status_code != 200 or "html" not in resp.headers.get("content-type", ""):
            return ""
        body = re.sub(r"<(script|style|noscript|svg)\b.*?</\1>", " ",
                      resp.text, flags=re.S | re.I)
        body = re.sub(r"<[^>]+>", " ", body)
        text = re.sub(r"\s+", " ", html.unescape(body)).strip()
        return text[:PAGE_EXTRACT_CHARS]
    except Exception:
        return ""


def web_search(query):
    """Text block handed to the model as the tool result."""
    engine = "google"
    try:
        results = google_search(query)
    except Exception:
        engine = "duckduckgo"
        try:
            results = ddg_search(query)
        except Exception as e:
            return "Web search failed ({}). Answer from your own knowledge and say the search was unavailable.".format(e)

    lines = ["Web search results for: {} (engine: {})".format(query, engine), ""]
    for i, r in enumerate(results, 1):
        lines.append("[{}] {}\n    {}".format(i, r["title"], r["url"]))
        if r["snippet"]:
            lines.append("    {}".format(r["snippet"]))
    fetched = 0
    for r in results:
        if fetched >= PAGE_FETCHES:
            break
        text = fetch_page_text(r["url"])
        if text:
            fetched += 1
            lines.append("")
            lines.append("Extract from {}:\n{}".format(r["url"], text))
    return "\n".join(lines)[:TOTAL_CAP_CHARS]


if __name__ == "__main__":
    import sys
    print(web_search(" ".join(sys.argv[1:]) or "current weather"))
