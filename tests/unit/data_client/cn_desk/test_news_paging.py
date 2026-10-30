"""Paging helpers for the cn-desk general feed. No network."""

from src.data_client.cn_desk.news_source import _slice_page, _stamp


def test_naive_eastmoney_clock_stays_shanghai():
    stamped = _stamp("2026-10-25 15:43:25")
    assert stamped.startswith("2026-10-25T15:43:25")
    assert stamped.endswith("+08:00")


def test_aware_timestamp_is_left_alone():
    stamped = _stamp("Sun, 25 Oct 2026 07:37:52 GMT")
    assert stamped.startswith("2026-10-25T07:37:52")
    assert "+00:00" in stamped


def test_cursor_reads_the_following_rows():
    rows = [{"id": f"id-{i}", "title": f"t{i}"} for i in range(8)]
    page, more = _slice_page(rows, 3, None, False)
    assert more is True
    assert [row["id"] for row in page] == ["id-0", "id-1", "id-2"]

    page2, more2 = _slice_page(rows, 3, page[-1]["id"], False)
    assert more2 is True
    assert [row["id"] for row in page2] == ["id-3", "id-4", "id-5"]
    assert {row["id"] for row in page}.isdisjoint(row["id"] for row in page2)


def test_full_upstream_page_is_not_treated_as_the_end():
    rows = [{"id": "a", "title": "a"}]
    assert _slice_page(rows, 2, None, True) is None


def test_short_feed_has_no_next_page():
    rows = [{"id": "a", "title": "a"}, {"id": "b", "title": "b"}]
    page, more = _slice_page(rows, 5, None, False)
    assert [row["id"] for row in page] == ["a", "b"]
    assert more is False


def test_missing_cursor_ends_once_upstream_is_exhausted():
    page, more = _slice_page([{"id": "a", "title": "a"}], 5, "gone", False)
    assert page == []
    assert more is False
