"""phase4 workflow schema

Revision ID: 20260421_120000
Revises: 20260419_120000
Create Date: 2026-04-21 12:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260421_120000"
down_revision = "20260419_120000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_items",
        sa.Column("review_id", sa.String(length=128), primary_key=True, nullable=False),
        sa.Column("subject_kind", sa.String(length=32), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("spotify_id", sa.String(length=128), nullable=False),
        sa.Column("review_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("decision_comment", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.String(length=128), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=False), nullable=False),
        sa.ForeignKeyConstraint(["spotify_id"], ["artists.spotify_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("subject_kind", "subject_id", "review_type", name="uq_review_items_subject_type"),
        sa.CheckConstraint("version >= 1", name="ck_review_items_version_positive"),
    )
    op.create_index("ix_review_items_subject", "review_items", ["subject_kind", "subject_id"])
    op.create_index(
        "ix_review_items_status_created_at",
        "review_items",
        ["status", "created_at"],
    )

    op.create_table(
        "audit_log_entries",
        sa.Column("log_id", sa.String(length=128), primary_key=True, nullable=False),
        sa.Column("aggregate_type", sa.String(length=32), nullable=False),
        sa.Column("aggregate_id", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=False), nullable=False),
    )
    op.create_index(
        "ix_audit_log_entries_aggregate",
        "audit_log_entries",
        ["aggregate_type", "aggregate_id", "created_at"],
    )
    op.create_index(
        "ix_audit_log_entries_actor",
        "audit_log_entries",
        ["actor_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_entries_actor", table_name="audit_log_entries")
    op.drop_index("ix_audit_log_entries_aggregate", table_name="audit_log_entries")
    op.drop_table("audit_log_entries")

    op.drop_index("ix_review_items_status_created_at", table_name="review_items")
    op.drop_index("ix_review_items_subject", table_name="review_items")
    op.drop_table("review_items")
