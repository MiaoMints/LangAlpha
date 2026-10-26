"""A tool's content identity, which every classification is keyed by.

The digest covers everything a vendor can change about a tool that could change
what it does or what a model would say about it, and nothing else, so a vendor
adding one tool reclassifies that tool alone.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def tool_digest(tool: dict[str, Any]) -> str:
    """Hash of a cached tool's name, description, input schema and annotations."""
    canonical = json.dumps(
        {
            "name": tool.get("name") or "",
            "description": tool.get("description") or "",
            "inputSchema": tool.get("input_schema") or {},
            "annotations": tool.get("annotations") or {},
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
