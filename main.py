import os
import re
from typing import List, Dict, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query, Response
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
RESOURCE_CACHE: Dict[str, Tuple[bytes, str]] = {}
SEARCH_INDEX: List[Dict[str, str]] = []

# --------------------
# Utilities
# --------------------
ABS_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
CSS_URL_RE = re.compile(r"url\(([^)]+)\)")


def is_absolute(url: str) -> bool:
    try:
        return bool(ABS_URL_RE.match(url))
    except Exception:
        return False


def resolve(base: str, link: Optional[str]) -> Optional[str]:
    if not link:
        return None
    link = link.strip()
    if not link:
        return None
    try:
        return urljoin(base, link)
    except Exception:
        return link


def resource_proxy_path(absolute_url: str) -> str:
    # Use relative path; frontend will prefix with backend base if needed
    return f"/resource?url={requests.utils.quote(absolute_url, safe='')}"


def sanitize_and_rewrite_html(html: str, base_url: str) -> str:
    """Remove dangerous elements but keep styles/images, rewriting resources through /resource.
    Also mark anchors with data-proxy-href for client-side navigation interception.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Remove entire elements that can execute code or embed third-party frames
    for tag in soup.find_all(["script", "iframe", "object", "embed"]):
        tag.decompose()

    # Remove inline event handlers
    for el in soup(True):
        attrs = dict(el.attrs)
        for k in list(attrs.keys()):
            if isinstance(k, str) and k.lower().startswith("on"):
                del el.attrs[k]

    # Neutralize meta refresh redirects
    for meta in soup.find_all("meta"):
        http_equiv = meta.get("http-equiv", "").lower()
        if http_equiv == "refresh":
            meta.decompose()

    # Rewrite link rel=stylesheet
    for link in soup.find_all("link"):
        rel = [r.lower() for r in link.get("rel", [])]
        if "stylesheet" in rel:
            href = link.get("href")
            abs_href = resolve(base_url, href)
            if abs_href:
                link["href"] = resource_proxy_path(abs_href)
            # Safety: prevent integrity/crossorigin leakage
            for attr in ["integrity", "crossorigin", "referrerpolicy"]:
                if attr in link.attrs:
                    del link.attrs[attr]
        else:
            # Remove preconnect/prefetch to avoid third-party requests
            as_attr = (link.get("as") or "").lower()
            if any(k in (link.get("rel") or []) for k in ["preconnect", "dns-prefetch", "prefetch", "preload"]):
                link.decompose()

    # Rewrite images and media to go through resource proxy
    for media_tag in soup.find_all(["img", "video", "audio", "source", "track"]):
        src = media_tag.get("src") or media_tag.get("data-src")
        abs_src = resolve(base_url, src)
        if abs_src:
            media_tag["src"] = resource_proxy_path(abs_src)
        for attr in ["loading", "decoding", "referrerpolicy", "integrity", "crossorigin", "srcset", "data-src"]:
            if attr in media_tag.attrs:
                del media_tag.attrs[attr]

    # Anchors: intercept via data-proxy-href; keep href as # to prevent default nav
    for a in soup.find_all("a"):
        href = a.get("href")
        abs_href = resolve(base_url, href)
        if abs_href:
            a["data-proxy-href"] = abs_href
            a["href"] = "#"
        a["target"] = "_self"
        a["rel"] = "nofollow noopener"

    # Forms: neutralize submission for now; client can enhance later
    for f in soup.find_all("form"):
        action = f.get("action")
        abs_action = resolve(base_url, action)
        if abs_action:
            f["data-proxy-action"] = abs_action
        f["action"] = "#"
        f["method"] = (f.get("method") or "get").lower()
        # Remove enctype that could cause uploads
        if "enctype" in f.attrs:
            del f.attrs["enctype"]

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
    return {"name": "Privacy Proxy & Search API", "endpoints": ["/search", "/proxy", "/resource", "/session/reset"]}


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
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }, allow_redirects=True)
        resp.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream fetch failed: {str(e)}")

    content_type = (resp.headers.get("Content-Type") or "").lower()
    if "text/html" not in content_type and not resp.text.startswith("<!DOCTYPE html"):
        # Non-HTML: show a placeholder to avoid binary leakage
        html = f"<html><body><h2>Non-HTML content</h2><p>Content-Type: {content_type}</p></body></html>"
    else:
        html = sanitize_and_rewrite_html(resp.text, resp.url)

    PROXY_CACHE[url] = html
    return {"url": resp.url, "html": html}


@app.get("/resource")
def resource(url: str = Query(..., description="Absolute URL of resource to fetch via proxy")):
    if not ABS_URL_RE.match(url):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")

    # Check optional whitelist
    whitelist = os.getenv("PROXY_WHITELIST", "").strip()
    if whitelist:
        allowed = [h.strip().lower() for h in whitelist.split(",") if h.strip()]
        if not any(url.lower().startswith(h if h.startswith("http") else f"https://{h}") for h in allowed):
            raise HTTPException(status_code=403, detail="URL not allowed by whitelist")

    if url in RESOURCE_CACHE:
        content, ctype = RESOURCE_CACHE[url]
        return Response(content, media_type=ctype)

    try:
        r = requests.get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": url,  # some CDNs require a referer
        }, allow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Resource fetch failed: {str(e)}")

    content_type = (r.headers.get("Content-Type") or "application/octet-stream").split(";")[0].strip().lower()

    content = r.content

    # If CSS, rewrite url(...) references to go through this proxy
    if "text/css" in content_type or urlparse(url).path.lower().endswith(".css"):
        try:
            text = r.text
            def replacer(match):
                raw = match.group(1).strip().strip('\"\'')
                if raw.lower().startswith("data:"):
                    return f"url({raw})"
                absu = resolve(url, raw)
                if not absu:
                    return f"url({raw})"
                return f"url({resource_proxy_path(absu)})"
            rewritten = CSS_URL_RE.sub(replacer, text)
            content = rewritten.encode("utf-8")
            content_type = "text/css"
        except Exception:
            # fallback to original
            pass

    # Cache small/medium resources to speed up
    if len(content) <= 5 * 1024 * 1024:  # 5 MB
        RESOURCE_CACHE[url] = (content, content_type)

    return Response(content, media_type=content_type)


@app.post("/session/reset")
def reset_session():
    PROXY_CACHE.clear()
    RESOURCE_CACHE.clear()
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
