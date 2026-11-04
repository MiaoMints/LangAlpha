"""Full text for a single news story, fetched on demand.

Providers ship a headline and, when the feed offers one, a short description;
the body lives on the publisher's page and is in no payload. Opening a story is
the only moment a reader wants the text, so it is fetched then and cached by
article id for a few minutes: a page that blocks us, times out, or renders
client-side costs nothing beyond the description the row already carries.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import httpx
import trafilatura
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Ten minutes: reopening a story is free, yet a corrected article does not stay
# stale for the rest of a session.
BODY_TTL_SECONDS = 600.0
# A failure is usually a block, a timeout or a slow origin, not a page that has
# no text. Remember it briefly so a burst of opens does not hammer the site, but
# let the row recover on its own instead of staying empty for ten minutes.
FAILURE_TTL_SECONDS = 60.0
BODY_TIMEOUT_SECONDS = 10.0
BODY_MAX_BYTES = 3_000_000
# A runaway number of cached keys would only ever come from a crawl; this keeps
# the map bounded without a timer.
_MAX_CACHE_KEYS = 512

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# key -> (expires_at_monotonic, body or None). None is cached too: a page that
# refused us once usually refuses us again, and retrying on every open would
# cost a timeout per click.
_cache: dict[str, tuple[float, str | None]] = {}
_locks: dict[str, asyncio.Lock] = {}

_BLANK_LINES = re.compile(r"\n{3,}")

# Publisher templates put the prose in one named container. Reading that node
# directly beats a generic extractor on Chinese finance pages, whose chrome
# (nav, share bars, app prompts) otherwise crowds the article out.
_BODY_SELECTORS = (
    "#ContentBody",
    "#artibody",
    ".rich_media_content",
    "[itemprop='articleBody']",
    ".article-content",
    ".article-body",
    ".news-content",
    "#post_body",
    ".post_body",
    ".article_content",
)

_DROPPED_TAGS = ("script", "style", "noscript", "figcaption", "iframe", "form")
# A line this short is a caption, a byline or a button label, not prose.
_MIN_LINE_CHARS = 4
# Share bars, app install prompts and legal footers survive every extractor;
# they are template, not reporting, and they read as noise in a reader view.
_BOILERPLATE = re.compile(
    r"打开微信|扫一扫|扫描二维码|即可将网页分享|分享到朋友圈|手机查看财经快讯|"
    r"一手掌握市场脉搏|方便，快捷|点击底部|关注.{0,8}微信|责任编辑|免责声明|"
    r"本文仅代表作者个人观点|未经授权禁止转载"
)
# A stock container with any real prose beats a generic extraction, which on
# Chinese finance pages drags the page chrome into the body.
_MIN_CONTAINER_CHARS = 40
# Wire copy is sometimes one sentence: "3M shares hit an eight-year high, up
# 9.7%." That is the whole article, so a long-body floor would throw it away.
_MIN_TEXT_CHARS = 12


def _clean(text: str) -> str:
    joined = "\n".join(line.strip() for line in text.splitlines())
    return _BLANK_LINES.sub("\n\n", joined).strip()


def _container_text(html: str) -> str | None:
    """Prose from a publisher's named body container, or None."""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        logger.debug("news.body.parse_failed", exc_info=True)
        return None
    for selector in _BODY_SELECTORS:
        try:
            node = soup.select_one(selector)
        except Exception:
            continue
        if node is None:
            continue
        for tag in node.find_all(_DROPPED_TAGS):
            tag.decompose()
        lines = [line.strip() for line in node.get_text("\n", strip=True).splitlines()]
        kept = [line for line in lines if len(line) >= _MIN_LINE_CHARS]
        text = _clean("\n".join(kept))
        if len(text) >= _MIN_CONTAINER_CHARS or _is_substantive(text):
            return text
    return None


def _is_substantive(text: str) -> bool:
    """Real prose, not a label: long enough and made of words or numbers."""
    return len(text) >= _MIN_TEXT_CHARS and re.search(r"[0-9\u4e00-\u9fff]", text) is not None


def extract_article_text(html: str, url: str) -> str | None:
    """The article as plain text, or None when the page carries no body."""
    container = _container_text(html)
    if container:
        return container
    try:
        extracted = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=False,
            favor_recall=True,
        )
    except Exception:
        logger.debug("news.body.extract_failed", exc_info=True)
        return None
    if not extracted:
        return None
    cleaned = _clean(extracted)
    return cleaned if _is_substantive(cleaned) else None


async def _download(url: str) -> str | None:
    try:
        # trust_env=False: a proxy exported by the host must not decide whether
        # an operator can read a story, and a broken one would fail every page.
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(BODY_TIMEOUT_SECONDS, connect=5.0),
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT, "Accept-Language": "*"},
            trust_env=False,
        ) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        logger.debug("news.body.download_failed %s: %s", url, type(exc).__name__)
        return None
    if response.status_code != 200:
        logger.debug("news.body.download_status %s: %s", url, response.status_code)
        return None
    content_type = response.headers.get("content-type", "")
    if content_type and "html" not in content_type.lower():
        return None
    return response.text[:BODY_MAX_BYTES]


async def read_article_body(url: Any, *, cache_key: str | None = None) -> str | None:
    """The article's text at `url`, or None when it cannot be read.

    `cache_key` names the entry; pass the article id so a url rewritten by the
    publisher still hits the same cache line.
    """
    if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
        return None
    key = cache_key or url

    hit = _cache.get(key)
    if hit is not None and hit[0] > time.monotonic():
        return hit[1]

    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]
        html = await _download(url)
        body = extract_article_text(html, url) if html else None
        if len(_cache) >= _MAX_CACHE_KEYS:
            _cache.clear()
        ttl = BODY_TTL_SECONDS if body else FAILURE_TTL_SECONDS
        _cache[key] = (time.monotonic() + ttl, body)
        return body
