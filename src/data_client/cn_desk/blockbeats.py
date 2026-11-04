"""BlockBeats flash feed, relayed through Cloudflare and read from the page state.

Two walls stand in the way of a plain HTTP client:

* the public API signs every call with HMAC-SHA256 and then rejects anything
  that is not a real browser (``40104 Invalid Signature`` from Python, ``200``
  from a browser on the same machine), and
* ``www.theblockbeats.info`` blackholes this server's IP entirely -- curl and
  Chromium both time out, while the same host answers Cloudflare in ~1s.

So the page is fetched through a small Cloudflare Worker relay and the flash
list is read out of the ``__NUXT__`` payload the server rendered into it. Nuxt
serialises that payload as a JavaScript expression, not JSON, so it is evaluated
with the Node binary that ships inside the Playwright package. No browser is
launched at runtime, and a result is reused for five minutes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FLASH_URL = "https://www.theblockbeats.info/newsflash"
DEFAULT_RELAY = "https://bb-flash.tristeanima233.workers.dev/"
CACHE_TTL_SECONDS = 300.0
FAILURE_TTL_SECONDS = 60.0
FETCH_TIMEOUT_SECONDS = 25.0
NODE_TIMEOUT_SECONDS = 60.0

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

_READ_STATE = """
const out = [];
const root = state && state.data && state.data[0];
for (const day of (root && root.days) || []) {
  for (const item of day.children || []) {
    if (!item || (item.lang && item.lang !== 'cn')) continue;
    out.push({
      id: String(item.id == null ? '' : item.id),
      article_id: String(item.article_id == null ? '' : item.article_id),
      title: String(item.title == null ? '' : item.title),
      content: String(item.content || item.free_content || ''),
      url: String(item.url == null ? '' : item.url),
      add_time: Number(item.add_time) || 0
    });
  }
}
console.log(JSON.stringify(out));
"""

_lock = asyncio.Lock()
_cache: tuple[float, list[dict[str, Any]]] | None = None


def _relay_url() -> str:
    return (os.environ.get("BLOCKBEATS_RELAY_URL") or DEFAULT_RELAY).strip()


def _relay_token() -> str:
    return (os.environ.get("BLOCKBEATS_RELAY_TOKEN") or "").strip()


def _node_binary() -> str | None:
    try:
        import playwright

        candidate = Path(playwright.__file__).parent / "driver" / "node"
        if candidate.is_file():
            return str(candidate)
    except Exception:
        pass
    return shutil.which("node")


def _evaluate(payload: str) -> list[dict[str, Any]]:
    """Evaluate the Nuxt payload with Node and return the flash items."""
    node = _node_binary()
    if not node:
        logger.warning("cn_desk.blockbeats_no_node")
        return []
    script = "const state=" + payload + ";\n" + _READ_STATE
    proc = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        timeout=NODE_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        logger.warning("cn_desk.blockbeats_node_failed | %s", proc.stderr.strip()[:400])
        return []
    raw = proc.stdout.strip().splitlines()
    if not raw:
        return []
    try:
        items = json.loads(raw[-1])
    except json.JSONDecodeError:
        logger.warning("cn_desk.blockbeats_bad_json | %s", raw[-1][:200])
        return []
    return items if isinstance(items, list) else []


def _extract_payload(html: str) -> str | None:
    marker = "window.__NUXT__="
    start = html.find(marker)
    if start < 0:
        return None
    chunk = html[start + len(marker) :]
    end = chunk.find("</script>")
    if end < 0:
        return None
    payload = chunk[:end].strip().rstrip(";").strip()
    return payload or None


async def _download() -> str | None:
    import httpx

    token = _relay_token()
    if not token:
        logger.warning("cn_desk.blockbeats_no_relay_token")
        return None
    params = {"k": token, "u": FLASH_URL}
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(FETCH_TIMEOUT_SECONDS),
        headers={"User-Agent": _UA},
        follow_redirects=True,
        trust_env=False,
    ) as client:
        response = await client.get(_relay_url(), params=params)
        if response.status_code != 200:
            logger.warning("cn_desk.blockbeats_relay_status | %s", response.status_code)
            return None
        return response.text


async def fetch_flashes() -> list[dict[str, Any]]:
    """Cached BlockBeats flashes, newest first. Never raises."""
    global _cache
    loop = asyncio.get_running_loop()
    if _cache is not None and loop.time() < _cache[0]:
        return _cache[1]
    async with _lock:
        if _cache is not None and loop.time() < _cache[0]:
            return _cache[1]
        items: list[dict[str, Any]] = []
        try:
            html = await _download()
            payload = _extract_payload(html or "")
            if payload:
                items = await asyncio.to_thread(_evaluate, payload)
        except Exception:
            logger.warning("cn_desk.blockbeats_failed", exc_info=True)
        if not items:
            _cache = (loop.time() + FAILURE_TTL_SECONDS, [])
            return []
        items.sort(key=lambda item: item.get("add_time") or 0, reverse=True)
        _cache = (loop.time() + CACHE_TTL_SECONDS, items)
        return items
