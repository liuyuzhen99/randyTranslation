"""phase6 async pipeline schema

Revision ID: 20260427_120000
Revises: 20260426_120000
Create Date: 2026-04-27 12:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260427_120000"
down_revision = "20260426_120000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pipeline_stage_executions",
        sa.Column("execution_id", sa.String(length=160), primary_key=True, nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("stage", sa.String(length=18), nullable=False),
        sa.Column("candidate_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=15), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("next_retry_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("result_payload", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=False), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.job_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["candidate_id"], ["video_candidates.candidate_id"], ondelete="SET NULL"),
        sa.UniqueConstraint("dedupe_key", name="uq_pipeline_stage_executions_dedupe_key"),
        sa.CheckConstraint("attempt >= 0", name="ck_pipeline_stage_executions_attempt_non_negative"),
        sa.CheckConstraint("max_attempts >= 1", name="ck_pipeline_stage_executions_max_attempts_positive"),
    )
    op.create_index(
        "ix_pipeline_stage_executions_job_stage",
        "pipeline_stage_executions",
        ["job_id", "stage"],
    )
    op.create_index(
        "ix_pipeline_stage_executions_candidate",
        "pipeline_stage_executions",
        ["candidate_id", "created_at"],
    )
    op.create_index(
        "ix_pipeline_stage_executions_status_retry",
        "pipeline_stage_executions",
        ["status", "next_retry_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_pipeline_stage_executions_status_retry", table_name="pipeline_stage_executions")
    op.drop_index("ix_pipeline_stage_executions_candidate", table_name="pipeline_stage_executions")
    op.drop_index("ix_pipeline_stage_executions_job_stage", table_name="pipeline_stage_executions")
    op.drop_table("pipeline_stage_executions")
