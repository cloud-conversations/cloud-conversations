"""builds that resume after a restart, and the maintenance pause

Revision ID: 002
Revises: 001
Create Date: 2026-11-03
"""
from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE auth.settings ADD COLUMN paused boolean NOT NULL DEFAULT false")
    op.execute("""
        CREATE TABLE experiments.planned_resources (
            idempotency_key text        PRIMARY KEY,
            run_id          uuid        NOT NULL
                            REFERENCES experiments.runs (run_id) ON DELETE CASCADE,
            kind            text        NOT NULL,
            site            text        NOT NULL,
            created_at      timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ON experiments.planned_resources (run_id)")
    op.execute("""
        ALTER TABLE chat.jobs
            ADD COLUMN claim         uuid,
            ADD COLUMN claimed_until timestamptz,
            ADD COLUMN attempts      int NOT NULL DEFAULT 0,
            ADD COLUMN run_after     timestamptz NOT NULL DEFAULT now()
    """)
    op.execute(
        "UPDATE chat.jobs SET claimed_until = now(), attempts = 1 WHERE state = 'running'"
    )
    op.execute(
        "CREATE INDEX jobs_to_claim ON chat.jobs (created_at) WHERE state IN ('queued', 'running')"
    )
    op.execute("""
        ALTER TABLE experiments.run_resources
            ADD COLUMN warned_at  timestamptz,
            ADD COLUMN checked_at timestamptz
    """)


def downgrade() -> None:
    op.execute(
        "ALTER TABLE experiments.run_resources DROP COLUMN warned_at, DROP COLUMN checked_at"
    )
    op.execute("DROP INDEX chat.jobs_to_claim")
    op.execute(
        "ALTER TABLE chat.jobs DROP COLUMN claim, DROP COLUMN claimed_until, "
        "DROP COLUMN attempts, DROP COLUMN run_after"
    )
    op.execute("DROP TABLE experiments.planned_resources")
    op.execute("ALTER TABLE auth.settings DROP COLUMN paused")
