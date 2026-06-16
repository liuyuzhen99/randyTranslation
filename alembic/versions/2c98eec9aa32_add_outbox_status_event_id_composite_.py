"""add outbox status event_id composite index

Revision ID: 2c98eec9aa32
Revises: 20260427_140000
Create Date: 2026-05-11 20:05:08.593339
"""
from __future__ import annotations

from alembic import op



# revision identifiers, used by Alembic.
revision = '2c98eec9aa32'
down_revision = '20260427_140000'
branch_labels = None
depends_on = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    if _is_sqlite():
        op.create_index('ix_outbox_status_event_id', 'outbox', ['status', 'event_id'], unique=False)
        return

    op.execute("CREATE INDEX IF NOT EXISTS ix_outbox_status_event_id ON outbox (status, event_id)")


def downgrade() -> None:
    if _is_sqlite():
        op.drop_index('ix_outbox_status_event_id', table_name='outbox')
        return

    op.execute("DROP INDEX IF EXISTS ix_outbox_status_event_id")
