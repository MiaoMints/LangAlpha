"""ROBAT Control MCP Server.

Reads and writes the trading bot's own console API (the ROBAT service on the
loopback interface). Every response is wrapped in the standard market-data
envelope (see mcp_servers/AGENT_CONTRACT.md): `data` is the one payload key,
`count` says how many records it holds, `source` names the upstream.
"""

from __future__ import annotations

try:
    from _bootstrap import MCPServer  # script launch: this bundle is sys.path[0]
except ModuleNotFoundError:  # imported as a package module (tests)
    from mcp_servers._bootstrap import MCPServer

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from mcp_servers._envelope import make_error, make_response
from mcp_servers._schemas import ANY, OBJECT, RECORDS, STR, envelope_schema, output_model

SOURCE = "robat"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
_TIMEOUT_SECONDS = 30.0

mcp = MCPServer("RobatControlMCP")

# The bot's list endpoints answer with a pagination envelope, not a bare array:
# {page, page_size, total_count, total_pages, items}. Declaring RECORDS made the
# SDK reject every list call, so the page shape is declared once instead.
PAGE = {
    "type": "object",
    "properties": {
        "page": {"type": "integer"},
        "page_size": {"type": "integer"},
        "total_count": {"type": "integer"},
        "total_pages": {"type": "integer"},
        "items": {"type": "array", "items": {"type": "object"}},
    },
}



# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def _base_url() -> str:
    """Console API root; the loopback service unless the host says otherwise."""
    return (os.getenv("ROBAT_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def _token() -> str | None:
    """The X-Token shared secret, straight from the host environment."""
    value = (os.getenv("ROBAT_API_TOKEN") or "").strip()
    return value or None


def _safe_detail(action: str, exc: Exception) -> str:
    """Sanitized failure text — never the raw exception, which can carry the URL."""
    return f"{action} failed: {type(exc).__name__}"


def _call(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One request to the console API, returned as ``(ok, payload)``-free envelope data.

    Returns either the decoded JSON body or an error envelope; callers pass the
    result straight through so a failure never raises into the agent.
    """
    token = _token()
    if token is None:
        return make_error(
            "client_unavailable",
            "ROBAT_API_TOKEN is not set in the host environment; the console API "
            "needs it as the X-Token header.",
        )

    url = f"{_base_url()}{path}"
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url = f"{url}?{urllib.parse.urlencode(clean)}"

    data = None
    headers = {"X-Token": token, "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = _read_error_detail(exc)
        code = {401: "auth_failed", 403: "auth_failed", 404: "not_found", 429: "rate_limited"}.get(
            exc.code, "invalid_argument" if 400 <= exc.code < 500 else "upstream_error"
        )
        return make_error(code, detail, status=exc.code)
    except urllib.error.URLError as exc:
        return make_error(
            "client_unavailable",
            f"console API unreachable at {_base_url()}: {type(exc.reason).__name__}",
        )
    except Exception as exc:  # noqa: BLE001
        return make_error("upstream_error", _safe_detail("Console request", exc))

    if not raw.strip():
        return make_response(None, source=SOURCE, count=0)
    try:
        return {"_body": json.loads(raw)}
    except json.JSONDecodeError:
        return {"_body": raw}


def _read_error_detail(exc: urllib.error.HTTPError) -> str:
    """FastAPI's own detail string when it sent one, else a bare status line."""
    try:
        payload = json.loads(exc.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return f"console API returned HTTP {exc.code}"
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, str) and detail.strip():
        return detail
    if isinstance(detail, list):
        return f"request rejected: HTTP {exc.code}"
    return f"console API returned HTTP {exc.code}"


def _envelope(
    result: dict[str, Any],
    *,
    count: int | None = None,
    **echo: Any,
) -> dict[str, Any]:
    """Turn ``_call``'s transport result into the published success envelope."""
    if "error" in result and "_body" not in result:
        return result
    payload = result.get("_body")
    if isinstance(payload, list):
        return make_response(payload, source=SOURCE, count=len(payload) if count is None else count, **echo)
    if isinstance(payload, dict):
        records = payload.get("items")
        if isinstance(records, list) and count is None:
            count = len(records)
        return make_response(payload, source=SOURCE, count=count if count is not None else 1, **echo)
    return make_response(payload, source=SOURCE, count=count if count is not None else 0, **echo)


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

_OUT_ACTIVE_POSITIONS = output_model("RobatActivePositions", envelope_schema(PAGE))


@mcp.tool()
def list_active_positions(page: int = 1, page_size: int = 20) -> _OUT_ACTIVE_POSITIONS:
    """Open positions the bot is tracking right now, one record per trade state.
    Use before answering anything about current exposure, stops, or target levels.

    Args:
        page: 1-based page number.
        page_size: Records per page, 1-50.

    Returns:
        dict: {count, data, page, total_count, source}. data is {page, page_size,
        total_count, total_pages, items}; each item is a trade summary carrying
        symbol, side, trader_id, entry and stop prices, take-profit levels, state
        and the lot ledger. On error: {error, detail}.
    """
    result = _call("GET", "/api/positions/active", params={"page": page, "page_size": page_size})
    if "error" in result:
        return result
    body = result.get("_body") or {}
    return _envelope(
        result,
        total_count=body.get("total_count") if isinstance(body, dict) else None,
        page=body.get("page") if isinstance(body, dict) else page,
    )


_OUT_CLOSED_POSITIONS = output_model("RobatClosedPositions", envelope_schema(PAGE))


@mcp.tool()
def list_closed_positions(
    page: int = 1,
    page_size: int = 20,
    trading_day: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> _OUT_CLOSED_POSITIONS:
    """Closed trades, newest first — the record for realised P&L and win-rate work.
    Filter by one trading day or by an inclusive date range, not both.

    Args:
        page: 1-based page number.
        page_size: Records per page, 1-50.
        trading_day: Single day, "YYYY-MM-DD".
        date_from: Range start, "YYYY-MM-DD".
        date_to: Range end, "YYYY-MM-DD".

    Returns:
        dict: {count, data, page, total_count, source}. data is {page, page_size,
        total_count, total_pages, items}; each item is a closed trade with exit
        price, realised P&L, and the closing reason. On error: {error, detail}.
    """
    result = _call(
        "GET",
        "/api/positions/closed",
        params={
            "page": page,
            "page_size": page_size,
            "trading_day": trading_day,
            "date_from": date_from,
            "date_to": date_to,
        },
    )
    if "error" in result:
        return result
    body = result.get("_body") or {}
    return _envelope(
        result,
        total_count=body.get("total_count") if isinstance(body, dict) else None,
        page=body.get("page") if isinstance(body, dict) else page,
    )


_OUT_POSITION_DETAIL = output_model("RobatPositionDetail", envelope_schema(OBJECT))


@mcp.tool()
def get_position_detail(position_key: str) -> _OUT_POSITION_DETAIL:
    """Full ledger for one position: every lot, entry fill, stop, and TP tier.
    Use when a summary is not enough — e.g. to explain how quantity was built.

    Args:
        position_key: The position or trade key from list_active_positions.

    Returns:
        dict: {count, data, source}. data is the position record: trade state,
        lot ledger, entry plan, stop and take-profit book, execution history.
        On error: {error, detail} with not_found for an unknown key.
    """
    result = _call("GET", f"/api/positions/{urllib.parse.quote(position_key, safe='')}/detail")
    return _envelope(result)


_OUT_OPEN_ORDERS = output_model("RobatOpenOrders", envelope_schema(PAGE))


@mcp.tool()
def list_open_orders(symbol: str | None = None) -> _OUT_OPEN_ORDERS:
    """Orders still live on the exchange. Use to check what is working and at what price.

    Args:
        symbol: Exchange symbol like "BTCUSDT"; omit for every symbol.

    Returns:
        dict: {count, data, source}. data is a list of order records with symbol,
        side, type, price, quantity, reduce_only and client order id. On error:
        {error, detail}.
    """
    return _envelope(_call("GET", "/api/orders/open", params={"symbol": symbol}))


_OUT_ORDER_HISTORY = output_model("RobatOrderHistory", envelope_schema(PAGE))


@mcp.tool()
def list_order_history(
    page: int = 1,
    page_size: int = 20,
    symbol: str | None = None,
) -> _OUT_ORDER_HISTORY:
    """Past orders, newest first. Use for a fill timeline or to prove an order went out.

    Args:
        page: 1-based page number.
        page_size: Records per page, 1-50.
        symbol: Exchange symbol like "BTCUSDT"; omit for every symbol.

    Returns:
        dict: {count, data, source}. data is a list of historical order records
        with status, filled quantity, average price and timestamps. On error:
        {error, detail}.
    """
    result = _call(
        "GET",
        "/api/orders/history",
        params={"page": page, "page_size": page_size, "symbol": symbol},
    )
    return _envelope(result)


_OUT_FILLS = output_model("RobatFills", envelope_schema(PAGE))


@mcp.tool()
def list_fills(
    page: int = 1,
    page_size: int = 20,
    symbol: str | None = None,
    trader_id: str | None = None,
) -> _OUT_FILLS:
    """Executed fills, newest first — the ground truth for what the exchange actually did.

    Args:
        page: 1-based page number.
        page_size: Records per page, 1-50.
        symbol: Exchange symbol like "BTCUSDT"; omit for every symbol.
        trader_id: Restrict to one trader's fills.

    Returns:
        dict: {count, data, source}. data is a list of fill records with price,
        quantity, fee, side and fill time. On error: {error, detail}.
    """
    result = _call(
        "GET",
        "/api/fills",
        params={"page": page, "page_size": page_size, "symbol": symbol, "trader_id": trader_id},
    )
    return _envelope(result)


_OUT_SIGNALS = output_model("RobatSignals", envelope_schema(PAGE))


@mcp.tool()
def list_signals(
    page: int = 1,
    page_size: int = 20,
    trader_id: str | None = None,
    symbol: str | None = None,
) -> _OUT_SIGNALS:
    """Inbound signals the bot parsed from Discord/Telegram, newest first.
    Use to explain why a trade exists or to inspect a missed message.

    Args:
        page: 1-based page number.
        page_size: Records per page, 1-50.
        trader_id: Restrict to one trader.
        symbol: Restrict to one symbol.

    Returns:
        dict: {count, data, source}. data is a list of signal records with source
        text, parsed intent, and the decision the bot reached. On error: {error, detail}.
    """
    result = _call(
        "GET",
        "/api/signals/feed",
        params={"page": page, "page_size": page_size, "trader_id": trader_id, "symbol": symbol},
    )
    return _envelope(result)


_OUT_ALERTS = output_model("RobatAlerts", envelope_schema(ANY))


@mcp.tool()
def list_active_alerts() -> _OUT_ALERTS:
    """Alerts currently firing — reconciliation mismatches, risk breaches, dead letters.
    Check this before blaming a strategy for a bad position.

    Returns:
        dict: {count, data, source}. data is a list of alert records with kind,
        severity, message and the scope it applies to. On error: {error, detail}.
    """
    return _envelope(_call("GET", "/api/alerts/active"))


_OUT_RISK = output_model("RobatRiskView", envelope_schema(ANY))


@mcp.tool()
def get_risk_view(trading_day: str | None = None) -> _OUT_RISK:
    """Daily risk posture: loss budget used, per-trader state, and every halt reason.

    Args:
        trading_day: Day as "YYYY-MM-DD"; omit for the current trading day.

    Returns:
        dict: {count, data, source}. data holds the daily risk payload and the
        per-trader control records. On error: {error, detail}.
    """
    return _envelope(_call("GET", "/risk/daily", params={"trading_day": trading_day}))


_OUT_TRADERS = output_model("RobatTraders", envelope_schema(ANY))


@mcp.tool()
def list_traders_risk() -> _OUT_TRADERS:
    """Every trader with its risk control record: enabled/halted, loss streak, limits.

    Returns:
        dict: {count, data, source}. data is a list of per-trader risk records.
        On error: {error, detail}.
    """
    return _envelope(_call("GET", "/risk/traders"))


_OUT_ACCOUNTS = output_model("RobatAccounts", envelope_schema(ANY))


@mcp.tool()
def list_accounts() -> _OUT_ACCOUNTS:
    """Exchange accounts the bot trades, with their account keys and balances.

    Returns:
        dict: {count, data, source}. data is the account list payload. On error:
        {error, detail}.
    """
    return _envelope(_call("GET", "/api/accounts"))


_OUT_DASHBOARD = output_model("RobatDashboard", envelope_schema(ANY))


@mcp.tool()
def get_dashboard(trading_day: str | None = None) -> _OUT_DASHBOARD:
    """Whole-desk snapshot: equity, open exposure, today's P&L, and trade counts.
    Use this first for a broad "how are we doing" question.

    Args:
        trading_day: Day as "YYYY-MM-DD"; omit for the current trading day.

    Returns:
        dict: {count, data, source}. data is the dashboard payload. On error:
        {error, detail}.
    """
    return _envelope(_call("GET", "/api/dashboard", params={"trading_day": trading_day}))


_OUT_RECON = output_model("RobatReconciliation", envelope_schema(PAGE))


@mcp.tool()
def list_reconciliation_logs(page: int = 1, page_size: int = 20) -> _OUT_RECON:
    """Position reconciliation runs — where the local ledger and the exchange disagree.

    Args:
        page: 1-based page number.
        page_size: Records per page, 1-50.

    Returns:
        dict: {count, data, source}. data is a list of reconciliation records with
        account, symbol, both sides' quantities and the action taken. On error:
        {error, detail}.
    """
    return _envelope(
        _call("GET", "/api/reconciliation/logs", params={"page": page, "page_size": page_size})
    )


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------


def _confirmed(confirm: bool, what: str) -> dict[str, Any] | None:
    """Guard for every mutating call: no explicit confirm, no side effect."""
    if confirm:
        return None
    return make_error(
        "invalid_argument",
        f"refused: pass confirm=True to {what}. Nothing was sent to the exchange.",
    )


_OUT_HALT = output_model("RobatHalt", envelope_schema(ANY))


@mcp.tool()
def halt_trader(trader_id: str, reason: str = "MANUAL_HALT", confirm: bool = False) -> _OUT_HALT:
    """Stop a trader from opening anything new. Existing positions stay managed.
    Use for a loss limit breach or a trader behaving out of character.

    Args:
        trader_id: The trader to halt.
        reason: Recorded halt reason.
        confirm: Must be True; otherwise nothing is sent.

    Returns:
        dict: {count, data, source}. data carries {status, control, summary} — the
        new risk control record and the refreshed trader view. On error: {error, detail}.
    """
    refused = _confirmed(confirm, "halt a trader")
    if refused is not None:
        return refused
    return _envelope(_call("POST", f"/risk/traders/{urllib.parse.quote(trader_id, safe='')}/halt", params={"reason": reason}))


_OUT_RESUME = output_model("RobatResume", envelope_schema(ANY))


@mcp.tool()
def resume_trader(trader_id: str, confirm: bool = False) -> _OUT_RESUME:
    """Re-enable a halted trader and clear its loss streak counter.

    Args:
        trader_id: The trader to resume.
        confirm: Must be True; otherwise nothing is sent.

    Returns:
        dict: {count, data, source}. data carries {status, control, summary}. On
        error: {error, detail}.
    """
    refused = _confirmed(confirm, "resume a trader")
    if refused is not None:
        return refused
    return _envelope(_call("POST", f"/risk/traders/{urllib.parse.quote(trader_id, safe='')}/resume"))


_OUT_MANAGE = output_model("RobatManagePosition", envelope_schema(ANY))


@mcp.tool()
def manage_position(
    trader_id: str,
    action: str,
    symbol: str | None = None,
    account_key: str | None = None,
    stop_price: float | None = None,
    tp_levels: list[float] | None = None,
    price_hint: float | None = None,
    reason: str | None = None,
    confirm: bool = False,
) -> _OUT_MANAGE:
    """Change a live position: close it, move the stop, lock profit, or reset TP tiers.
    Omit symbol only when one open position can be resolved unambiguously.

    Args:
        trader_id: Owner of the position.
        action: CLOSE_POSITION | MOVE_STOP_TO_BE | LOCK_PROFIT_AND_BREAKEVEN |
            COST_FIRST_WALK | SET_STOP | SET_TAKE_PROFIT | MOVE_STOP.
        symbol: Exchange symbol; omit to resolve from the trader's open positions.
        account_key: Exchange account; omit for the default account.
        stop_price: New stop, required by SET_STOP and MOVE_STOP.
        tp_levels: New take-profit prices, required by SET_TAKE_PROFIT.
        price_hint: Current price, helps symbol resolution.
        reason: Recorded reason for the change.
        confirm: Must be True; otherwise nothing is sent.

    Returns:
        dict: {count, data, source}. data is the management result, or
        {status: rejected, reason: symbol_missing|symbol_unresolved_refused_guess}
        when the symbol could not be settled. On error: {error, detail}.
    """
    refused = _confirmed(confirm, "manage a position")
    if refused is not None:
        return refused
    body: dict[str, Any] = {"trader_id": trader_id, "action": action}
    for key, value in (
        ("symbol", symbol),
        ("account_key", account_key),
        ("stop_price", stop_price),
        ("tp_levels", tp_levels),
        ("price_hint", price_hint),
        ("reason", reason),
    ):
        if value is not None:
            body[key] = value
    return _envelope(_call("POST", "/maintenance/manage-trader-position", body=body))


_OUT_TRADER_POSITIONS = output_model("RobatTraderPositions", envelope_schema(ANY))


@mcp.tool()
def get_trader_open_positions(
    trader_id: str,
    account_key: str | None = None,
    include_mark: bool = True,
) -> _OUT_TRADER_POSITIONS:
    """One trader's open positions straight from the exchange, with mark prices.
    Read this before deciding what to close or move.

    Args:
        trader_id: The trader to inspect.
        account_key: Exchange account; omit for the default account.
        include_mark: Include mark price and unrealised P&L.

    Returns:
        dict: {count, data, source}. data is the list of open positions as the
        exchange reports them. On error: {error, detail}. Note this endpoint is a
        POST on the console API but changes nothing.
    """
    body: dict[str, Any] = {"trader_id": trader_id, "include_mark": bool(include_mark)}
    if account_key is not None:
        body["account_key"] = account_key
    return _envelope(_call("POST", "/maintenance/trader-open-positions", body=body))


_OUT_CANCEL_ENTRY = output_model("RobatCancelPendingEntry", envelope_schema(ANY))


@mcp.tool()
def cancel_pending_entry(
    trader_id: str,
    symbol: str,
    account_key: str | None = None,
    reason: str | None = None,
    confirm: bool = False,
) -> _OUT_CANCEL_ENTRY:
    """Cancel a trader's not-yet-filled entry orders for one symbol.

    Args:
        trader_id: Owner of the pending entry.
        symbol: Exchange symbol like "BTCUSDT".
        account_key: Exchange account; omit for the default account.
        reason: Recorded reason.
        confirm: Must be True; otherwise nothing is sent.

    Returns:
        dict: {count, data, source}. data is the cancellation result. On error:
        {error, detail}.
    """
    refused = _confirmed(confirm, "cancel a pending entry")
    if refused is not None:
        return refused
    body: dict[str, Any] = {"trader_id": trader_id, "symbol": symbol}
    for key, value in (("account_key", account_key), ("reason", reason)):
        if value is not None:
            body[key] = value
    return _envelope(_call("POST", "/maintenance/cancel-pending-entry", body=body))


_OUT_CANCEL_ORDER = output_model("RobatCancelOrder", envelope_schema(ANY))


@mcp.tool()
def cancel_order(
    signal_id: str,
    order_key: str | None = None,
    exchange_order_id: int | None = None,
    client_order_id: str | None = None,
    confirm: bool = False,
) -> _OUT_CANCEL_ORDER:
    """Cancel one specific order by its order key, exchange id, or client id.

    Args:
        signal_id: Signal the order belongs to.
        order_key: Local order key.
        exchange_order_id: Exchange-assigned id.
        client_order_id: Client-assigned id.
        confirm: Must be True; otherwise nothing is sent.

    Returns:
        dict: {count, data, source}. data is the cancellation result; not_found
        when no such order, invalid_argument when it is no longer cancellable.
    """
    refused = _confirmed(confirm, "cancel an order")
    if refused is not None:
        return refused
    body: dict[str, Any] = {"signal_id": signal_id}
    for key, value in (
        ("order_key", order_key),
        ("exchange_order_id", exchange_order_id),
        ("client_order_id", client_order_id),
    ):
        if value is not None:
            body[key] = value
    return _envelope(_call("POST", "/maintenance/cancel-order", body=body))


_OUT_RECONCILE = output_model("RobatReconcile", envelope_schema(ANY))


@mcp.tool()
def reconcile_positions(
    account_key: str | None = None,
    symbol: str | None = None,
    confirm: bool = False,
) -> _OUT_RECONCILE:
    """Force a position reconciliation against the exchange for one scope.
    Use after an alert says the ledger and the exchange disagree.

    Args:
        account_key: Exchange account; omit for every account.
        symbol: Exchange symbol like "BTCUSDT"; omit for every symbol.
        confirm: Must be True; otherwise nothing is sent.

    Returns:
        dict: {count, data, source}. data is the reconciliation result per scope.
        On error: {error, detail}.
    """
    refused = _confirmed(confirm, "run a reconciliation")
    if refused is not None:
        return refused
    body: dict[str, Any] = {}
    for key, value in (("account_key", account_key), ("symbol", symbol)):
        if value is not None:
            body[key] = value
    return _envelope(_call("POST", "/maintenance/reconcile-positions", body=body))


_OUT_SETTINGS = output_model("RobatRuntimeSettings", envelope_schema(OBJECT))


@mcp.tool()
def get_runtime_settings() -> _OUT_SETTINGS:
    """Current runtime trading switches: entry enabled, risk multipliers, intervals.

    Returns:
        dict: {count, data, source}. data is the settings record currently in force.
        On error: {error, detail}.
    """
    return _envelope(_call("GET", "/api/runtime-settings"))


@mcp.tool()
def update_runtime_settings(patch: dict[str, Any], confirm: bool = False) -> _OUT_SETTINGS:
    """Change runtime switches. Read get_runtime_settings first — the patch is merged
    into the current record, so never send a whole record built from memory.

    Args:
        patch: Only the keys to change, e.g. {"entry_enabled": false}.
        confirm: Must be True; otherwise nothing is sent.

    Returns:
        dict: {count, data, source}. data is the settings record after the change.
        On error: {error, detail} with invalid_argument for a rejected key.
    """
    refused = _confirmed(confirm, "change runtime settings")
    if refused is not None:
        return refused
    return _envelope(_call("POST", "/api/runtime-settings", body=patch))


if __name__ == "__main__":
    mcp.run(transport="stdio")
