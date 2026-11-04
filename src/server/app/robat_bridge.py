"""ROBAT Bridge API Router - read-only proxy of the local ROBAT console API."""

import logging
import os

import httpx
from fastapi import APIRouter

from src.server.utils.api import CurrentUserId

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/robat", tags=["ROBAT Bridge"])

_DEFAULT_BASE_URL = "http://127.0.0.1:8000"
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def _base_url() -> str:
    return (os.getenv("ROBAT_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")


def _token() -> str:
    return (os.getenv("ROBAT_API_TOKEN") or "").strip()


def _num(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _project(item: dict) -> dict:
    """Keep only the fields the dashboard card needs."""
    risk = _num(item.get("risk_budget_usdt"))
    pnl = _num(item.get("unrealized_pnl"))
    r_multiple = (pnl / risk) if (pnl is not None and risk and risk > 0) else None
    steps = []
    for step in item.get("tp_steps") or []:
        if not isinstance(step, dict):
            continue
        steps.append(
            {
                "name": step.get("name"),
                "target_price": _num(step.get("target_price")),
                "planned_qty": _num(step.get("planned_qty")),
                "filled_qty": _num(step.get("filled_qty")),
                "status": step.get("status"),
                "algo_status": step.get("algo_status"),
            }
        )
    return {
        "symbol": item.get("symbol"),
        "side": item.get("side"),
        "state": item.get("state"),
        "quantity": _num(item.get("entry_executed_qty")),
        "entry": _num(item.get("entry_avg_price")) or _num(item.get("entry_price")),
        "mark": _num(item.get("mark_price")),
        "take_profit": _num(item.get("tp")),
        "stop_loss": _num(item.get("sl")),
        "unrealized_pnl": pnl,
        "realized_pnl": _num(item.get("realized_pnl")),
        "r_multiple": r_multiple,
        "risk_budget_usdt": risk,
        "margin_usdt": _num(item.get("margin_usdt")),
        "notional_usdt": _num(item.get("notional_usdt")),
        "leverage": _num(item.get("configured_leverage")),
        "trader_name": item.get("trader_name"),
        "tp_status": item.get("tp_status"),
        "sl_status": item.get("sl_status"),
        "tp_steps": steps,
        "updated_at": item.get("updated_at"),
    }


def _unavailable(status: str, error: str) -> dict:
    return {"status": status, "error": error, "count": 0, "total_count": 0, "positions": []}


@router.get("/positions")
async def list_robat_positions(user_id: CurrentUserId, page: int = 1, page_size: int = 50):
    """Live ROBAT positions, read-only. Never raises for upstream failures."""
    token = _token()
    if not token:
        logger.warning("ROBAT bridge: ROBAT_API_TOKEN is not configured")
        return _unavailable("unconfigured", "ROBAT_API_TOKEN is not set")

    url = f"{_base_url()}/api/positions/active"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                url,
                params={"page": page, "page_size": page_size},
                headers={"X-Token": token, "Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        logger.warning("ROBAT bridge: positions unreachable at %s (%s)", url, type(exc).__name__)
        return _unavailable("unavailable", type(exc).__name__)

    if resp.status_code != 200:
        logger.warning("ROBAT bridge: positions HTTP %s from %s", resp.status_code, url)
        return _unavailable("unavailable", f"http_{resp.status_code}")

    try:
        payload = resp.json()
    except ValueError:
        return _unavailable("unavailable", "invalid_json")

    items = payload.get("items") or []
    positions = [_project(item) for item in items if isinstance(item, dict)]
    return {
        "status": "ok",
        "error": "",
        "count": len(positions),
        "total_count": payload.get("total_count", len(positions)),
        "positions": positions,
    }
