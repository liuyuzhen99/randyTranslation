"""phase3 catalog schema

Revision ID: 20260419_120000
Revises: 20260415_220500
Create Date: 2026-04-19 12:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260419_120000"
down_revision = "20260415_220500"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("artists") as batch_op:
        batch_op.add_column(
            sa.Column("sync_status", sa.String(length=32), nullable=False, server_default="pending")
        )
        batch_op.add_column(sa.Column("last_sync_started_at", sa.DateTime(timezone=False), nullable=True))
        batch_op.add_column(sa.Column("last_sync_completed_at", sa.DateTime(timezone=False), nullable=True))
        batch_op.add_column(sa.Column("last_sync_error", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("last_channel_resolved_at", sa.DateTime(timezone=False), nullable=True)
        )
        batch_op.add_column(sa.Column("last_discovery_at", sa.DateTime(timezone=False), nullable=True))

    op.create_table(
        "artist_sync_runs",
        sa.Column("run_id", sa.String(length=128), primary_key=True, nullable=False),
        sa.Column("spotify_id", sa.String(length=128), nullable=True),
        sa.Column("source_kind", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("discovered_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trigger", sa.String(length=32), nullable=False, server_default="system"),
        sa.ForeignKeyConstraint(["spotify_id"], ["artists.spotify_id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "retry_count >= 0",
            name="ck_artist_sync_runs_retry_count_non_negative",
        ),
    )
    op.create_index(
        "ix_artist_sync_runs_spotify_id_started_at",
        "artist_sync_runs",
        ["spotify_id", "started_at"],
    )
    op.create_index("ix_artist_sync_runs_status", "artist_sync_runs", ["status"])

    op.create_table(
        "video_candidates",
        sa.Column("candidate_id", sa.String(length=128), primary_key=True, nullable=False),
        sa.Column("spotify_id", sa.String(length=128), nullable=False),
        sa.Column("video_id", sa.String(length=128), nullable=False),
        sa.Column("channel_id", sa.String(length=255), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.String(length=64), nullable=False, server_default="youtube_rss"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending_review"),
        sa.Column("ingestion_status", sa.String(length=32), nullable=False, server_default="completed"),
        sa.Column("published_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("discovery_run_id", sa.String(length=128), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["spotify_id"], ["artists.spotify_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["discovery_run_id"], ["artist_sync_runs.run_id"], ondelete="SET NULL"),
        sa.UniqueConstraint("spotify_id", "video_id", name="uq_video_candidates_artist_video"),
    )
    op.create_index(
        "ix_video_candidates_spotify_id_published_at",
        "video_candidates",
        ["spotify_id", "published_at"],
    )
    op.create_index("ix_video_candidates_status", "video_candidates", ["status"])


def downgrade() -> None:
    op.drop_index("ix_video_candidates_status", table_name="video_candidates")
    op.drop_index("ix_video_candidates_spotify_id_published_at", table_name="video_candidates")
    op.drop_table("video_candidates")

    op.drop_index("ix_artist_sync_runs_status", table_name="artist_sync_runs")
    op.drop_index("ix_artist_sync_runs_spotify_id_started_at", table_name="artist_sync_runs")
    op.drop_table("artist_sync_runs")

    with op.batch_alter_table("artists") as batch_op:
        batch_op.drop_column("last_discovery_at")
        batch_op.drop_column("last_channel_resolved_at")
        batch_op.drop_column("last_sync_error")
        batch_op.drop_column("last_sync_completed_at")
        batch_op.drop_column("last_sync_started_at")
        batch_op.drop_column("sync_status")
