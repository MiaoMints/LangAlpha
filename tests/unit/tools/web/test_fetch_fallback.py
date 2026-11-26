"""Free-fallback behaviour of WebFetch's extraction step (no paid key needed)."""

import pytest

import src.tools.web.fetch as fetch_module
from src.tools.web.fetch import (
    DIGEST_LIMIT,
    ExtractionUnavailable,
    _extraction_models,
    _page_digest,
    _extract_with_llm,
)
from src.tools.web.types import FetchResponse, FetchResult


class _Raising:
    """LLM(name).get_llm() that fails for the names in ``broken``."""

    def __init__(self, broken):
        self.broken, self.built = set(broken), []

    def __call__(self, name):
        outer = self

        class _Client:
            def get_llm(self):
                if name in outer.broken:
                    raise ValueError(f"{name} environment variable is not set")
                outer.built.append(name)
                return object()

        return _Client()


@pytest.fixture
def no_call(monkeypatch):
    async def stop(*args, **kwargs):
        raise RuntimeError("sentinel: the call was reached")

    monkeypatch.setattr(fetch_module, "make_api_call", stop)
    monkeypatch.setattr(fetch_module, "maybe_disable_streaming", lambda llm: None)


class TestCandidates:
    def test_fallbacks_follow_the_primary_in_order_without_repeats(self, monkeypatch):
        monkeypatch.setenv("LANGALPHA_FREE_FALLBACK", "1")
        from src.config import tool_settings

        monkeypatch.setattr(
            tool_settings,
            "_get_agent_config_dict",
            lambda: {"llm": {"fallback": ["second", "first", 3, "", "second"]}},
        )
        assert _extraction_models("first") == ["first", "second"]

    def test_switch_off_leaves_the_primary_alone(self, monkeypatch):
        monkeypatch.setenv("LANGALPHA_FREE_FALLBACK", "0")
        from src.config import tool_settings

        monkeypatch.setattr(
            tool_settings, "_get_agent_config_dict", lambda: {"llm": {"fallback": ["second"]}}
        )
        assert _extraction_models("first") == ["first"]


class TestExtract:
    @pytest.mark.asyncio
    async def test_a_model_that_cannot_be_built_hands_the_page_to_the_next(
        self, monkeypatch, no_call
    ):
        monkeypatch.setattr(
            fetch_module, "_extraction_models", lambda model: [model, "spare"]
        )
        raising = _Raising(broken={"first"})

        monkeypatch.setattr(fetch_module, "LLM", raising)
        with pytest.raises(RuntimeError, match="sentinel"):
            await _extract_with_llm("# Page\n\nbody", "prompt", "first")

        assert raising.built == ["spare"]

    @pytest.mark.asyncio
    async def test_no_model_at_all_names_each_failure(self, monkeypatch, no_call):
        monkeypatch.setattr(fetch_module, "_extraction_models", lambda model: [model, "spare"])
        monkeypatch.setattr(fetch_module, "LLM", _Raising(broken={"first", "spare"}))

        with pytest.raises(ExtractionUnavailable) as caught:
            await _extract_with_llm("# Page\n\nbody", "prompt", "first")

        assert caught.value.failures == [
            "first: ValueError",
            "spare: ValueError",
        ]


class TestDigest:
    def test_links_and_tags_are_stripped_and_long_text_is_cut(self):
        page = (
            "![pic](https://img.example/a.png)\n\n# Title\n\n"
            "Read [the guide](https://site.example/guide) now.\n\n"
            "<script>x()</script>\n\n\n\n" + "word " * 2000
        )

        text = _page_digest(page)

        assert text.startswith("No extraction model is available")
        assert "![pic]" not in text and "](" not in text and "<script>" not in text
        assert "the guide" in text and "\n\n\n" not in text
        assert text.rstrip().endswith("[truncated]")
        assert len(text) < DIGEST_LIMIT + 300

    def test_short_pages_are_returned_whole(self):
        assert _page_digest("just text").endswith("just text")


class TestThroughTheTool:
    @pytest.mark.asyncio
    async def test_page_text_is_answered_when_every_model_is_missing(self, monkeypatch):
        class AnswersWithMarkdown:
            provider_names = ["stub"]

            async def fetch(self, req):
                return FetchResponse(
                    results=[
                        FetchResult(
                            url=req.urls[0],
                            markdown="# Page\n\nSee [docs](https://site.example/d). " + "fact " * 30,
                        )
                    ],
                    provider="stub",
                )

        async def no_sitemap(url):
            return ""

        monkeypatch.setattr(fetch_module, "get_fetch_service", lambda: AnswersWithMarkdown())
        monkeypatch.setattr(fetch_module, "get_sitemap_summary", no_sitemap)
        monkeypatch.setattr(fetch_module, "_extraction_models", lambda model: [model])
        monkeypatch.setattr(fetch_module, "LLM", _Raising(broken={"unused"}))

        content, artifact = await fetch_module.web_fetch(
            "https://site.example/page", "anything", model="unused", include_sitemap=False
        )

        assert content.startswith("No extraction model is available")
        assert "See docs." in content and "](https://" not in content
        assert artifact["error"] == "extraction_unavailable"

    @pytest.mark.asyncio
    async def test_switch_off_does_not_degrade_to_page_digest(self, monkeypatch):
        class AnswersWithMarkdown:
            provider_names = ["stub"]

            async def fetch(self, req):
                return FetchResponse(
                    results=[FetchResult(url=req.urls[0], markdown="# Page\n\n" + "fact " * 30)],
                    provider="stub",
                )

        monkeypatch.setenv("LANGALPHA_FREE_FALLBACK", "0")
        monkeypatch.setattr(fetch_module, "get_fetch_service", lambda: AnswersWithMarkdown())
        monkeypatch.setattr(fetch_module, "_extraction_models", lambda model: [model])
        monkeypatch.setattr(fetch_module, "LLM", _Raising(broken={"unused"}))

        content, artifact = await fetch_module.web_fetch(
            "https://site.example/page", "anything", model="unused", include_sitemap=False
        )

        assert artifact["error"] == "exception"
        assert content.startswith("Failed to process")
