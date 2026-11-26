"""The Yahoo stand-ins' row shapes, with the ticker replaced by frames shaped like Yahoo's."""

from types import SimpleNamespace

import pandas as pd
import pytest
from datetime import date

from src.data_client.yfinance import free_fundamentals as ff


class FakeTicker:
    def __init__(self, **frames):
        for name, frame in frames.items():
            setattr(self, name, frame)


@pytest.fixture
def ticker(monkeypatch):
    def install(**frames):
        fake = FakeTicker(**frames)
        monkeypatch.setattr(ff, "yahoo_ticker", lambda symbol: fake)
        return fake

    return install


class TestEarningsReports:
    def test_past_rows_keep_actual_and_estimate_and_drop_future_ones(self, ticker):
        dates = pd.DataFrame(
            {"EPS Estimate": [10.1, 9.9, 9.0], "Reported EPS": [float("nan"), 10.2, 9.1], "Surprise(%)": [float("nan"), 3.0, 1.1]},
            index=pd.to_datetime(["2027-01-28", "2026-10-29", "2026-07-30"]),
        )
        ticker(earnings_dates=dates)

        rows = ff._earnings_reports("AAPL", 5)

        assert rows == [
            {"date": "2026-10-29", "epsActual": 10.2, "epsEstimated": 9.9, "revenueActual": None, "revenueEstimated": None},
            {"date": "2026-07-30", "epsActual": 9.1, "epsEstimated": 9.0, "revenueActual": None, "revenueEstimated": None},
        ]

    def test_an_empty_history_is_no_report(self, ticker):
        ticker(earnings_dates=pd.Series(name="Date", dtype=float))
        assert ff._earnings_reports("AAPL", 3) == []

    def test_an_unreadable_history_is_no_report(self, monkeypatch):
        def explode(symbol):
            raise TypeError("no history")

        monkeypatch.setattr(ff, "yahoo_ticker", explode)
        assert ff._earnings_reports("AAPL", 3) == []

    def test_each_report_is_placed_against_a_quarter_end(self, ticker, monkeypatch):
        dates = pd.DataFrame(
            {"EPS Estimate": [9.9], "Reported EPS": [10.2], "Surprise(%)": [3.0]},
            index=pd.to_datetime(["2026-10-29"]),
        )
        ticker(earnings_dates=dates)
        monkeypatch.setattr(
            ff,
            "_get_income_statements",
            lambda symbol, period, limit: [{"date": "2026-09-30"}, {"date": "2026-06-30"}],
        )

        rows = ff.earnings_surprises("AAPL", "quarter", 3)

        assert len(rows) == 1
        assert rows[0]["epsActual"] == 10.2
        assert rows[0]["fiscalDateEnding"] == "2026-09-30"


class TestEstimateDates:
    @pytest.mark.parametrize(
        ("step_months", "steps", "expected"),
        [(12, 0, "2027-09-30"), (12, 1, "2028-09-30"), (3, 0, "2026-12-31"), (3, 1, "2027-03-31")],
    )
    def test_the_period_end_steps_from_the_latest_statement(self, ticker, step_months, steps, expected):
        today = date(2026, 11, 26)
        ticker(
            income_stmt=SimpleNamespace(columns=[pd.Timestamp("2026-09-30"), pd.Timestamp("2025-09-30")]),
            quarterly_income_stmt=SimpleNamespace(columns=[pd.Timestamp("2026-09-30")]),
        )
        assert ff._ahead_end("AAPL", step_months, steps, today) == expected

    def test_no_statement_leaves_the_date_out(self, ticker):
        today = date(2026, 11, 26)
        ticker(income_stmt=None, quarterly_income_stmt=None)
        assert ff._ahead_end("AAPL", 12, 0, today) is None

    def test_estimates_carry_their_period_end(self, ticker, monkeypatch):
        monkeypatch.setattr(ff, "_ahead_end", lambda symbol, span, steps: {12: "2027-09-30", 3: "2026-12-31"}[span])
        monkeypatch.setattr(
            ff,
            "_estimates",
            lambda symbol: {
                "0y": {"date": None, "epsAvg": 9.4, "revenueAvg": 5.0},
                "0q": {"date": None, "epsAvg": 2.3, "revenueAvg": 1.0},
            },
        )
        ticker(income_stmt=SimpleNamespace(columns=[pd.Timestamp("2026-09-30")]),
               quarterly_income_stmt=SimpleNamespace(columns=[pd.Timestamp("2026-09-30")]))

        rows = ff.analyst_estimates("AAPL", "annual", 4)

        assert [(row["period"], row["date"], row["epsAvg"]) for row in rows] == [
            ("0y", "2027-09-30", 9.4),
            ("0q", "2026-12-31", 2.3),
        ]


class TestInsiderTrades:
    def test_yahoo_rows_become_filings_with_a_side_and_a_price(self, ticker):
        frame = pd.DataFrame(
            [
                {"Shares": 1000.0, "Value": 200000.0, "Text": "", "Insider": "Jane Doe", "Position": "CEO",
                 "Transaction": "Buy", "Start Date": pd.Timestamp("2026-11-03"), "Ownership": "Direct"},
                {"Shares": 500.0, "Value": None, "Text": "Sale", "Insider": "John Roe", "Position": "CFO",
                 "Transaction": None, "Start Date": pd.Timestamp("2026-11-04"), "Ownership": "Indirect"},
            ]
        )
        ticker(insider_transactions=frame)

        answer = ff.insider_trades("AAPL", 10)

        first, second = answer["trades"]
        assert (first["transactionDate"], first["securitiesTransacted"], first["price"], first["acquiredDisposed"], first["reportingName"]) == (
            "2026-11-04", 500.0, None, "D", "John Roe",
        )
        assert (second["transactionDate"], second["price"], second["acquiredDisposed"], second["reportingName"]) == (
            "2026-11-03", 200.0, "A", "Jane Doe",
        )
        assert answer["stats"] == [
            {
                "symbol": "AAPL",
                "year": 2026,
                "quarter": 4,
                "totalPurchases": 1,
                "totalSales": 1,
                "totalAcquired": 1000.0,
                "totalDisposed": 500.0,
            }
        ]

    def test_no_filings_is_no_trade_and_no_stats(self, ticker):
        ticker(insider_transactions=pd.DataFrame())
        assert ff.insider_trades("AAPL", 5) == {"trades": [], "stats": []}


class TestSharesFloatAndExecutives:
    def test_shares_float_includes_free_float_percentage(self, ticker):
        ticker(info={"floatShares": 800.0, "sharesOutstanding": 1000.0})
        rows = ff.shares_float("AAPL")
        assert len(rows) == 1
        assert rows[0]["freeFloat"] == 80.0
        assert rows[0]["floatShares"] == 800.0
        assert rows[0]["outstandingShares"] == 1000.0

    def test_key_executives_reads_info_once(self, monkeypatch):
        calls = 0

        def fake_ticker(symbol):
            nonlocal calls
            calls += 1
            return FakeTicker(
                info={
                    "financialCurrency": "USD",
                    "companyOfficers": [
                        {"name": "Alice", "title": "CEO", "totalPay": 1_000_000, "yearBorn": 1975},
                        {"name": "Bob", "title": "CFO", "totalPay": 800_000, "yearBorn": 1980},
                    ],
                }
            )

        monkeypatch.setattr(ff, "yahoo_ticker", fake_ticker)
        rows = ff.key_executives("AAPL")
        assert calls == 1
        assert [(r["name"], r["currencyPay"]) for r in rows] == [("Alice", "USD"), ("Bob", "USD")]


class TestDividendsAndSplits:
    def test_an_empty_series_is_no_distribution(self, ticker):
        ticker(dividends=pd.Series(name="Date", dtype=float), splits=pd.Series(name="Date", dtype=float))
        assert ff.dividends_and_splits("AAPL") == {"dividends": [], "splits": []}

    def test_history_becomes_rows_newest_first(self, ticker):
        index = pd.to_datetime(["2025-05-15", "2026-02-12"])
        ticker(dividends=pd.Series([0.25, 0.26], index=index, name="Date"),
               splits=pd.Series([4.0], index=pd.to_datetime(["2020-08-31"]), name="Date"))

        answer = ff.dividends_and_splits("AAPL")

        assert answer["dividends"] == [
            {"date": "2026-02-12", "dividend": 0.26, "adjDividend": 0.26},
            {"date": "2025-05-15", "dividend": 0.25, "adjDividend": 0.25},
        ]
        assert answer["splits"] == [{"date": "2020-08-31", "numerator": 4.0, "denominator": 1, "split": 4.0}]


class TestQuarter:
    @pytest.mark.parametrize(("day", "expected"), [("2026-03-31", "Q1"), ("2026-06-30", "Q2"), ("2026-09-27", "Q3"), ("2025-12-31", "Q4"), (None, None)])
    def test_calendar_quarters(self, day, expected):
        assert ff._quarter(day) == expected


class TestStatementsShapeAndDates:
    def test_single_statement_returns_list_and_all_returns_dict(self, ticker, monkeypatch):
        monkeypatch.setattr(ff, "_get_income_statements", lambda s, p, l: [{"date": "2025-09-30", "revenue": 100}])
        monkeypatch.setattr(ff, "_get_cash_flows", lambda s, p, l: [{"date": "2025-09-30", "operatingCashFlow": 50}])
        bal = pd.DataFrame({"2025-09-30": [200]}, index=["Total Assets"])
        ticker(balance_sheet=bal, quarterly_balance_sheet=bal)

        assert isinstance(ff.statements("AAPL", "income", "annual", 4), list)
        assert isinstance(ff.statements("AAPL", "balance", "annual", 4), list)
        assert isinstance(ff.statements("AAPL", "cash", "annual", 4), list)
        both = ff.statements("AAPL", "all", "annual", 4)
        assert set(both) == {"income_statement", "balance_sheet", "cash_flow"}

    def test_day_guards_nat_and_nan(self):
        assert ff._day(pd.NaT) is None
        assert ff._day(float("nan")) is None
        assert ff._day(float("inf")) is None
