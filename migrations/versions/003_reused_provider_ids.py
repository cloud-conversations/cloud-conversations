"""provider ids recorded again after their resource is released

Revision ID: 003
Revises: 002
Create Date: 2026-11-11
"""
from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE experiments.run_resources DROP CONSTRAINT run_resources_pkey")
    op.execute("""
        CREATE UNIQUE INDEX run_resources_live_provider_id_uidx
            ON experiments.run_resources (kind, provider_id)
         WHERE released_at IS NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX experiments.run_resources_live_provider_id_uidx")
    op.execute("ALTER TABLE experiments.run_resources ADD PRIMARY KEY (kind, provider_id)")
