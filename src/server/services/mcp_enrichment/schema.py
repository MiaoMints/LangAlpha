"""What the two stages produce, and the versions their rows are cached under.

The output models are the structured-output schema the model fills, so a field
here is a question the model is asked; the stored rows keep the same names.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

# A change to either stage's instructions or output shape bumps its version, which
# makes every cached row of that stage stale: the worker reruns it and the gate
# stops reading the old rows. Old rows stay for audit.
PROFILE_PROMPT_VERSION = "1"
GROUPING_PROMPT_VERSION = "1"


class Effect(StrEnum):
    """What calling a tool does, ordered from nothing at stake to an order."""

    READ = "read"
    ACCOUNT_WRITE = "account_write"
    PUBLIC_POST = "public_post"
    ORDER = "order"

    @property
    def rank(self) -> int:
        return list(Effect).index(self)

    def at_least(self, floor: Effect) -> Effect:
        """The stricter of this effect and ``floor``. Rules only ever tighten."""
        return self if self.rank >= floor.rank else floor


class ServerProfile(BaseModel):
    """The server as a whole: the one line the agent reads beside its name."""

    description: str


class ToolGroup(BaseModel):
    """One brokerage tool: what its calls do, and a group key from the brokerage's
    list or none. The effect comes first so the model settles it before the group;
    it feeds the gate only and never reaches the agent."""

    name: str
    effect: Effect
    group: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = ""


class ToolGroupBatch(BaseModel):
    tools: list[ToolGroup]
