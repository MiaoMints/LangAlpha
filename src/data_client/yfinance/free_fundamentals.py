"""The fundamentals tools' payloads from Yahoo Finance, in FMP's row shapes.

Each reader answers with what the matching FMP client call would have returned,
so a tool that lost its FMP key envelopes Yahoo's rows exactly where FMP's
stood. Yahoo publishes less than FMP: a single trailing-twelve-months ratio
row instead of one per period, one current price-target consensus, no DCF.
What Yahoo does not have stays absent rather than guessed.
"""

from __future__ import annotations

import calendar
import logging
import math
from datetime import date, datetime, timezone
from typing import Any

from ..fmp.analyst import (
    price_targets,
    rating_actions,
    rating_counts,
    surprise_rows,
    target_consensus,
)
from .financial_source import (
    _clean_value,
    _dataframe_to_records,
    _get_cash_flows,
    _get_financial_ratios,
    _get_income_statements,
    _get_key_metrics,
)
from .yahoo import yahoo_ticker

logger = logging.getLogger(__name__)

Row = dict[str, Any]

#: Yahoo balance-sheet line -> FMP's name; the rest turn camelCase.
_BALANCE = {
    "total_assets": "totalAssets",
    "total_liabilities_net_minority_interest": "totalLiabilities",
    "stockholders_equity": "stockholdersEquity",
    "current_assets": "totalCurrentAssets",
    "current_liabilities": "totalCurrentLiabilities",
    "cash_and_equivalents": "cashAndCashEquivalents",
    "goodwill_and_intangible_assets": "goodwillAndIntangibleAssets",
    "retained_earnings": "retainedEarnings",
    "working_capital": "workingCapital",
    "tangible_asset_value": "tangibleAssetValue",
    "invested_capital": "investedCapital",
}

#: Income line -> FMP's growth metric, both FMP growth endpoints name alike.
_GROWTH = {
    "revenue": "revenueGrowth",
    "costOfRevenue": "costOfRevenueGrowth",
    "grossProfit": "grossProfitGrowth",
    "operatingIncome": "operatingIncomeGrowth",
    "netIncome": "netIncomeGrowth",
    "epsdiluted": "epsdilutedGrowth",
    "basicEps": "epsGrowth",
    "researchAndDevelopmentExpenses": "researchAndDevelopmentExpenseGrowth",
}

#: The estimate periods in FMP's order: this year, next year, then the quarters.
_ESTIMATE_PERIODS = ("0y", "+1y", "0q", "+1q")

_RATING_COUNTS = {
    "strongBuy": "analystRatingsStrongBuy",
    "buy": "analystRatingsBuy",
    "hold": "analystRatingsHold",
    "sell": "analystRatingsSell",
    "strongSell": "analystRatingsStrongSell",
}


def _camel(name: str) -> str:
    head, *rest = str(name).split("_")
    return head + "".join(part.capitalize() for part in rest)


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def _day(cell: Any) -> str | None:
    """A date cell as ``YYYY-MM-DD``."""
    if cell is None:
        return None
    if isinstance(cell, str):
        return cell[:10] if cell else None
    if isinstance(cell, (int, float)) and not isinstance(cell, bool):
        if not math.isfinite(cell):
            return None
        return datetime.fromtimestamp(cell, tz=timezone.utc).strftime("%Y-%m-%d")
    to_pydatetime = getattr(cell, "to_pydatetime", None)
    if callable(to_pydatetime):
        try:
            cell = to_pydatetime()
        except (TypeError, ValueError):
            return None
    if isinstance(cell, (datetime, date)):
        try:
            return cell.strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            return None
    return None


def _info(ticker: Any) -> dict[str, Any]:
    try:
        info = ticker.info
    except Exception as exc:  # noqa: BLE001 (a missing fact is an absent key)
        logger.debug("yfinance.info failed: %s", type(exc).__name__)
        return {}
    return info if isinstance(info, dict) else {}


def _statement(frame: Any, limit: int, key_map: dict[str, str] | None = None) -> list[Row]:
    """A dated statement frame as rows, newest period first, FMP-named."""
    rows = _dataframe_to_records(frame)
    rows.sort(key=lambda row: str(row.get("date") or ""), reverse=True)
    out = []
    for row in rows[: max(int(limit), 0)]:
        shaped = {_camel(key_map.get(str(key), key)) if key_map else _camel(str(key)): value for key, value in row.items()}
        out.append({key: _clean_value(value) for key, value in shaped.items()})
    return out


def statements(symbol: str, statement_type: str, period: str, limit: int) -> list[Row] | dict[str, list[Row]]:
    """Period rows for one statement, or ``{income_statement, balance_sheet, cash_flow}`` for ``"all"``."""
    if statement_type == "income":
        return _get_income_statements(symbol, period, limit)
    if statement_type == "balance":
        ticker = yahoo_ticker(symbol)
        frame = ticker.quarterly_balance_sheet if period == "quarter" else ticker.balance_sheet
        return _statement(frame, limit, _BALANCE)
    if statement_type == "cash":
        return _get_cash_flows(symbol, period, limit)
    ticker = yahoo_ticker(symbol)
    frame = ticker.quarterly_balance_sheet if period == "quarter" else ticker.balance_sheet
    return {
        "income_statement": _get_income_statements(symbol, period, limit),
        "balance_sheet": _statement(frame, limit, _BALANCE),
        "cash_flow": _get_cash_flows(symbol, period, limit),
    }


def ratios(symbol: str, period: str, limit: int) -> dict[str, list[Row]]:
    """``{key_metrics, ratios}``: one trailing-twelve-months row each."""
    most = max(int(limit), 0)
    return {"key_metrics": _get_key_metrics(symbol)[:most], "ratios": _get_financial_ratios(symbol)[:most]}


def growth(symbol: str, period: str, limit: str | int) -> dict[str, list[Row]]:
    """Year-on-year growth of each income line, as both FMP growth endpoints name it."""
    step = 4 if period == "quarter" else 1
    most = max(int(limit), 0)
    rows = _get_income_statements(symbol, period, (most + 1) * step)
    out = []
    for index, row in enumerate(rows):
        base = rows[index + step] if index + step < len(rows) else {}
        grown = {}
        for line, metric in _GROWTH.items():
            now, then = _num(row.get(line)), _num(base.get(line))
            if now is not None and then:
                grown[metric] = now / then - 1
        if grown:
            out.append({"date": row.get("date"), "reportedCurrency": row.get("reportedCurrency"), **grown})
        if len(out) >= most:
            break
    return {"financial_growth": out, "income_statement_growth": out}


def valuation(symbol: str, period: str, limit: str | int) -> dict[str, list[Row]]:
    """``{current_dcf, historical_dcf, enterprise_value}``: Yahoo has no DCF."""
    info = _info(yahoo_ticker(symbol))
    value = _num(info.get("enterpriseValue"))
    cap, debt, cash = _num(info.get("marketCap")), _num(info.get("totalDebt")), _num(info.get("totalCash"))
    if value is None and cap is not None:
        value = cap + (debt or 0.0) - (cash or 0.0)
    rows = (
        [
            {
                "symbol": symbol,
                "date": date.today().isoformat(),
                "enterpriseValue": value,
                "marketCap": cap,
                "totalDebt": debt,
                "totalCash": cash,
                "reportedCurrency": info.get("financialCurrency"),
            }
        ]
        if value is not None
        else []
    )
    return {"current_dcf": [], "historical_dcf": [], "enterprise_value": rows[: max(int(limit), 0)]}


def _target_rows(symbol: str) -> list[Row]:
    """The one consensus row in the shape ``target_consensus`` reads."""
    try:
        targets = yahoo_ticker(symbol).analyst_price_targets
    except Exception as exc:  # noqa: BLE001 (no consensus is an empty list)
        logger.warning("yfinance.analyst_price_targets failed: %s", type(exc).__name__)
        return []
    if not isinstance(targets, dict) or not targets:
        return []
    return [
        {
            "targetHigh": targets.get("high"),
            "targetLow": targets.get("low"),
            "targetMedian": targets.get("median"),
            "targetConsensus": targets.get("mean"),
        }
    ]


def _rating_rows(symbol: str) -> list[Row]:
    """Recommendation counts per month, latest first, in the shape ``rating_counts`` reads."""
    try:
        frame = yahoo_ticker(symbol).recommendations_summary
    except Exception as exc:  # noqa: BLE001 (no counts is an empty list)
        logger.warning("yfinance.recommendations_summary failed: %s", type(exc).__name__)
        return []
    if frame is None or not hasattr(frame, "to_dict") or frame.empty:
        return []
    today = date.today()
    out = []
    for label, cell in frame.to_dict(orient="index").items():
        text = str(label).strip()
        months = int(text[:-1]) if text[:-1].lstrip("+-").isdigit() else 0
        month = today.year * 12 + (today.month - 1) - months
        year, month0 = divmod(month, 12)
        out.append(
            {
                "date": f"{year:04d}-{month0 + 1:02d}",
                **{name: _clean_value(cell.get(key)) for key, name in _RATING_COUNTS.items()},
            }
        )
    return out


def analyst_ratings(symbol: str, limit: str | int, places: int, tz: str | None) -> dict[str, list[Row]]:
    """``{target_consensus, rating_counts, rating_actions, price_targets}``."""
    from ..analyst_grades import yahoo_price_target_news, yahoo_stock_grades

    most = max(int(limit), 0)
    return {
        "target_consensus": target_consensus(_target_rows(symbol), places),
        "rating_counts": rating_counts(_rating_rows(symbol))[:most],
        "rating_actions": rating_actions(yahoo_stock_grades(symbol, most)),
        "price_targets": price_targets(yahoo_price_target_news(symbol, most), places, tz),
    }


def _estimates(symbol: str) -> dict[str, list[Row]]:
    """EPS and revenue estimates per forward period, keyed by the period label."""
    ticker = yahoo_ticker(symbol)
    tables: dict[str, Any] = {}
    for metric, attribute in (("eps", "earnings_estimate"), ("revenue", "revenue_estimate")):
        try:
            frame = getattr(ticker, attribute)
        except Exception as exc:  # noqa: BLE001 (an unestimated metric stays absent)
            logger.warning("yfinance.%s failed: %s", attribute, type(exc).__name__)
            frame = None
        tables[metric] = frame if frame is not None and hasattr(frame, "to_dict") and not frame.empty else None

    def cells(metric: str, label: str) -> dict[str, Any]:
        frame = tables[metric]
        if frame is None or label not in frame.index:
            return {}
        cell = frame.loc[label]
        if isinstance(cell, dict):  # a one-row frame turned records
            return cell
        return cell.to_dict() if hasattr(cell, "to_dict") else {}

    def pick(cell: dict[str, Any], *names: str) -> Any:
        lowered = {str(key).lower(): value for key, value in cell.items()}
        for name in names:
            for key, value in lowered.items():
                if key.startswith(name):
                    return value
        return None

    out = {}
    for label in _ESTIMATE_PERIODS:
        eps, revenue = cells("eps", label), cells("revenue", label)
        if not eps and not revenue:
            continue
        out[label] = {
            "date": _day(pick(eps or revenue, "period", "fiscal", "date", "end")),
            "epsAvg": pick(eps, "avg", "mean"),
            "epsLow": pick(eps, "low"),
            "epsHigh": pick(eps, "high"),
            "numAnalystsEps": pick(eps, "numberofanalysts", "analysts", "count"),
            "revenueAvg": pick(revenue, "avg", "mean"),
            "revenueLow": pick(revenue, "low"),
            "revenueHigh": pick(revenue, "high"),
            "numAnalystsRevenue": pick(revenue, "numberofanalysts", "analysts", "count"),
        }
    return out


def analyst_estimates(symbol: str, period: str, limit: str | int) -> list[Row]:
    """Estimate rows for the forward periods, years first for an annual ask."""
    rows = _estimates(symbol)
    order = ("0y", "+1y", "0q", "+1q") if period != "quarter" else ("0q", "+1q", "0y", "+1y")
    out = []
    for label in order:
        row = rows.get(label)
        if row is not None:
            span = 3 if label.endswith("q") else 12
            steps = 1 if label.startswith("+") else 0
            out.append({**row, "period": label, "date": row.get("date") or _ahead_end(symbol, span, steps)})
        if len(out) >= max(int(limit), 0):
            break
    return out


def _earnings_reports(symbol: str, limit: int) -> list[Row]:
    """Past reports with the actual and estimated EPS Yahoo dates (no revenue)."""
    most = max(int(limit), 0)
    try:
        dates = yahoo_ticker(symbol).earnings_dates
    except Exception as exc:  # noqa: BLE001 (no history is an empty list)
        logger.warning("yfinance.earnings_dates failed: %s", type(exc).__name__)
        return []
    if dates is None or not hasattr(dates, "empty") or dates.empty:
        return []
    out = []
    for stamp, row in dates.head(most + 4).iterrows():
        actual = _num(row.get("Reported EPS"))
        if actual is None:  # a future date carries estimates only
            continue
        out.append(
            {
                "date": _day(stamp),
                "epsActual": actual,
                "epsEstimated": _num(row.get("EPS Estimate")),
                "revenueActual": None,
                "revenueEstimated": None,
            }
        )
    return [row for row in out if row["date"]][:most]


def _quarter(day: str | None) -> str | None:
    """The fiscal quarter label FMP gives a statement period end."""
    try:
        return f"Q{(int(str(day)[5:7]) - 1) // 3 + 1}"
    except (TypeError, ValueError):
        return None


def _ahead_end(symbol: str, step_months: int, steps: int, today: date | None = None) -> str | None:
    """The period end ``steps`` periods past the one in progress.

    Yahoo's ``0y``/``0q`` is the period still running, so the walk starts past
    the latest statement and stops at the first end that has not passed yet.
    """
    frame = getattr(yahoo_ticker(symbol), "quarterly_income_stmt" if step_months < 12 else "income_stmt", None)
    try:
        ends = [datetime(month=cell.month, day=cell.day, year=cell.year).date() for cell in frame.columns]
    except Exception:  # noqa: BLE001 (no statement leaves the date out)
        ends = []
    if not ends:
        return None
    last, today = max(ends), today or date.today()
    count = 0
    while True:  # the period still running is the first end after today
        end = _month_end(last, step_months * count)
        count += 1
        if end > today:
            break
    return _month_end(last, step_months * (count - 1 + steps)).isoformat()


def _month_end(statement: date, step_months: int) -> date:
    """The same day of the month ``step_months`` later, kept to a month end."""
    ordinal = statement.month - 1 + step_months
    year, month = statement.year + ordinal // 12, ordinal % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    if statement.day == calendar.monthrange(statement.year, statement.month)[1]:
        return date(year, month, last_day)
    return date(year, month, min(statement.day, last_day))


def _word(*values: Any) -> str:
    """The first cell that holds text: Yahoo leaves blanks as NaN."""
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _series_items(series: Any) -> list[tuple[Any, Any]]:
    try:
        return list(series.items()) if series is not None else []
    except Exception:  # noqa: BLE001 (an unreadable history is an empty one)
        return []


def earnings_surprises(symbol: str, period: str, limit: str | int) -> list[Row]:
    """Earnings reports, each placed against its period end as FMP's are."""
    most = max(int(limit), 0)
    reports = _earnings_reports(symbol, most)
    statements = [
        {"date": row.get("date"), "period": _quarter(row.get("date"))}
        for row in _get_income_statements(symbol, "quarter", most + 2)
    ]
    rows = surprise_rows(reports, statements)
    return rows[:most]


def insider_trades(symbol: str, limit: str | int) -> dict[str, list[Row]]:
    """``{trades, stats}``: the filings Yahoo dates, and their running totals."""
    ticker = yahoo_ticker(symbol)
    try:
        frame = ticker.insider_transactions
    except Exception as exc:  # noqa: BLE001 (no filings is an empty answer)
        logger.warning("yfinance.insider_transactions failed: %s", type(exc).__name__)
        frame = None
    records = frame.to_dict("records") if frame is not None and hasattr(frame, "empty") and not frame.empty else []
    trades = []
    for row in records:
        shares, value = _num(row.get("Shares")), _num(row.get("Value"))
        kind = _word(row.get("Transaction"), row.get("Text")).lower()
        if kind.startswith(("buy", "acqui", "purchase")):
            side = "A"
        elif kind.startswith(("sale", "sell", "dispos")):
            side = "D"
        else:
            side = "N"
        trades.append(
            {
                "symbol": symbol,
                "transactionDate": _day(row.get("Start Date")),
                "filingDate": None,
                "reportingName": row.get("Insider"),
                "transactionType": _word(row.get("Transaction"), row.get("Text")) or None,
                "securitiesTransacted": shares,
                "price": value / shares if value is not None and shares else None,
                "securityAdpTip": row.get("Ownership"),
                "acquiredDisposed": side,
                "insider": row.get("Insider"),
                "position": row.get("Position"),
            }
        )
    trades.sort(key=lambda row: str(row["transactionDate"] or ""), reverse=True)
    trades = trades[: max(int(limit), 0)]
    quarterly: dict[tuple[int, int], dict[str, Any]] = {}
    for row in trades:
        day = row.get("transactionDate")
        if not day or len(str(day)) < 7:
            continue
        try:
            year = int(str(day)[:4])
            quarter = (int(str(day)[5:7]) - 1) // 3 + 1
        except (TypeError, ValueError):
            continue
        bucket = quarterly.setdefault(
            (year, quarter),
            {
                "symbol": symbol,
                "year": year,
                "quarter": quarter,
                "totalPurchases": 0,
                "totalSales": 0,
                "totalAcquired": 0.0,
                "totalDisposed": 0.0,
            },
        )
        side = str(row.get("acquiredDisposed") or "").upper()
        shares = float(row.get("securitiesTransacted") or 0.0)
        if side == "A":
            bucket["totalPurchases"] += 1
            bucket["totalAcquired"] += shares
        elif side == "D":
            bucket["totalSales"] += 1
            bucket["totalDisposed"] += shares
    stats = [quarterly[key] for key in sorted(quarterly, reverse=True)]
    return {"trades": trades, "stats": stats}


def dividends_and_splits(symbol: str) -> dict[str, list[Row]]:
    """``{dividends, splits}`` from the listing's own history."""
    ticker = yahoo_ticker(symbol)
    dividends, splits = [], []
    try:
        for stamp, amount in _series_items(ticker.dividends):
            dividends.append({"date": _day(stamp), "dividend": _num(amount), "adjDividend": _num(amount)})
        for stamp, ratio in _series_items(ticker.splits):
            value = _num(ratio)
            if value:
                splits.append({"date": _day(stamp), "numerator": value, "denominator": 1, "split": value})
    except Exception as exc:  # noqa: BLE001 (no history is an empty answer)
        logger.warning("yfinance.dividends/splits failed: %s", type(exc).__name__)
    dividends.sort(key=lambda row: str(row["date"]), reverse=True)
    splits.sort(key=lambda row: str(row["date"]), reverse=True)
    return {"dividends": dividends, "splits": splits}


def shares_float(symbol: str) -> list[Row]:
    """The float and the shares out, dated the day they were read."""
    info = _info(yahoo_ticker(symbol))
    free = _num(info.get("floatShares"))
    listed = _num(info.get("sharesOutstanding")) or _num(info.get("impliedSharesOutstanding"))
    if free is None and listed is None:
        return []
    pct = round(free / listed * 100.0, 2) if free is not None and listed else None
    return [
        {
            "symbol": symbol,
            "date": date.today().isoformat(),
            "freeFloat": pct,
            "floatShares": free,
            "outstandingShares": listed,
        }
    ]


def key_executives(symbol: str) -> list[Row]:
    """The officers Yahoo lists, in FMP's key-executives row shape."""
    info = _info(yahoo_ticker(symbol))
    officers = info.get("companyOfficers") or []
    default_currency = info.get("financialCurrency")
    out = []
    for officer in officers:
        if isinstance(officer, dict):
            out.append(
                {
                    "name": officer.get("name"),
                    "title": officer.get("title"),
                    "pay": _num(officer.get("totalPay")),
                    "currencyPay": officer.get("currencyPay") or default_currency,
                    "gender": officer.get("gender"),
                    "yearBorn": _num(officer.get("yearBorn")),
                    "titleSince": officer.get("titleSince"),
                }
            )
    return out


def read(data_type: str, symbol: str, asked: dict[str, Any]) -> Any:
    """Dispatch on a tool's ``data_type``; ``asked`` holds its arguments."""
    limit = asked.get("limit")
    most = limit if isinstance(limit, int) else {"analyst_ratings": 50, "analyst_estimates": 10, "earnings_surprises": 12, "insider_trades": 50}.get(data_type, 10)
    if data_type == "financial_statements":
        return statements(symbol, asked.get("statement_type", "all"), asked.get("period", "annual"), most)
    if data_type == "financial_ratios":
        return ratios(symbol, asked.get("period", "annual"), most)
    if data_type == "growth_metrics":
        return growth(symbol, asked.get("period", "annual"), most)
    if data_type == "historical_valuation":
        return valuation(symbol, asked.get("period", "annual"), most)
    if data_type == "analyst_ratings":
        places = int(asked.get("places", 0) or 0)
        return analyst_ratings(symbol, most, places, asked.get("tz"))
    if data_type == "analyst_estimates":
        return analyst_estimates(symbol, asked.get("period", "annual"), most)
    if data_type == "earnings_surprises":
        return earnings_surprises(symbol, asked.get("period", "quarter"), most)
    if data_type == "insider_trades":
        return insider_trades(symbol, most)
    if data_type == "dividends_and_splits":
        return dividends_and_splits(symbol)
    if data_type == "shares_float":
        return shares_float(symbol)
    if data_type == "key_executives":
        return key_executives(symbol)
    raise ValueError(f"No Yahoo stand-in for {data_type}")
