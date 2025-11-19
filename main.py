import os
import re
from typing import List, Dict, Optional

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Privacy Proxy & Search API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------
# In-memory state
# --------------------
PROXY_CACHE: Dict[str, str] = {}
SEARCH_INDEX: List[Dict[str, str]] = []

# --------------------
# Utilities
# --------------------
ABS_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def sanitize_html(html: str) -> str:
    """Remove potentially dangerous/trackable elements and external resources.
    - Strip <script>, <iframe>, <object>
    - Strip inline event handlers (on*)
    - Remove external resources (img, video, audio, link rel=stylesheet) to avoid third-party requests
    - Keep text and basic structure
    """
    soup = BeautifulSoup(html, "html.parser")

    # Remove entire elements
    for tag in soup.find_all(["script", "iframe", "object"]):
        tag.decompose()

    # Remove inline event handlers
    for el in soup(True):
        attrs = dict(el.attrs)
        for k in list(attrs.keys()):
            if isinstance(k, str) and k.lower().startswith("on"):
                del el.attrs[k]

    # Remove external stylesheets and media to prevent third-party requests
    for link in soup.find_all("link"):
        rel = "".join(link.get("rel", [])).lower()
        href = link.get("href", "")
        if "stylesheet" in rel and ABS_URL_RE.match(href):
            link.decompose()

    for img in soup.find_all(["img", "video", "audio", "source"]):
        src = img.get("src") or img.get("data-src")
        if src and ABS_URL_RE.match(src):
            # Remove external media entirely
            img.decompose()
        else:
            # Remove lazy-loading attributes that might trigger network
            for attr in ["loading", "decoding", "referrerpolicy", "integrity", "crossorigin"]:
                if attr in img.attrs:
                    del img.attrs[attr]

    # Neutralize meta refresh redirects
    for meta in soup.find_all("meta"):
        http_equiv = meta.get("http-equiv", "").lower()
        if http_equiv == "refresh":
            meta.decompose()

    # Keep anchor tags but ensure target is neutral
    for a in soup.find_all("a"):
        a["target"] = "_self"

    return str(soup)


class SearchResult(BaseModel):
    title: str
    snippet: str
    url: str


def build_static_index() -> List[Dict[str, str]]:
    """Return a small static search index. No external network calls."""
    return [
        {
            "title": "Example Domain",
            "snippet": "This domain is for use in illustrative examples in documents.",
            "url": "https://example.org/",
        },
        {
            "title": "FastAPI – High performance, easy to learn, fast to code, ready for production",
            "snippet": "FastAPI framework, high performance, easy to learn, fast to code, ready for production.",
            "url": "https://fastapi.tiangolo.com/",
        },
        {
            "title": "MDN Web Docs",
            "snippet": "Resources for developers, by developers. Documenting web technologies, including CSS, HTML, and JavaScript.",
            "url": "https://developer.mozilla.org/en-US/",
        },
        {
            "title": "Python.org",
            "snippet": "The official home of the Python Programming Language.",
            "url": "https://www.python.org/",
        },
        {
            "title": "Wikipedia",
            "snippet": "The Free Encyclopedia.",
            "url": "https://www.wikipedia.org/",
        },
    ]


def search_index(query: str, limit: int = 10) -> List[SearchResult]:
    q = query.strip().lower()
    results: List[SearchResult] = []
    for item in SEARCH_INDEX:
        title = item.get("title", "")
        snippet = item.get("snippet", "")
        hay = (title + " " + snippet).lower()
        score = 0
        if q in title.lower():
            score += 2
        if q in snippet.lower():
            score += 1
        if score > 0 or q in hay:
            results.append((score, SearchResult(**item)))
    # Sort by score desc then title
    results.sort(key=lambda x: (-x[0], x[1].title))
    return [r[1] for r in results][:limit]


@app.on_event("startup")
def on_startup():
    global SEARCH_INDEX
    # Build static index only (no external network calls)
    SEARCH_INDEX = build_static_index()


@app.get("/")
def root():
    return {"name": "Privacy Proxy & Search API", "endpoints": ["/search", "/proxy", "/session/reset"]}


@app.get("/search", response_model=List[SearchResult])
def search(q: str = Query("", description="Search query"), limit: int = 10):
    if not q.strip():
        return []
    return search_index(q, limit=limit)


@app.get("/proxy")
def proxy(url: str = Query(..., description="Absolute http(s) URL to fetch and sanitize")):
    if not ABS_URL_RE.match(url):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")

    # Check optional whitelist
    whitelist = os.getenv("PROXY_WHITELIST", "").strip()
    if whitelist:
        allowed = [h.strip().lower() for h in whitelist.split(",") if h.strip()]
        if not any(url.lower().startswith(h if h.startswith("http") else f"https://{h}") for h in allowed):
            raise HTTPException(status_code=403, detail="URL not allowed by whitelist")

    if url in PROXY_CACHE:
        return {"url": url, "html": PROXY_CACHE[url]}

    try:
        resp = requests.get(url, timeout=10, headers={
            "User-Agent": "PrivacyProxy/1.0 (+research)"
        })
        resp.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream fetch failed: {str(e)}")

    content_type = resp.headers.get("Content-Type", "")
    if "text/html" not in content_type:
        # Non-HTML: show a placeholder to avoid binary leakage
        html = f"<html><body><h2>Non-HTML content</h2><p>Content-Type: {content_type}</p></body></html>"
    else:
        html = sanitize_html(resp.text)

    PROXY_CACHE[url] = html
    return {"url": url, "html": html}


@app.post("/session/reset")
def reset_session():
    PROXY_CACHE.clear()
    return {"ok": True, "message": "Session cleared"}


# Existing test endpoint (kept for health checks)
@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Used",
    }
    return response


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
