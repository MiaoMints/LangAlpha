"""The built-in site crawl and map: discovery rules, job lifecycle, registration.

No network: pages come from an injected HTML reader, markdown from a stubbed
crawler. URLs and content are neutral placeholders.
"""

import asyncio

import pytest

import src.tools.web.crawl as crawl_mod
import src.tools.web.providers.inhouse as inhouse
from src.tools.web.providers.inhouse import InhouseCrawlAdapter, InhouseFetchAdapter
from src.tools.web.types import CrawlRequest, CrawlState, MapRequest, WebErrorType, WebToolError
from tests.unit.tools.web.test_crawl import FakeBackend

SITE = "https://site.example"
ROOT = f"{SITE}/"
A = f"{SITE}/guide/setup"
B = f"{SITE}/blog/post"
DEEP = f"{SITE}/guide/advanced"
OFF = "https://elsewhere.example/x"
PDF = f"{SITE}/files/report.pdf"
OTHER_PORT = f"{SITE}:8443/admin"

PAGES = {
    ROOT: f'<a href="/guide/setup">Setup guide</a> <a href="{B}">Post</a>'
    f' <a href="{OFF}">Off site</a> <a href="{PDF}">Report</a> <a href="{OTHER_PORT}">Admin</a>'
    f'<a href="mailto:x@y.z">Mail</a> <a href="{ROOT}#top">Top</a>',
    A: f'<a href="/guide/advanced">Advanced</a>',
    B: "",
    DEEP: "",
}


async def _page(url: str) -> str:
    if url == f"{SITE}/guide/advanced":
        raise RuntimeError("blocked by policy")
    return PAGES[url]


@pytest.fixture
def crawl(monkeypatch):
    async def no_crawler():
        return object()

    async def markdown(self, crawler, url):
        from src.tools.web.types import FetchResult

        if url == DEEP:
            raise RuntimeError("crawler crashed")
        return FetchResult(url=url, title=url.rsplit("/", 1)[-1] or "home", markdown=f"Body of {url}.")

    monkeypatch.setattr("src.tools.web.inhouse.safe_wrapper.get_safe_crawler", no_crawler)
    monkeypatch.setattr(InhouseFetchAdapter, "_fetch_one", markdown)
    return InhouseCrawlAdapter(html_fetch=_page)


def _req(**kw):
    return CrawlRequest(url=ROOT, limit=kw.pop("limit", 10), **kw)


async def _finish(adapter, req):
    job = await adapter.start_crawl(req, {})
    await asyncio.gather(_CRAWL_JOBS[job.id].task, return_exceptions=True)
    return await adapter.crawl_status(job.id)





_CRAWL_JOBS = inhouse._CRAWL_JOBS


@pytest.mark.asyncio
class TestCrawl:
    async def test_follows_same_site_links_and_skips_downloads(self, crawl):
        status = await _finish(crawl, _req())
        assert status.state is CrawlState.COMPLETED
        urls = [p.url for p in status.pages]
        assert urls[0] == ROOT
        assert set(urls) == {ROOT, A, B, DEEP}
        assert status.total == len(urls) == 4
        assert all(p.error is None and p.markdown for p in status.pages if p.url != DEEP)
        deep = next(p for p in status.pages if p.url == DEEP)  # the crawler crashes on it
        assert deep.error is not None and deep.error.type is WebErrorType.PROVIDER_ERROR

    async def test_unreadable_page_is_an_error_row_not_a_crash(self, crawl):
        status = await _finish(crawl, _req())
        failed = [p for p in status.pages if p.url == DEEP]
        assert len(failed) == 1
        assert failed[0].error is not None and failed[0].markdown is None
        assert status.error is None  # one bad page does not fail the job

    async def test_limit_is_a_hard_cap(self, crawl):
        status = await _finish(crawl, _req(limit=2))
        assert [p.url for p in status.pages] == [ROOT, A]

    async def test_depth_stops_discovery(self, crawl):
        status = await _finish(crawl, _req(max_depth=1))
        assert DEEP not in [p.url for p in status.pages]

    async def test_include_and_exclude_paths(self, crawl):
        status = await _finish(crawl, _req(include_paths=["^/guide/"]))
        assert {p.url for p in status.pages} == {ROOT, A, DEEP}
        inhouse._CRAWL_JOBS.clear()
        status = await _finish(crawl, _req(exclude_paths=["^/blog"]))
        assert B not in [p.url for p in status.pages]

    async def test_query_ranks_matching_pages_first(self, crawl):
        status = await _finish(crawl, _req(query="setup", limit=2))
        assert [p.url for p in status.pages] == [ROOT, A]

    async def test_cancel_stops_the_crawl(self, monkeypatch):
        gate = asyncio.Event()

        async def slow(url):
            await gate.wait()
            return ""

        adapter = InhouseCrawlAdapter(html_fetch=slow)
        job = await adapter.start_crawl(_req(limit=20), {})
        await asyncio.sleep(0.01)
        await adapter.cancel_crawl(job.id)
        status = await adapter.crawl_status(job.id)
        assert status.state is CrawlState.CANCELLED
        assert job.id not in _CRAWL_JOBS
        gate.set()
        await asyncio.sleep(0.01)

    async def test_finished_job_is_reported_once(self, crawl):
        status = await _finish(crawl, _req())
        assert status.state is CrawlState.COMPLETED
        assert not _CRAWL_JOBS
        with pytest.raises(WebToolError) as miss:
            await crawl.crawl_status("0" * 32)
        assert miss.value.error.type is WebErrorType.NOT_FOUND

    async def test_non_web_url_is_refused(self, crawl):
        with pytest.raises(WebToolError) as miss:
            await crawl.start_crawl(CrawlRequest(url="ftp://site.example/x", limit=5), {})
        assert miss.value.error.type is WebErrorType.UNSUPPORTED_URL

    async def test_nothing_readable_is_an_error_on_the_job(self, crawl, monkeypatch):
        from src.tools.web.types import FetchResult

        async def blank(self, crawler, url):
            return FetchResult(url=url, markdown="")

        monkeypatch.setattr(InhouseFetchAdapter, "_fetch_one", blank)
        status = await _finish(crawl, _req(limit=1))
        assert status.state is CrawlState.COMPLETED
        assert [p.url for p in status.pages] == [ROOT] and not status.pages[0].ok
        assert status.error is not None and status.error.type is WebErrorType.EMPTY

    async def test_empty_job_is_an_error(self, crawl):
        status = await _finish(crawl, _req(limit=0))
        assert status.pages == [] and status.error is not None


@pytest.fixture
def no_sitemap(monkeypatch):
    async def sitemap(domain, max_urls=100, timeout=10.0):
        return []

    monkeypatch.setattr("src.tools.web.inhouse.sitemap.fetch_sitemap_urls", sitemap)


@pytest.mark.asyncio
class TestMap:
    async def test_sitemap_and_start_page_links_are_merged_and_ranked(self, crawl, monkeypatch):
        async def sitemap(domain, max_urls=100, timeout=10.0):
            assert domain == "site.example"
            return [{"url": f"{SITE}/orphan"}, {"url": OFF}]

        monkeypatch.setattr("src.tools.web.inhouse.sitemap.fetch_sitemap_urls", sitemap)
        found = await crawl.map_site(MapRequest(url=ROOT, query="setup", limit=50), {})
        urls = [i.url for i in found]
        assert urls[0] == A  # the only link whose text mentions the query
        assert set(urls) == {A, B, f"{SITE}/orphan"}
        assert all(i.url.startswith(SITE) for i in found)

    async def test_limit_bounds_the_answer(self, crawl, no_sitemap):
        found = await crawl.map_site(MapRequest(url=ROOT, limit=2), {})
        assert len(found) == 2


class TestRegistration:
    def test_no_key_with_the_fallback_on_registers_the_builtin_tools(self, monkeypatch):
        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
        monkeypatch.setenv("LANGALPHA_FREE_FALLBACK", "1")
        tools = crawl_mod.create_crawl_tools(FakeBackend())
        assert [t.name for t in tools] == ["WebCrawl", "WebMap"]

    def test_no_key_with_the_fallback_off_registers_nothing(self, monkeypatch):
        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
        monkeypatch.setenv("LANGALPHA_FREE_FALLBACK", "0")
        assert crawl_mod.create_crawl_tools(FakeBackend()) == []

    def test_allowed_keeps_extensionless_paths_named_like_binary_extensions(self):
        site = inhouse._site_key(ROOT)
        assert InhouseCrawlAdapter._allowed(f"{SITE}/docs/pdf", site, [], []) is True
        assert InhouseCrawlAdapter._allowed(f"{SITE}/api/json", site, [], []) is True
        assert InhouseCrawlAdapter._allowed(f"{SITE}/files/report.pdf", site, [], []) is False
