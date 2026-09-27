"""Web tools: search (Tavily, Brave, SerpAPI, or Google Custom Search) and page
fetching (no key needed).

Fetching arbitrary URLs from the server is an SSRF risk, so `fetch_page`:
* allows only http/https on the standard ports,
* resolves the host and refuses any non-public address (loopback, private,
  link-local, reserved, multicast; IPv4-mapped IPv6 included),
* follows redirects manually and re-validates every hop,
* caps response size and time, and only reads text content types.

Residual risk: a DNS answer can change between validation and connection
(rebinding). Run the API with egress restricted to the public internet when
that matters.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from html.parser import HTMLParser
from typing import Any

import httpx
from pydantic import BaseModel, Field

from app.config import get_settings
from app.core.errors import InvalidInputError, RateLimitError, ToolError, TransientError
from app.mcp.schemas import ToolContext

ALLOWED_PORTS = {80, 443}
MAX_REDIRECTS = 3
TEXT_TYPES = ("text/html", "text/plain", "application/xhtml+xml", "application/json", "text/markdown")
URL_RE = re.compile(r"https?://[^\s<>\"'`)\]]+", re.IGNORECASE)
USER_AGENT = "agent-orchestrator/1.0 (+research agent)"
# Tests inject an httpx.MockTransport here.
TRANSPORT: httpx.BaseTransport | None = None


class FetchPageInput(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    max_chars: int = Field(default=20_000, ge=500, le=100_000)


class WebSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    max_results: int = Field(default=5, ge=1, le=10)


def search_available() -> str:
    settings = get_settings()
    if not settings.web_search_api_key:
        return "not_configured"
    if settings.web_search_provider == "google":
        return "available" if settings.google_search_engine_id else "not_configured"
    return "available" if settings.web_search_provider in {"tavily", "brave", "serpapi"} else "not_configured"


def fetch_available() -> str:
    return "available" if get_settings().web_fetch_enabled else "not_configured"


def extract_urls(text: str, *, exclude: re.Pattern[str] | None = None, limit: int = 3) -> list[str]:
    urls = []
    for match in URL_RE.findall(text):
        url = match.rstrip(".,;:!?")
        if exclude is not None and exclude.search(url):
            continue
        if url not in urls:
            urls.append(url)
    return urls[:limit]


# ---------------------------------------------------------------- SSRF guard


def _public_address(host: str, port: int) -> None:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise InvalidInputError(f"Host could not be resolved: {host}") from None
    if not infos:
        raise InvalidInputError(f"Host could not be resolved: {host}")
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if not address.is_global or address.is_multicast:
            raise PermissionError(f"Fetching non-public addresses is not allowed ({host}).")


def validate_public_url(url: str) -> httpx.URL:
    try:
        parsed = httpx.URL(url)
    except Exception:
        raise InvalidInputError("Invalid URL.") from None
    if parsed.scheme not in {"http", "https"}:
        raise InvalidInputError("Only http and https URLs can be fetched.")
    if not parsed.host:
        raise InvalidInputError("URL has no host.")
    if parsed.userinfo:
        raise InvalidInputError("URLs with credentials are not allowed.")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise PermissionError(f"Port {port} is not allowed.")
    _public_address(parsed.host, port)
    return parsed


# ------------------------------------------------------------- HTML to text


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "title":
            self._in_title = True
        elif tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(parser.parts))
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return " ".join(parser.title.split()), text


# ------------------------------------------------------------------ handlers


def fetch_page(arguments: FetchPageInput, context: ToolContext) -> dict[str, Any]:
    settings = get_settings()
    if not settings.web_fetch_enabled:
        raise ToolError("Web page fetching is disabled (WEB_FETCH_ENABLED=false).", retryable=False)
    url = validate_public_url(arguments.url)
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1"}
    with httpx.Client(timeout=settings.web_timeout_seconds, follow_redirects=False, headers=headers, transport=TRANSPORT) as client:
        for _ in range(MAX_REDIRECTS + 1):
            try:
                with client.stream("GET", url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise ToolError("Redirect without a location.", retryable=False)
                        url = validate_public_url(str(url.join(location)))
                        continue
                    if response.status_code == 429:
                        raise RateLimitError("The site rate-limited the request.")
                    if response.status_code >= 500:
                        raise TransientError(f"The site returned HTTP {response.status_code}.")
                    if response.status_code >= 400:
                        raise InvalidInputError(f"The page returned HTTP {response.status_code}.")
                    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    if content_type and not content_type.startswith(TEXT_TYPES):
                        raise InvalidInputError(f"Unsupported content type: {content_type}.")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) >= settings.web_fetch_max_bytes:
                            break
                    encoding = response.encoding or "utf-8"
            except httpx.TimeoutException:
                raise TransientError("The page did not respond in time.") from None
            except httpx.TransportError:
                raise TransientError("The page could not be reached.") from None
            raw = bytes(body[: settings.web_fetch_max_bytes]).decode(encoding, errors="replace")
            if content_type in {"text/html", "application/xhtml+xml"} or raw.lstrip()[:1] == "<":
                title, text = html_to_text(raw)
            else:
                title, text = "", raw
            return {
                "url": str(url),
                "title": title[:300],
                "content": text[: arguments.max_chars],
                "truncated": len(text) > arguments.max_chars,
            }
    raise ToolError("Too many redirects.", retryable=False)


def web_search(arguments: WebSearchInput, context: ToolContext) -> list[dict[str, Any]]:
    settings = get_settings()
    if search_available() != "available":
        raise ToolError("Web search is not configured; check the provider API key and Google engine ID.", retryable=False)
    key = settings.web_search_api_key or ""
    try:
        with httpx.Client(timeout=settings.web_timeout_seconds, headers={"User-Agent": USER_AGENT}, transport=TRANSPORT) as client:
            if settings.web_search_provider == "google":
                response = client.get(
                    "https://www.googleapis.com/customsearch/v1",
                    params={
                        "key": key,
                        "cx": settings.google_search_engine_id,
                        "q": arguments.query,
                        "num": arguments.max_results,
                    },
                )
            elif settings.web_search_provider == "serpapi":
                response = client.get(
                    "https://serpapi.com/search.json",
                    params={"engine": "google", "api_key": key, "q": arguments.query, "num": arguments.max_results},
                )
            elif settings.web_search_provider == "brave":
                response = client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": arguments.query, "count": arguments.max_results},
                    headers={"X-Subscription-Token": key, "Accept": "application/json"},
                )
            else:
                response = client.post(
                    "https://api.tavily.com/search",
                    json={"query": arguments.query, "max_results": arguments.max_results, "search_depth": "basic"},
                    headers={"Authorization": f"Bearer {key}"},
                )
    except httpx.TimeoutException:
        raise TransientError("Web search timed out.") from None
    except httpx.TransportError:
        raise TransientError("Web search provider could not be reached.") from None
    if response.status_code == 429:
        raise RateLimitError("Web search rate limit reached.")
    if response.status_code in {401, 403}:
        raise ToolError("Web search provider rejected the API key.", retryable=False)
    if response.status_code >= 500:
        raise TransientError(f"Web search provider returned HTTP {response.status_code}.")
    if response.status_code >= 400:
        raise ToolError(f"Web search failed (HTTP {response.status_code}).", retryable=False)
    data = response.json()
    if settings.web_search_provider == "google":
        items = data.get("items") or []
        results = [{"title": item.get("title", ""), "url": item.get("link", ""), "content": item.get("snippet", "")} for item in items]
    elif settings.web_search_provider == "serpapi":
        if data.get("error") and not data.get("organic_results"):
            raise ToolError(f"Web search failed: {data['error']}", retryable=False)
        items = data.get("organic_results") or []
        results = [{"title": item.get("title", ""), "url": item.get("link", ""), "content": item.get("snippet", "")} for item in items]
    elif settings.web_search_provider == "brave":
        items = ((data.get("web") or {}).get("results") or [])
        results = [{"title": item.get("title", ""), "url": item.get("url", ""), "content": item.get("description", "")} for item in items]
    else:
        items = data.get("results") or []
        results = [{"title": item.get("title", ""), "url": item.get("url", ""), "content": item.get("content", "")} for item in items]
    return [
        {"title": str(item["title"])[:300], "url": str(item["url"])[:2000], "content": str(item["content"])[:2000]}
        for item in results[: arguments.max_results]
        if item["url"]
    ]
