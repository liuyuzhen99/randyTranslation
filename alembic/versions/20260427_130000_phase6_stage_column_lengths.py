"""phase6 stage column lengths

Revision ID: 20260427_130000
Revises: 20260427_120000
Create Date: 2026-04-27 13:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260427_130000"
down_revision = "20260427_120000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.alter_column("jobs", "current_stage", type_=sa.String(length=32), existing_nullable=True)
    op.alter_column("job_events", "stage", type_=sa.String(length=32), existing_nullable=True)
    op.alter_column(
        "pipeline_stage_executions",
        "stage",
        type_=sa.String(length=32),
        existing_nullable=False,
    )
    op.alter_column(
        "pipeline_stage_executions",
        "status",
        type_=sa.String(length=32),
        existing_nullable=False,
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.alter_column(
        "pipeline_stage_executions",
        "status",
        type_=sa.String(length=15),
        existing_nullable=False,
    )
    op.alter_column(
        "pipeline_stage_executions",
        "stage",
        type_=sa.String(length=18),
        existing_nullable=False,
    )
    op.alter_column("job_events", "stage", type_=sa.String(length=10), existing_nullable=True)
    op.alter_column("jobs", "current_stage", type_=sa.String(length=10), existing_nullable=True)
