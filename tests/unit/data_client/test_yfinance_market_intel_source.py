"""The free ``MarketIntelSource``: row shapes, units and the FINRA file, with Yahoo replaced by frames."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from src.data_client.base import IntervalNotSupported
from src.data_client.yfinance import market_intel_source as mis

FINRA_TEXT = """Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market
20261125|A|515153.884785|3797|1061246.031589|B,Q,N
20261125|AAPL|24116232.1|1024|45321000.9|B,Q,N
20261125|BAD|x|1|10|B,Q,N
20261125|SHORT|1|2
"""


CHAIN_COLUMNS = ["contractSymbol", "strike"]


def _frame(rows: list[dict], columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=columns)


def _chain(calls: list[dict], puts: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(calls=_frame(calls, CHAIN_COLUMNS), puts=_frame(puts, CHAIN_COLUMNS))


@pytest.fixture
def ticker(monkeypatch):
    """``install(fake)`` makes every ``yf.Ticker(...)`` return ``fake``."""

    def install(fake):
        monkeypatch.setattr(mis.yf, "Ticker", lambda symbol: fake)
        return fake

    return install


@pytest.fixture
def finra(monkeypatch):
    """Clears the FINRA cache before and after a test."""
    mis._finra_cache.clear()
    yield
    mis._finra_cache.clear()


class TestParseFinra:
    def test_decimal_volumes_are_kept(self):
        rows = mis._parse_finra(FINRA_TEXT)

        # FINRA publishes fractional share counts; an int() read used to drop every such row.
        assert [row["ticker"] for row in rows] == ["A", "AAPL"]
        aapl = rows[1]
        assert aapl["date"] == "2026-11-25"
        assert aapl["short_volume"] == 24116232
        assert aapl["total_volume"] == 45321000
        assert aapl["short_volume_ratio"] == 53.21

    def test_unreadable_and_short_rows_are_skipped(self):
        assert [row["ticker"] for row in mis._parse_finra(FINRA_TEXT)] == ["A", "AAPL"]

    def test_unknown_header_or_empty_text_reads_as_nothing(self):
        assert mis._parse_finra("") == []
        assert mis._parse_finra("Date|Ticker|Short|Total\n20261125|AAPL|1|2\n") == []


class TestFinraFile:
    def test_a_failed_download_is_empty_and_cached(self, finra, monkeypatch):
        calls = []

        def boom(url, timeout):
            calls.append(url)
            raise OSError("offline")

        monkeypatch.setattr(mis, "http_get", boom)
        assert mis._finra_file("20261125") == []
        assert mis._finra_file("20261125") == []
        assert calls == [mis.FINRA_URL.format(yyyymmdd="20261125")]

    def test_a_published_file_is_parsed(self, finra, monkeypatch):
        monkeypatch.setattr(mis, "http_get", lambda url, timeout: SimpleNamespace(text=FINRA_TEXT, raise_for_status=lambda: None))
        rows = mis._finra_file("20261125")
        assert [row["ticker"] for row in rows] == ["A", "AAPL"]


class TestShortVolume:
    def test_newest_first_under_a_limit(self, finra, monkeypatch):
        monkeypatch.setattr(mis, "_finra_days", lambda: ["20261126", "20261125", "20261124"])
        files = {
            "20261126": [],
            "20261125": [{"ticker": "AAPL", "date": "20261125", "short_volume": 3, "total_volume": 10, "short_volume_ratio": 30.0}],
            "20261124": [{"ticker": "AAPL", "date": "20261124", "short_volume": 4, "total_volume": 8, "short_volume_ratio": 50.0}],
        }
        monkeypatch.setattr(mis, "_finra_file", lambda day: files[day])

        rows = asyncio.run(mis.YFinanceMarketIntelSource().get_short_volume("AAPL.US", limit=1, sort="date.desc"))

        assert [row["date"] for row in rows] == ["20261125"]

    def test_non_us_symbols_are_not_looked_up(self, finra, monkeypatch):
        def never(day):
            raise AssertionError(f"FINRA asked for {day}")

        monkeypatch.setattr(mis, "_finra_file", never)
        source = mis.YFinanceMarketIntelSource()
        assert asyncio.run(source.get_short_volume("0700.HK")) == []
        assert asyncio.run(source.get_short_volume("VOD.L")) == []


class TestShortInterestAndFloat:
    def test_short_interest_from_the_profile(self, ticker):
        stamp = int(datetime(2026, 11, 13, tzinfo=timezone.utc).timestamp())
        ticker(SimpleNamespace(info={"sharesShort": 143471087, "dateShortInterest": stamp, "shortRatio": 3.04}))

        rows = asyncio.run(mis.YFinanceMarketIntelSource().get_short_interest("AAPL"))

        assert rows == [
            {
                "ticker": "AAPL",
                "settlement_date": "2026-11-13",
                "short_interest": 143471087.0,
                "avg_daily_volume": pytest.approx(143471087 / 3.04),
                "days_to_cover": 3.04,
            }
        ]

    def test_no_short_data_is_an_empty_list(self, ticker):
        ticker(SimpleNamespace(info={}))
        assert asyncio.run(mis.YFinanceMarketIntelSource().get_short_interest("AAPL")) == []

    def test_float_shares(self, ticker):
        ticker(SimpleNamespace(info={"floatShares": 14507477291, "sharesOutstanding": 14800000000}))
        row = asyncio.run(mis.YFinanceMarketIntelSource().get_float_shares("AAPL"))
        assert row == {"symbol": "AAPL", "free_float": 14507477291.0, "listed_shares": 14800000000.0, "free_float_percent": 98.02, "date_filed": None}


class TestOptionsChain:
    CALLS = [
        {"contractSymbol": "AAPL261218C00250000", "strike": 250.0},
        {"contractSymbol": "AAPL261218C00200000", "strike": 200.0},
    ]
    PUTS = [{"contractSymbol": "AAPL261218P00200000", "strike": 200.0}]

    @pytest.fixture
    def optioned(self, ticker):
        fake = SimpleNamespace(
            options=["2026-12-18", "2026-11-20"],
            option_chain=lambda expiry: _chain(self.CALLS, self.PUTS) if expiry == "2026-12-18" else _chain([], [{"contractSymbol": "AAPL261118P00200000", "strike": 200.0}]),
        )
        return ticker(fake)

    def test_rows_follow_the_ginlix_shape(self, optioned):
        result = asyncio.run(mis.YFinanceMarketIntelSource().get_options_chain("AAPL"))

        assert result["next_cursor"] is None
        assert result["truncated"] is False
        assert result["results"][0] == {
            "ticker": "O:AAPL261118P00200000",
            "underlying_ticker": "AAPL",
            "contract_type": "put",
            "expiration_date": "2026-11-20",
            "strike_price": 200.0,
            "shares_per_contract": 100,
            "primary_exchange": "OPRA",
        }
        assert [(row["expiration_date"], row["strike_price"], row["contract_type"]) for row in result["results"]] == [
            ("2026-11-20", 200.0, "put"),
            ("2026-12-18", 200.0, "call"),
            ("2026-12-18", 200.0, "put"),
            ("2026-12-18", 250.0, "call"),
        ]

    def test_filters_and_limit(self, optioned):
        result = asyncio.run(
            mis.YFinanceMarketIntelSource().get_options_chain(
                "AAPL", contract_type="calls", strike_price_gte=210, expiration_date_gte="2026-12-01"
            )
        )
        assert [(row["ticker"], row["strike_price"]) for row in result["results"]] == [("O:AAPL261218C00250000", 250.0)]

        result = asyncio.run(mis.YFinanceMarketIntelSource().get_options_chain("AAPL", limit=2))
        assert len(result["results"]) == 2
        assert result["truncated"] is True


class TestOptionsSnapshot:
    QUOTE = {
        "contractSymbol": "AAPL261218C00200000",
        "strike": 200.0,
        "lastPrice": 5.5,
        "bid": 5.0,
        "ask": 6.0,
        "change": 0.5,
        "percentChange": 10.0,
        "volume": 10,
        "impliedVolatility": 0.3,
        "openInterest": 100,
        "lastTradeDate": pd.Timestamp("2026-11-25 15:00:00-0500"),
    }

    def test_quoted_contract_carries_units_yahoo_reports(self, ticker):
        asked = []

        def option_chain(expiry):
            asked.append(expiry)
            return SimpleNamespace(calls=_frame([self.QUOTE], list(self.QUOTE)), puts=_frame([], list(self.QUOTE)))

        ticker(SimpleNamespace(fast_info={"lastPrice": 210.0}, option_chain=option_chain))

        [row] = asyncio.run(mis.YFinanceMarketIntelSource().get_options_snapshot(["O:AAPL261218C00200000"]))

        assert row["ticker"] == "O:AAPL261218C00200000"
        # Yahoo takes only the dashed expiry; the bare OCC digits raised there and left the row empty.
        assert asked == ["2026-12-18"]
        # Unix ns, which is what the snapshot table dates and ages a session by.
        assert row["session"]["last_updated"] == int(self.QUOTE["lastTradeDate"].timestamp() * 1e9)
        assert row["session"]["timeframe"] == "DELAYED"
        assert row["session"]["close"] == 5.5
        assert row["session"]["change_percent"] == 10.0
        assert row["last_quote"] == {"bid": 5.0, "ask": 6.0, "mid": 5.5}
        assert row["break_even_price"] == 205.5
        assert row["underlying_asset"] == {"ticker": "AAPL", "price": 210.0}
        assert row["implied_volatility"] == 0.3
        assert row["open_interest"] == 100.0

    def test_an_unreadable_code_still_gets_an_empty_row(self, ticker):
        ticker(SimpleNamespace(fast_info={}, option_chain=lambda expiry: _chain([], [])))

        [row] = asyncio.run(mis.YFinanceMarketIntelSource().get_options_snapshot(["not-an-option"]))

        assert row["ticker"] == "O:not-an-option"
        assert row["session"]["close"] is None
        assert row["session"]["last_updated"] is None

    def test_a_chain_that_fails_to_download_leaves_the_row_unpriced(self, ticker):
        def fail(expiry):
            raise OSError("offline")

        ticker(SimpleNamespace(fast_info={"lastPrice": 210.0}, option_chain=fail))

        [row] = asyncio.run(mis.YFinanceMarketIntelSource().get_options_snapshot(["AAPL261218C00200000"]))

        assert row["session"]["close"] is None
        assert row["underlying_asset"] == {"ticker": "AAPL", "price": 210.0}

    def test_nat_and_nan_timestamps_produce_none_and_cached_underlying(self, monkeypatch):
        calls = 0

        def make_ticker(root):
            nonlocal calls
            calls += 1
            q1 = {**self.QUOTE, "lastTradeDate": pd.NaT}
            q2 = {**self.QUOTE, "contractSymbol": "AAPL261218P00200000", "lastTradeDate": float("nan")}
            return SimpleNamespace(
                fast_info={"lastPrice": 210.0},
                option_chain=lambda expiry: SimpleNamespace(
                    calls=_frame([q1], list(q1)), puts=_frame([q2], list(q2))
                ),
            )

        monkeypatch.setattr(mis.yf, "Ticker", make_ticker)
        rows = asyncio.run(
            mis.YFinanceMarketIntelSource().get_options_snapshot(
                ["O:AAPL261218C00200000", "O:AAPL261218P00200000"]
            )
        )
        assert calls == 1
        assert [r["session"]["last_updated"] for r in rows] == [None, None]
        assert [r["underlying_asset"]["price"] for r in rows] == [210.0, 210.0]
        assert mis._ms(pd.NaT) is None
        assert mis._ms(float("nan")) is None

    def test_resample_handles_none_high_low(self):
        bars = [
            {"time": 0, "open": 1.0, "high": None, "low": None, "close": 1.2, "volume": 1.0},
            {"time": 3600_000, "open": 1.2, "high": 2.0, "low": 1.1, "close": 1.9, "volume": 2.0},
        ]
        [out] = mis._resample(bars, hours=4)
        assert (out["high"], out["low"], out["close"], out["volume"]) == (2.0, 1.1, 1.9, 3.0)


class TestOptionsOhlcv:
    def test_history_columns_are_read_whatever_case_yahoo_used(self, ticker):
        stamps = pd.to_datetime(["2026-11-24 14:30:00-0500", "2026-11-24 15:30:00-0500"]).tz_convert("America/New_York")
        history = pd.DataFrame(
            {"Open": [1.0, 2.0], "High": [1.5, float("nan")], "Low": [0.9, 1.8], "Close": [1.2, 2.1], "Volume": [10, float("nan")]},
            index=stamps,
        )
        seen = {}

        def record(**kwargs):
            seen.update(kwargs)
            return history

        ticker(SimpleNamespace(history=record))

        bars = asyncio.run(mis.YFinanceMarketIntelSource().get_options_ohlcv("O:AAPL261218C00200000", "2026-11-01", "2026-11-25", "1hour"))

        assert seen["interval"] == "1h"
        # The end date is exclusive on Yahoo's side.
        assert seen["end"] == "2026-11-26"
        assert [bar["time"] for bar in bars] == [int(stamp.timestamp() * 1000) for stamp in stamps]
        assert [bar["close"] for bar in bars] == [1.2, 2.1]
        assert [bar["volume"] for bar in bars] == [10.0, 0.0]
        assert bars[1]["high"] is None

    def test_four_hour_bars_are_built_from_hourly_ones(self, ticker):
        stamps = pd.to_datetime(
            ["2026-11-24 13:30:00-0500", "2026-11-24 14:30:00-0500", "2026-11-24 15:30:00-0500", "2026-11-25 09:30:00-0500"]
        ).tz_convert("America/New_York")
        history = pd.DataFrame(
            {"Open": [1.0, 2.0, 3.0, 4.0], "High": [1.5, 2.5, 3.5, 4.5], "Low": [0.9, 1.9, 2.9, 3.9], "Close": [1.2, 2.2, 3.2, 4.2], "Volume": [1, 2, 3, 4]},
            index=stamps,
        )
        ticker(SimpleNamespace(history=lambda **kwargs: history))

        bars = asyncio.run(mis.YFinanceMarketIntelSource().get_options_ohlcv("AAPL261218C00200000", interval="4hour"))

        # Buckets are aligned to the UTC clock, so 13:30 and 14:30 New York share one, 15:30 gets the next.
        assert [
            (bar["open"], bar["close"], bar["high"], bar["low"], bar["volume"]) for bar in bars
        ] == [(1.0, 2.2, 2.5, 0.9, 3.0), (3.0, 3.2, 3.5, 2.9, 3.0), (4.0, 4.2, 4.5, 3.9, 4.0)]

    def test_an_interval_yahoo_has_no_bar_for_is_refused(self, ticker):
        ticker(SimpleNamespace(history=lambda **kwargs: pd.DataFrame()))
        with pytest.raises(IntervalNotSupported):
            asyncio.run(mis.YFinanceMarketIntelSource().get_options_ohlcv("AAPL261218C00200000", interval="2hour"))


class TestMovers:
    def test_times_are_unix_milliseconds(self, monkeypatch):
        from yfinance.screener import screener

        monkeypatch.setattr(
            screener,
            "screen",
            lambda query: {
                "quotes": [
                    {
                        "symbol": "AAPL",
                        "regularMarketPrice": 210.0,
                        "regularMarketChange": 2.1,
                        "regularMarketChangePercent": 1.01,
                        "regularMarketVolume": 1000,
                        "regularMarketTime": 1764083100,
                    },
                    {"symbol": "NOPRICE", "regularMarketPrice": float("nan")},
                ]
            },
        )

        rows = asyncio.run(mis.YFinanceMarketIntelSource().get_movers("gainers"))

        # Every reader divides a mover's time by 1000; seconds here would land in 1970.
        assert rows == [
            {
                "symbol": "AAPL",
                "price": 210.0,
                "change": 2.1,
                "change_percent": 1.01,
                "volume": 1000.0,
                "traded_value": 210000.0,
                "traded_at": 1764083100000,
            }
        ]

    def test_an_unknown_direction_or_a_failed_screen_is_empty(self, monkeypatch):
        source = mis.YFinanceMarketIntelSource()
        assert asyncio.run(source.get_movers("sideways")) == []

        from yfinance.screener import screener

        def fail(query):
            raise OSError("offline")

        monkeypatch.setattr(screener, "screen", fail)
        assert asyncio.run(source.get_movers("actives")) == []
