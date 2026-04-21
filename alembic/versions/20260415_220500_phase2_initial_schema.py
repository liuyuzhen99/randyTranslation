"""phase2 initial schema

Revision ID: 20260415_220500
Revises:
Create Date: 2026-04-15 22:05:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260415_220500"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artists",
        sa.Column("spotify_id", sa.String(length=128), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("yt_channel_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
    )

    op.create_table(
        "videos",
        sa.Column("video_id", sa.String(length=128), primary_key=True, nullable=False),
        sa.Column("spotify_id", sa.String(length=128), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("processed_status", sa.String(length=32), nullable=False),
        sa.Column("local_video_path", sa.Text(), nullable=True),
        sa.Column("srt_path", sa.Text(), nullable=True),
        sa.Column("final_video_path", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["spotify_id"], ["artists.spotify_id"]),
    )
    op.create_index("ix_videos_spotify_id", "videos", ["spotify_id"])
    op.create_index("ix_videos_processed_status", "videos", ["processed_status"])

    op.create_table(
        "subtitles",
        sa.Column("subtitle_id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("video_id", sa.String(length=128), nullable=False),
        sa.Column("line_index", sa.Integer(), nullable=False),
        sa.Column("start_time", sa.Float(), nullable=False),
        sa.Column("end_time", sa.Float(), nullable=False),
        sa.Column("en_text", sa.Text(), nullable=False),
        sa.Column("zh_text", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["video_id"], ["videos.video_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("video_id", "line_index", name="uq_subtitles_video_line_index"),
    )

    op.create_table(
        "jobs",
        sa.Column("job_id", sa.String(length=64), primary_key=True, nullable=False),
        sa.Column("song_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("progress", sa.Text(), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("current_stage", sa.String(length=32), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=False), nullable=False),
        sa.CheckConstraint("retry_count >= 0", name="ck_jobs_retry_count_non_negative"),
    )
    op.create_index("ix_jobs_status", "jobs", ["status"])

    op.create_table(
        "job_events",
        sa.Column("event_id", sa.String(length=128), primary_key=True, nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=False), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.job_id"], ondelete="CASCADE"),
        sa.CheckConstraint("retry_count >= 0", name="ck_job_events_retry_count_non_negative"),
    )
    op.create_index("ix_job_events_job_id_created_at", "job_events", ["job_id", "created_at"])

    op.create_table(
        "outbox",
        sa.Column("event_id", sa.String(length=128), primary_key=True, nullable=False),
        sa.Column("topic", sa.String(length=255), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("aggregate_id", sa.String(length=128), nullable=True),
        sa.Column("dedupe_key", sa.String(length=255), nullable=True),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.UniqueConstraint("dedupe_key", name="uq_outbox_dedupe_key"),
    )
    op.create_index("ix_outbox_status", "outbox", ["status"])
    op.create_index("ix_outbox_correlation_id", "outbox", ["correlation_id"])


def downgrade() -> None:
    op.drop_index("ix_outbox_correlation_id", table_name="outbox")
    op.drop_index("ix_outbox_status", table_name="outbox")
    op.drop_table("outbox")

    op.drop_index("ix_job_events_job_id_created_at", table_name="job_events")
    op.drop_table("job_events")

    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_table("jobs")

    op.drop_table("subtitles")

    op.drop_index("ix_videos_processed_status", table_name="videos")
    op.drop_index("ix_videos_spotify_id", table_name="videos")
    op.drop_table("videos")

    op.drop_table("artists")
