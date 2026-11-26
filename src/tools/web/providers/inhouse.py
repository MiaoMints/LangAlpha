"""In-house fetch adapter over SafeCrawlerWrapper.

The zero-key default and terminal chain entry: OSS deployments work with no
vendor keys, and it keeps the YouTube-transcript / X-post / PDF extractors
no third party replicates. Returns raw page markdown (llm extraction policy).
"""

import asyncio
import heapq
import html as html_module
import itertools
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from src.tools.web.inhouse.guard import fetch_host
from src.tools.web.providers._shared import clip_error, empty_content_error, is_empty_markdown
from src.tools.web.types import (
    CrawlJob,
    CrawlPage,
    CrawlRequest,
    CrawlState,
    CrawlStatus,
    FetchRequest,
    FetchResponse,
    FetchResult,
    MapRequest,
    UrlInfo,
    WebError,
    WebErrorType,
    WebToolError,
)

logger = logging.getLogger(__name__)

# SafeCrawlerWrapper CrawlResult.error_type → (normalized type, provider_fault).
#
# provider_fault is stated for every outcome instead of falling back to the
# error type's default: it means "this failure is independent of the URL we
# asked for", so the chain should stop routing here entirely. A host that
# blocks, rate-limits, times out or refuses the connection is a property of
# that host — it must not open a breaker shared by every URL.
_ERROR_TYPES = {
    # Target-side.
    "blocked": (WebErrorType.FORBIDDEN, False),
    "stealth_failed": (WebErrorType.ANTI_BOT, False),
    "timeout": (WebErrorType.TIMEOUT, False),
    "connection_timeout": (WebErrorType.TIMEOUT, False),
    "rate_limited": (WebErrorType.RATE_LIMITED, False),
    "host_circuit_open": (WebErrorType.CIRCUIT_OPEN, False),
    "empty_content": (WebErrorType.EMPTY, False),
    "dns_error": (WebErrorType.PROVIDER_ERROR, False),
    "connection_refused": (WebErrorType.PROVIDER_ERROR, False),
    "network_error": (WebErrorType.PROVIDER_ERROR, False),
    "crawl_error": (WebErrorType.PROVIDER_ERROR, False),
    # Despite the name, its only producer is the Tier 1 unreachable
    # short-circuit (scrapling_crawler.py) — "could not resolve" and
    # "connection refused" are the target's problem, not ours.
    "infra_error": (WebErrorType.PROVIDER_ERROR, False),
    # Our own capacity/infrastructure — any other URL would fail the same way.
    "queue_full": (WebErrorType.PROVIDER_ERROR, True),
    "browser_closed": (WebErrorType.PROVIDER_ERROR, True),
    # The infra breaker, also ours, yet off so the router's breaker does not
    # count the browser_closed failures that opened it a second time.
    "circuit_open": (WebErrorType.CIRCUIT_OPEN, False),
}


class InhouseFetchAdapter:
    """FetchAdapter over the in-house tiered crawler (scrapling)."""

    name = "inhouse"

    async def _fetch_one(self, crawler, url: str) -> FetchResult:
        result = await crawler.crawl(url)
        if result.success:
            return FetchResult(url=url, title=result.title, markdown=result.markdown)
        kind = result.error_type or "crawl_error"
        if kind == "cancelled":
            # Not a failure at all — the caller stopped. Neither retry it on
            # the next provider nor count it against this one.
            return FetchResult(
                url=url,
                error=WebError(
                    type=WebErrorType.PROVIDER_ERROR,
                    message="Crawl was cancelled",
                    retryable=False,
                    provider_fault=False,
                    native_kind=kind,
                ),
            )
        mapped = _ERROR_TYPES.get(kind)
        if mapped is None:
            # A crawler outcome added without a mapping here. Default to
            # target-side so it cannot silently open the provider breaker.
            logger.warning("inhouse fetch: unmapped crawler error_type %r", kind)
            mapped = (WebErrorType.PROVIDER_ERROR, False)
        error_type, provider_fault = mapped
        return FetchResult(
            url=url,
            error=WebError(
                type=error_type,
                message=str(result.error or kind)[:300],
                provider_fault=provider_fault,
                native_kind=kind,
            ),
        )

    async def fetch(self, req: FetchRequest, native_params: Dict[str, Any]) -> FetchResponse:
        from src.tools.web.inhouse.safe_wrapper import get_safe_crawler

        crawler = await get_safe_crawler()
        results: List[FetchResult] = await asyncio.gather(
            *(self._fetch_one(crawler, u) for u in req.urls)
        )
        return FetchResponse(results=list(results), provider=self.name)


# ---------------------------------------------------------------------------
# Site crawl + map without a vendor key
# ---------------------------------------------------------------------------

_CRAWL_JOBS: Dict[str, "_Job"] = {}

# A link to one of these is a download, not a page: skipped before any request.
_BINARY_SUFFIX = frozenset(
    """7z avi bin bmp bz2 css csv deb dmg doc docx eot exe flv gif gz iso jar jpeg jpg
    js json m4a md5 mov mp3 mp4 msi odt ogg ogv otf pdf pkg png ppt pptx psd rar rpm
    svg tar tgz ttf txt wav webm webp wmv woff woff2 xls xlsx xml zip""".split()
)
_MAX_HTML_BYTES = 2_000_000
_CRAWL_CONCURRENCY = 4
_DISCOVERY_FACTOR = 10  # links a crawl may look at, as a multiple of its page cap


@dataclass
class _Job:
    id: str
    pages: List[CrawlPage]
    state: CrawlState
    error: Optional[WebError]
    task: Optional["asyncio.Task"] = None
    finished_at: Optional[float] = None


_JOB_TTL_SECONDS = 1800.0
_MAX_CRAWL_JOBS = 128


def _prune_crawl_jobs() -> None:
    """Drop stale terminal jobs so an unpolled crawl cannot leak memory."""
    now = time.monotonic()
    stale = [
        jid
        for jid, j in _CRAWL_JOBS.items()
        if j.state.terminal and j.finished_at is not None and now - j.finished_at > _JOB_TTL_SECONDS
    ]
    for jid in stale:
        _CRAWL_JOBS.pop(jid, None)
    if len(_CRAWL_JOBS) >= _MAX_CRAWL_JOBS:
        terminal = sorted(
            ((j.finished_at or 0.0, jid) for jid, j in _CRAWL_JOBS.items() if j.state.terminal)
        )
        for _, jid in terminal[: len(_CRAWL_JOBS) - _MAX_CRAWL_JOBS + 1]:
            _CRAWL_JOBS.pop(jid, None)


def _site_key(url: str) -> Optional[Tuple[str, str, int]]:
    """(scheme, host, port) of a web URL; None for anything else."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return (parts.scheme, parts.hostname.lower(), port)


def _terms(query: Optional[str]) -> List[str]:
    return [t for t in re.findall(r"\w+", (query or "").lower()) if len(t) > 1]


def _relevance(url: str, title: str, terms: List[str]) -> int:
    """How many query terms a link's address and title mention."""
    if not terms:
        return 0
    text = f"{url} {title or ''}".lower()
    return sum(1 for t in terms if t in text)


def _links_from_html(html: str, base: str) -> List[Tuple[str, str]]:
    """Absolute (url, anchor text) pairs from ``<a href>`` tags, fragments dropped."""
    out: Dict[str, str] = {}
    for href, inner in re.findall(r"<a\b[^>]*?\bhref\s*=\s*[\"']([^\"'>]+)[\"'][^>]*>(.*?)</a>", html, re.I | re.S):
        href = href.strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "data:")):
            continue
        text = html_module.unescape(re.sub(r"<[^>]+>", " ", inner))
        text = " ".join(text.split())[:200]
        out.setdefault(urljoin(base, href).split("#", 1)[0], text)
    return list(out.items())


class InhouseCrawlAdapter:
    """Crawl over the in-house crawler: breadth-first, same site only.

    Each page is read twice — once by the safe crawler for its markdown, once
    as plain HTML to find its links. Jobs live in this process's memory, so a
    restart loses them; pages already polled are gone with the job.
    """

    name = "inhouse"

    def __init__(self, html_fetch: Optional[Any] = None) -> None:
        self._html_fetch = html_fetch

    async def _fetch_html(self, url: str) -> str:
        if self._html_fetch is None:
            from src.tools.web.inhouse.guard import fetch_capped

            body = await fetch_capped(url, cap=lambda _seen: _MAX_HTML_BYTES)
        else:
            body = await self._html_fetch(url)
        if isinstance(body, str):
            return body
        return body.decode("utf-8", errors="ignore")

    async def _links(self, url: str) -> List[Tuple[str, str]]:
        try:
            html = await self._fetch_html(url)
        except Exception as e:  # guarded transport: blocked hosts, caps, timeouts
            logger.debug("inhouse crawl: no links from %s (%s)", fetch_host(url), type(e).__name__)
            return []
        return _links_from_html(html, url)

    @staticmethod
    def _allowed(url: str, site: Tuple[str, str, int], include: List[Any], exclude: List[Any]) -> bool:
        if _site_key(url) != site:
            return False
        parts = urlsplit(url)
        path = parts.path or "/"
        tail = path.rsplit("/", 1)[-1]
        if "." in tail and tail.rsplit(".", 1)[-1].lower() in _BINARY_SUFFIX:
            return False
        if len(parts.query) > 200:
            return False
        if any(p.search(path) for p in exclude):
            return False
        return not include or any(p.search(path) for p in include)

    async def _run(self, job: _Job, req: CrawlRequest) -> None:
        from src.tools.web.inhouse.safe_wrapper import get_safe_crawler

        include = [re.compile(p) for p in req.include_paths or []]
        exclude = [re.compile(p) for p in req.exclude_paths or []]
        site = _site_key(req.url)
        terms = _terms(req.query)
        max_seen = max(req.limit * _DISCOVERY_FACTOR, req.limit)
        crawler = await get_safe_crawler()
        heap: List[Tuple[int, int, int, str]] = []
        order = itertools.count()
        heapq.heappush(heap, (0, 0, next(order), req.url.split("#", 1)[0]))
        enqueued = {req.url.split("#", 1)[0]}
        taken = 0  # pages reserved against the cap, fetched or not yet
        inflight = 0
        lock = asyncio.Lock()

        async def worker() -> None:
            nonlocal taken, inflight
            while True:
                async with lock:
                    if taken >= req.limit:
                        return
                    if not heap:
                        if inflight == 0:
                            return
                        idle = True
                    else:
                        idle = False
                        _, depth, _, url = heapq.heappop(heap)
                        taken += 1
                        inflight += 1
                if idle:
                    # Another worker is still reading a page that may link onward.
                    await asyncio.sleep(0.05)
                    continue
                try:
                    page = await self._page(crawler, url)
                    links: List[Tuple[str, str]] = []
                    if page.ok and (req.max_depth is None or depth < req.max_depth):
                        links = await self._links(url)
                finally:
                    async with lock:
                        inflight -= 1
                async with lock:
                    job.pages.append(page)
                    for link, text in links:
                        if taken + len(enqueued) >= req.limit + max_seen or link in enqueued:
                            continue
                        if not self._allowed(link, site, include, exclude):
                            continue
                        enqueued.add(link)
                        rank = -_relevance(link, text, terms)
                        heapq.heappush(heap, (rank, depth + 1, next(order), link))

        try:
            await asyncio.gather(*(worker() for _ in range(_CRAWL_CONCURRENCY)))
        except asyncio.CancelledError:
            job.state = CrawlState.CANCELLED
            raise
        except Exception as e:
            logger.warning("inhouse crawl %s failed: %r", job.id, e)
            job.error = WebError(type=WebErrorType.PROVIDER_ERROR, message=clip_error(str(e)))
            job.state = CrawlState.FAILED
        else:
            job.state = CrawlState.COMPLETED
            if not any(p.ok for p in job.pages):
                # Nothing readable: say why on the job, not only on each page.
                job.error = next((p.error for p in job.pages if p.error), empty_content_error())
        finally:
            job.finished_at = time.monotonic()

    async def _page(self, crawler: Any, url: str) -> CrawlPage:
        try:
            fetched = await InhouseFetchAdapter()._fetch_one(crawler, url)
        except Exception as e:  # a page that breaks the crawler is one bad row
            logger.debug("inhouse crawl: %s failed (%r)", fetch_host(url), e)
            return CrawlPage(
                url=url,
                error=WebError(type=WebErrorType.PROVIDER_ERROR, message=clip_error(str(e))),
            )
        page = CrawlPage(url=fetched.url, title=fetched.title, markdown=fetched.markdown, error=fetched.error)
        if page.ok and page.markdown is not None and is_empty_markdown(page.markdown):
            return CrawlPage(url=url, title=fetched.title, error=empty_content_error())
        return page

    async def start_crawl(self, req: CrawlRequest, native_params: Dict[str, Any]) -> CrawlJob:
        if _site_key(req.url) is None:
            raise WebToolError(
                WebError(type=WebErrorType.UNSUPPORTED_URL, message=clip_error(f"Not a web page: {req.url}"))
            )
        _prune_crawl_jobs()
        job_id = uuid.uuid4().hex
        job = _Job(id=job_id, pages=[], state=CrawlState.RUNNING, error=None)
        job.task = asyncio.create_task(self._run(job, req))
        _CRAWL_JOBS[job_id] = job
        return CrawlJob(id=job_id, provider=self.name)

    async def crawl_status(self, job_id: str, skip: int = 0) -> CrawlStatus:
        job = _CRAWL_JOBS.get(job_id)
        if job is None:
            raise WebToolError(
                WebError(type=WebErrorType.NOT_FOUND, message=clip_error(f"Unknown crawl job {job_id}"))
            )
        status = CrawlStatus(
            state=job.state,
            total=len(job.pages),
            completed=len(job.pages),
            pages=list(job.pages[skip:]),
            error=job.error,
        )
        if job.state.terminal:
            # Reported once: nothing polls a finished job again.
            _CRAWL_JOBS.pop(job_id, None)
        return status

    async def cancel_crawl(self, job_id: str) -> None:
        job = _CRAWL_JOBS.get(job_id)
        if job is None:
            return
        # Kept in the table so the caller can still poll the cancelled state once.
        job.state = CrawlState.CANCELLED
        job.finished_at = time.monotonic()
        if job.task is not None and not job.task.done():
            job.task.cancel()

    async def map_site(self, req: MapRequest, native_params: Dict[str, Any]) -> List[UrlInfo]:
        from src.tools.web.inhouse.sitemap import fetch_sitemap_urls

        if _site_key(req.url) is None:
            raise WebToolError(
                WebError(type=WebErrorType.UNSUPPORTED_URL, message=clip_error(f"Not a web page: {req.url}"))
            )
        found: Dict[str, UrlInfo] = {}

        def keep(url: str, title: Optional[str]) -> None:
            if url not in found and len(found) < req.limit * _DISCOVERY_FACTOR:
                found[url] = UrlInfo(url=url, title=title or None)

        # A sitemap lists what the site itself considers its pages.
        for row in await fetch_sitemap_urls(urlsplit(req.url).netloc, max_urls=req.limit * _DISCOVERY_FACTOR):
            if self._allowed(row["url"], _site_key(req.url), [], []):
                keep(row["url"], None)
        # Sites without one still link their pages from the start page.
        for link, text in await self._links(req.url):
            if link != req.url and self._allowed(link, _site_key(req.url), [], []):
                keep(link, text)

        terms = _terms(req.query)
        ranked = sorted(found.values(), key=lambda i: -_relevance(i.url, i.title, terms))
        return ranked[: max(1, req.limit)]
