"""Network tools: web search (DuckDuckGo HTML, no key needed), URL fetch, arXiv."""
from __future__ import annotations

import asyncio
import html
import io
import time
import re
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

UA = "Mozilla/5.0 (X11; Linux x86_64) roundtable-research/1.0"
_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=15.0), follow_redirects=True,
                                    headers={"User-Agent": UA})
    return _client


_arxiv_lock: asyncio.Lock | None = None
_arxiv_last = 0.0
ARXIV_INTERVAL = 3.1  # arXiv API terms: at most one request every 3 seconds


async def _get(url: str, *, arxiv: bool = False, **kw) -> httpx.Response:
    """GET with backoff on 429/503; arXiv requests are serialized and spaced across all agents."""
    global _arxiv_lock, _arxiv_last
    for attempt in range(4):
        if arxiv:
            if _arxiv_lock is None:
                _arxiv_lock = asyncio.Lock()
            async with _arxiv_lock:
                wait = _arxiv_last + ARXIV_INTERVAL - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    r = await _try_get(url, attempt, **kw)
                finally:
                    _arxiv_last = time.monotonic()
        else:
            r = await _try_get(url, attempt, **kw)
        if r is None:
            await asyncio.sleep(3.0 * (attempt + 1))
            continue
        if r.status_code not in (429, 503) or attempt == 3:
            r.raise_for_status()
            return r
        ra = r.headers.get("retry-after")
        await asyncio.sleep(float(ra) if ra and ra.isdigit() else 5.0 * (attempt + 1))
    raise RuntimeError("unreachable")


async def _try_get(url: str, attempt: int, **kw) -> httpx.Response | None:
    """None means a timeout that is worth retrying."""
    try:
        return await client().get(url, **kw)
    except httpx.TimeoutException:
        if attempt == 3:
            raise
        return None


def html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|section|article)>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()


async def web_search(query: str, max_results: int = 8) -> list[dict]:
    r = await client().post("https://html.duckduckgo.com/html/", data={"q": query})
    r.raise_for_status()
    results = []
    for m in re.finditer(r'(?s)<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=<a[^>]+class="result__a"|$)',
                         r.text):
        href, title, rest = m.groups()
        if "uddg=" in href:
            href = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0])
        if href.startswith("//"):
            href = "https:" + href
        sn = re.search(r'(?s)class="result__snippet"[^>]*>(.*?)</a>', rest)
        results.append({
            "title": html_to_text(title),
            "url": href,
            "snippet": html_to_text(sn.group(1)) if sn else "",
        })
        if len(results) >= max_results:
            break
    return results


async def fetch_url(url: str) -> tuple[str, str]:
    """Returns (content_type, text)."""
    if not re.match(r"^https?://", url):
        raise ValueError("only http(s) URLs are supported")
    r = await _get(url, arxiv="arxiv.org" in url)
    ctype = r.headers.get("content-type", "")
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        return "application/pdf", pdf_to_text(r.content)
    if "html" in ctype or r.text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        return ctype, html_to_text(r.text)
    return ctype, r.text


def pdf_to_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "[pypdf not installed: cannot extract PDF text]"
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for i, page in enumerate(reader.pages):
        try:
            pages.append(f"[page {i + 1}]\n" + (page.extract_text() or ""))
        except Exception as e:  # malformed pages should not kill the whole document
            pages.append(f"[page {i + 1}: extraction failed: {e}]")
    return "\n\n".join(pages)


ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


async def arxiv_search(query: str, max_results: int = 8, id_list: str | None = None) -> list[dict]:
    if id_list:
        params = {"id_list": id_list, "max_results": 1}
    else:
        params = {"search_query": query if ":" in query else f"all:{query}", "start": 0,
                  "max_results": max(1, min(max_results, 30)), "sortBy": "relevance"}
    r = await _get("https://export.arxiv.org/api/query", arxiv=True, params=params)
    root = ET.fromstring(r.text)
    out = []
    for e in root.findall("a:entry", ATOM):
        aid = (e.findtext("a:id", "", ATOM) or "").rsplit("/abs/", 1)[-1]
        out.append({
            "arxiv_id": aid,
            "title": " ".join((e.findtext("a:title", "", ATOM) or "").split()),
            "authors": [a.findtext("a:name", "", ATOM) for a in e.findall("a:author", ATOM)],
            "published": (e.findtext("a:published", "", ATOM) or "")[:10],
            "abstract": " ".join((e.findtext("a:summary", "", ATOM) or "").split()),
            "url": f"https://arxiv.org/abs/{aid}",
        })
    return out


async def arxiv_paper(arxiv_id: str, cache_dir: Path) -> dict:
    arxiv_id = arxiv_id.strip().removeprefix("arXiv:").removeprefix("https://arxiv.org/abs/")
    if not re.match(r"^[\w.\-/]+$", arxiv_id):
        raise ValueError("invalid arXiv id")
    safe = arxiv_id.replace("/", "_")
    txt_path = cache_dir / f"arxiv_{safe}.txt"
    try:
        found = await arxiv_search("", 1, id_list=arxiv_id)
    except (httpx.HTTPError, ET.ParseError):
        found = []
    meta = found[0] if found and found[0].get("title") else {"arxiv_id": arxiv_id, "url": f"https://arxiv.org/abs/{arxiv_id}"}
    if txt_path.exists():
        text = txt_path.read_text()
    else:
        r = await _get(f"https://arxiv.org/pdf/{arxiv_id}", arxiv=True)
        text = pdf_to_text(r.content)
        cache_dir.mkdir(parents=True, exist_ok=True)
        txt_path.write_text(text)
    return {"meta": meta, "text": text}
