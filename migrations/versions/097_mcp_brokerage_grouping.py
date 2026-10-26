"""Migration 097: model-proposed groups for brokerage tools, and their queue.

Four new tables, nothing existing touched.

``mcp_tool_classifications`` caches what a model said a shipped brokerage's
uncurated tool does and which capability group it belongs in. Every row is
keyed by ``scope_key`` (``brokerage:<name>``, shared by every user because
every user sees the same vendor list) and by the tool's ``tool_digest``, plus
the ``model`` and ``prompt_version`` that wrote it, so a vendor publishing one
new tool adds a row for that tool alone, and a new model or prompt writes new
rows beside the old ones rather than over them. The gate reads only the current
model and version.

``mcp_tool_choices`` holds a user's own group for a brokerage read tool, made
on the tools page. ``mcp_curation_items`` lists the tools a model looked at and
the gate left ungrouped, for a person to curate.

``mcp_grouping_jobs`` is the queue. Discovery inserts a row when a brokerage's
tool list holds a tool no current row classifies; any worker claims it. One
pending row per brokerage: a second discovery before the first is claimed
replaces its tool list rather than queueing a duplicate.

Rollback: ``alembic downgrade 095``. The previous build never reads these
tables, so it can also run beside them.

Revision ID: 097
Revises: 095
"""

from alembic import op

revision = "097"
down_revision = "095"
branch_labels = None
depends_on = None

_EFFECTS = "('read', 'account_write', 'public_post', 'order')"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # ``effect`` is the one the gate reads: the model's (kept in ``model_effect``),
    # tightened by the deterministic rules, whose verdict is kept in ``rule``.
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS mcp_tool_classifications (
            scope_key TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            tool_digest VARCHAR(64) NOT NULL,
            model TEXT NOT NULL,
            prompt_version VARCHAR(32) NOT NULL,
            group_key VARCHAR(64),
            effect VARCHAR(16) NOT NULL CHECK (effect IN {_EFFECTS}),
            model_effect VARCHAR(16) NOT NULL CHECK (model_effect IN {_EFFECTS}),
            rule TEXT,
            confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
            reason TEXT NOT NULL DEFAULT '',
            reviewed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (scope_key, tool_name, tool_digest, model, prompt_version)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS mcp_tool_choices (
            user_id VARCHAR(255) NOT NULL,
            brokerage VARCHAR(64) NOT NULL,
            tool_name TEXT NOT NULL,
            group_key VARCHAR(64),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, brokerage, tool_name)
        )
    """)
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS mcp_curation_items (
            brokerage VARCHAR(64) NOT NULL,
            tool_name TEXT NOT NULL,
            tool_digest VARCHAR(64) NOT NULL,
            effect VARCHAR(16) NOT NULL CHECK (effect IN {_EFFECTS}),
            group_key VARCHAR(64),
            reason TEXT NOT NULL DEFAULT '',
            first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            resolved_at TIMESTAMPTZ,
            PRIMARY KEY (brokerage, tool_name, tool_digest)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS mcp_grouping_jobs (
            job_id BIGSERIAL PRIMARY KEY,
            scope_key TEXT NOT NULL,
            server_name VARCHAR(255) NOT NULL,
            tools JSONB NOT NULL,
            status VARCHAR(10) NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'failed')),
            attempts INTEGER NOT NULL DEFAULT 0,
            run_after TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            lease_until TIMESTAMPTZ,
            error TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_mcp_grouping_jobs_pending
        ON mcp_grouping_jobs (scope_key) WHERE status = 'pending'
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_mcp_grouping_jobs_due
        ON mcp_grouping_jobs (run_after) WHERE status IN ('pending', 'running')
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_mcp_curation_items_open
        ON mcp_curation_items (brokerage, last_seen_at DESC) WHERE resolved_at IS NULL
    """)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for table in (
        "mcp_grouping_jobs",
        "mcp_curation_items",
        "mcp_tool_choices",
        "mcp_tool_classifications",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table}")
