"""drop FTS column (search dropped); add alerts table

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-08

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Surgical reset: search/FTS feature removed.
    op.execute("DROP INDEX IF EXISTS raw_items_search_idx")
    op.execute("ALTER TABLE raw_items DROP COLUMN IF EXISTS search_tsv")

    # New: alerts table for threshold-cross history.
    op.create_table(
        "alerts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("fired_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("dimension", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),  # info | warn | critical
        sa.Column("headline", sa.String(), nullable=False),
        sa.Column("detail", sa.String()),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text())),
    )
    op.create_index("ix_alerts_fired_at", "alerts", ["fired_at"])


def downgrade() -> None:
    op.drop_index("ix_alerts_fired_at", table_name="alerts")
    op.drop_table("alerts")
    op.execute("""
        ALTER TABLE raw_items
        ADD COLUMN search_tsv tsvector
        GENERATED ALWAYS AS (
            to_tsvector('english', coalesce(title, '') || ' ' || coalesce(raw_text, ''))
        ) STORED
    """)
    op.execute("CREATE INDEX raw_items_search_idx ON raw_items USING GIN (search_tsv)")
