"""Migration 092: give connected Webull rows the description that matches their tools.

A brokerage row is written with the shipped description when the user connects
it, and nothing rewrites it later. Every Webull row connected before the order
instructions were curated therefore still tells the agent that nothing there
places, previews or stages an order, while its staged-order tools are offered
beside that sentence. Only a row still holding the exact shipped text is
rewritten, so a description the user edited stays theirs.

Pinned as literals rather than imported for the reason 030 gives: the shipped
text is free to change again, and a later change is that change's migration.

Revision ID: 092
Revises: 090
"""

from alembic import op

revision = "092"
down_revision = "090"
branch_labels = None
depends_on = None

_HOST = r"^https?://api\.webull\.com(:[0-9]+)?([/?#]|$)"

_OLD = (
    "Webull brokerage account: balances, positions and buying power, "
    "order history, watchlists, real-time quotes, and instrument "
    "reference data. Reads only: nothing here places, previews or "
    "stages an order. Covers equities, options, futures, event "
    "contracts and crypto."
)

_NEW = (
    "Webull brokerage account: balances, positions and buying power, "
    "order history, watchlists, real-time quotes, instrument reference "
    "data, fundamentals, news, and insider and institutional holdings, "
    "and draft orders for stocks, options, futures and crypto that the "
    "user confirms in the Webull app before anything is placed. Covers "
    "equities, options, futures, event contracts and crypto."
)


def _rewrite(old: str, new: str) -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # The description reaches the agent through the composite, which is cached
    # per workspace against mcp_config_version, so the change is only seen once
    # each affected user's workspaces move on.
    op.execute(
        f"""
        WITH rewritten AS (
            UPDATE user_mcp_servers SET description = $d${new}$d$
             WHERE description = $d${old}$d$
               AND url ~* '{_HOST}'
            RETURNING user_id
        )
        UPDATE workspaces ws SET mcp_config_version = ws.mcp_config_version + 1
         WHERE ws.user_id IN (SELECT DISTINCT user_id FROM rewritten)
        """
    )


def upgrade() -> None:
    _rewrite(_OLD, _NEW)


def downgrade() -> None:
    _rewrite(_NEW, _OLD)
