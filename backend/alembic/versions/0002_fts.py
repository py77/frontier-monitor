"""raw_items full-text search column

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-08

"""
from alembic import op


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Stored generated column over title+raw_text. Postgres 12+.
    op.execute("""
        ALTER TABLE raw_items
        ADD COLUMN search_tsv tsvector
        GENERATED ALWAYS AS (
            to_tsvector('english', coalesce(title, '') || ' ' || coalesce(raw_text, ''))
        ) STORED
    """)
    op.execute("CREATE INDEX raw_items_search_idx ON raw_items USING GIN (search_tsv)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS raw_items_search_idx")
    op.execute("ALTER TABLE raw_items DROP COLUMN IF EXISTS search_tsv")
