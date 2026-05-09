"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-08

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("pillar", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_sources_pillar", "sources", ["pillar"])

    op.create_table(
        "raw_items",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("source_id", sa.String(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("pillar", sa.String(), nullable=False),
        sa.Column("url", sa.String()),
        sa.Column("title", sa.String()),
        sa.Column("author", sa.String()),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_text", sa.String()),
        sa.Column("raw_json", postgresql.JSONB(astext_type=sa.Text())),
    )
    op.create_index("ix_raw_items_source_id", "raw_items", ["source_id"])
    op.create_index("ix_raw_items_pillar", "raw_items", ["pillar"])
    op.create_index("ix_raw_items_published_at", "raw_items", ["published_at"])
    op.create_index("ix_raw_items_pillar_pub", "raw_items", ["pillar", "published_at"])

    op.create_table(
        "signals",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("raw_item_id", sa.String(), sa.ForeignKey("raw_items.id"), nullable=False),
        sa.Column("signal_type", sa.String(), nullable=False),
        sa.Column("analyst_version", sa.String(), nullable=False),
        sa.Column("pillar", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("raw_item_id", "signal_type", "analyst_version", name="uq_signal_idem"),
    )
    op.create_index("ix_signals_raw_item_id", "signals", ["raw_item_id"])
    op.create_index("ix_signals_pillar", "signals", ["pillar"])

    op.create_table(
        "timeseries",
        sa.Column("series", sa.String(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text())),
    )

    op.create_table(
        "digests",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("period", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("markdown", sa.String(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("digests")
    op.drop_table("timeseries")
    op.drop_index("ix_signals_pillar", table_name="signals")
    op.drop_index("ix_signals_raw_item_id", table_name="signals")
    op.drop_table("signals")
    op.drop_index("ix_raw_items_pillar_pub", table_name="raw_items")
    op.drop_index("ix_raw_items_published_at", table_name="raw_items")
    op.drop_index("ix_raw_items_pillar", table_name="raw_items")
    op.drop_index("ix_raw_items_source_id", table_name="raw_items")
    op.drop_table("raw_items")
    op.drop_index("ix_sources_pillar", table_name="sources")
    op.drop_table("sources")
