"""phase5 artifacts schema

Revision ID: 20260426_120000
Revises: 20260421_120000
Create Date: 2026-04-26 12:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260426_120000"
down_revision = "20260421_120000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("artifact_id", sa.String(length=160), primary_key=True, nullable=False),
        sa.Column("owner_type", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("artifact_type", sa.String(length=64), nullable=False),
        sa.Column("object_uri", sa.Text(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("bucket", sa.String(length=255), nullable=False),
        sa.Column("storage_provider", sa.String(length=64), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("job_id", sa.String(length=64), nullable=True),
        sa.Column("candidate_id", sa.String(length=128), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("lifecycle_status", sa.String(length=32), nullable=False, server_default="ready"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=False), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.job_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["video_candidates.candidate_id"],
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "owner_type",
            "owner_id",
            "artifact_type",
            "version",
            name="uq_artifacts_owner_type_version",
        ),
        sa.CheckConstraint("version >= 1", name="ck_artifacts_version_positive"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_artifacts_size_non_negative"),
    )
    op.create_index("ix_artifacts_job_id", "artifacts", ["job_id"])
    op.create_index("ix_artifacts_owner", "artifacts", ["owner_type", "owner_id"])
    op.create_index("ix_artifacts_object_uri", "artifacts", ["object_uri"])


def downgrade() -> None:
    op.drop_index("ix_artifacts_object_uri", table_name="artifacts")
    op.drop_index("ix_artifacts_owner", table_name="artifacts")
    op.drop_index("ix_artifacts_job_id", table_name="artifacts")
    op.drop_table("artifacts")
