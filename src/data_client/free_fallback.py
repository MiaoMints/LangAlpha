"""Kill switch for the free stand-ins behind the MCP servers' paid sources.

``LANGALPHA_FREE_FALLBACK=0`` turns every stand-in off, so a call fails the
way it did before they existed (yfinance and FINRA are then never contacted).
"""

from __future__ import annotations

import os

_OFF = {"0", "false", "off", "no"}


def free_fallback_enabled() -> bool:
    """Whether a paid source that is missing or failing may be stood in for."""
    return os.getenv("LANGALPHA_FREE_FALLBACK", "1").strip().lower() not in _OFF
