"""phase6 stage constraints

Revision ID: 20260427_140000
Revises: 20260427_130000
Create Date: 2026-04-27 14:00:00
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260427_140000"
down_revision = "20260427_130000"
branch_labels = None
depends_on = None

NEW_STAGE_NAMES = (
    "DOWNLOAD",
    "TRANSCRIBE",
    "AUDIT",
    "MANUAL_REVIEW",
    "TRANSLATE",
    "TRANSLATION_REVIEW",
    "RENDER",
)
OLD_STAGE_NAMES = ("DOWNLOAD", "TRANSCRIBE", "AUDIT", "TRANSLATE", "RENDER")


def _stage_constraint(names: tuple[str, ...]) -> str:
    values = ", ".join(f"'{name}'" for name in names)
    return f"current_stage IS NULL OR current_stage IN ({values})"


def _job_event_stage_constraint(names: tuple[str, ...]) -> str:
    values = ", ".join(f"'{name}'" for name in names)
    return f"stage IS NULL OR stage IN ({values})"


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE jobs DROP CONSTRAINT IF EXISTS stagetype")
    op.create_check_constraint("stagetype", "jobs", _stage_constraint(NEW_STAGE_NAMES))
    op.execute("ALTER TABLE job_events DROP CONSTRAINT IF EXISTS stagetype")
    op.create_check_constraint("stagetype", "job_events", _job_event_stage_constraint(NEW_STAGE_NAMES))


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE job_events DROP CONSTRAINT IF EXISTS stagetype")
    op.create_check_constraint("stagetype", "job_events", _job_event_stage_constraint(OLD_STAGE_NAMES))
    op.execute("ALTER TABLE jobs DROP CONSTRAINT IF EXISTS stagetype")
    op.create_check_constraint("stagetype", "jobs", _stage_constraint(OLD_STAGE_NAMES))
