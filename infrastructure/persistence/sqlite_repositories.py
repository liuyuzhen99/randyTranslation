from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Iterable, Optional

from domain.entities import Artist, Job, OutboxEvent, Subtitle, VectorRecord, Video
from domain.enums import JobStatus
from domain.repositories import (
    ArtistRepository,
    JobRepository,
    OutboxRepository,
    SubtitleRepository,
    VectorRepository,
    VideoRepository,
)


class _SQLiteRepositoryMixin:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._ensure_tables()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _ensure_tables(self) -> None:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS artists (
                    spotify_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    yt_channel_id TEXT,
                    status TEXT DEFAULT 'active'
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS videos (
                    video_id TEXT PRIMARY KEY,
                    spotify_id TEXT,
                    title TEXT NOT NULL,
                    published_at TEXT,
                    processed_status TEXT DEFAULT 'new',
                    local_video_path TEXT,
                    srt_path TEXT,
                    final_video_path TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS subtitles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT NOT NULL,
                    line_index INTEGER NOT NULL,
                    start_time REAL NOT NULL,
                    end_time REAL NOT NULL,
                    en_text TEXT NOT NULL,
                    zh_text TEXT,
                    status TEXT DEFAULT 'raw'
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    song_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress TEXT NOT NULL,
                    result TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox (
                    event_id TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS vectors (
                    vector_id TEXT PRIMARY KEY,
                    namespace TEXT NOT NULL,
                    text TEXT NOT NULL,
                    metadata_json TEXT DEFAULT '{}'
                )
                """
            )


class SQLiteArtistRepository(_SQLiteRepositoryMixin, ArtistRepository):
    def upsert(self, artist: Artist) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO artists (spotify_id, name, yt_channel_id, status)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(spotify_id) DO UPDATE SET
                    name=excluded.name,
                    yt_channel_id=excluded.yt_channel_id,
                    status=excluded.status
                """,
                (artist.spotify_id, artist.name, artist.yt_channel_id, artist.status),
            )

    def get(self, spotify_id: str) -> Optional[Artist]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT spotify_id, name, yt_channel_id, status FROM artists WHERE spotify_id=?",
                (spotify_id,),
            ).fetchone()
            if not row:
                return None
            return Artist(**dict(row))


class SQLiteVideoRepository(_SQLiteRepositoryMixin, VideoRepository):
    def upsert(self, video: Video) -> None:
        published_at = video.published_at.isoformat() if video.published_at else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO videos (
                    video_id, spotify_id, title, published_at, processed_status,
                    local_video_path, srt_path, final_video_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    spotify_id=excluded.spotify_id,
                    title=excluded.title,
                    published_at=excluded.published_at,
                    processed_status=excluded.processed_status,
                    local_video_path=excluded.local_video_path,
                    srt_path=excluded.srt_path,
                    final_video_path=excluded.final_video_path
                """,
                (
                    video.video_id,
                    video.spotify_id,
                    video.title,
                    published_at,
                    video.processed_status,
                    video.local_video_path,
                    video.srt_path,
                    video.final_video_path,
                ),
            )

    def get(self, video_id: str) -> Optional[Video]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM videos WHERE video_id=?", (video_id,)).fetchone()
            if not row:
                return None
            data = dict(row)
            if data.get("published_at"):
                data["published_at"] = datetime.fromisoformat(data["published_at"])
            return Video(**data)


class SQLiteSubtitleRepository(_SQLiteRepositoryMixin, SubtitleRepository):
    def replace_for_video(self, video_id: str, subtitles: Iterable[Subtitle]) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM subtitles WHERE video_id=?", (video_id,))
            conn.executemany(
                """
                INSERT INTO subtitles (
                    video_id, line_index, start_time, end_time, en_text, zh_text, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        subtitle.video_id,
                        subtitle.line_index,
                        subtitle.start_time,
                        subtitle.end_time,
                        subtitle.en_text,
                        subtitle.zh_text,
                        subtitle.status,
                    )
                    for subtitle in subtitles
                ],
            )

    def list_for_video(self, video_id: str) -> list[Subtitle]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT video_id, line_index, start_time, end_time, en_text, zh_text, status
                FROM subtitles
                WHERE video_id=?
                ORDER BY line_index ASC
                """,
                (video_id,),
            ).fetchall()
            return [Subtitle(**dict(row)) for row in rows]


class SQLiteJobRepository(_SQLiteRepositoryMixin, JobRepository):
    def create(self, job: Job) -> None:
        self.update(job)

    def get(self, job_id: str) -> Optional[Job]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                return None
            return Job(
                job_id=row["job_id"],
                song_name=row["song_name"],
                status=JobStatus(row["status"]),
                progress=row["progress"],
                result=row["result"],
            )

    def update(self, job: Job) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (job_id, song_name, status, progress, result)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    song_name=excluded.song_name,
                    status=excluded.status,
                    progress=excluded.progress,
                    result=excluded.result
                """,
                (job.job_id, job.song_name, job.status.value, job.progress, job.result),
            )

    def list_all(self) -> dict[str, Job]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM jobs").fetchall()
            return {
                row["job_id"]: Job(
                    job_id=row["job_id"],
                    song_name=row["song_name"],
                    status=JobStatus(row["status"]),
                    progress=row["progress"],
                    result=row["result"],
                )
                for row in rows
            }


class SQLiteOutboxRepository(_SQLiteRepositoryMixin, OutboxRepository):
    def add(self, event: OutboxEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO outbox (event_id, topic, payload, status)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(event_id) DO NOTHING
                """,
                (event.event_id, event.topic, event.payload, event.status),
            )

    def list_pending(self) -> list[OutboxEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT event_id, topic, payload, status FROM outbox WHERE status='pending'"
            ).fetchall()
            return [OutboxEvent(**dict(row)) for row in rows]

    def get(self, event_id: str) -> Optional[OutboxEvent]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT event_id, topic, payload, status FROM outbox WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if row is None:
                return None
            return OutboxEvent(**dict(row))

    def update(self, event: OutboxEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO outbox (event_id, topic, payload, status)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    topic=excluded.topic,
                    payload=excluded.payload,
                    status=excluded.status
                """,
                (event.event_id, event.topic, event.payload, event.status),
            )


class SQLiteVectorRepository(_SQLiteRepositoryMixin, VectorRepository):
    def upsert(self, record: VectorRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO vectors (vector_id, namespace, text, metadata_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(vector_id) DO UPDATE SET
                    namespace=excluded.namespace,
                    text=excluded.text,
                    metadata_json=excluded.metadata_json
                """,
                (record.vector_id, record.namespace, record.text, str(record.metadata)),
            )

    def search(self, namespace: str, text: str, limit: int = 5) -> list[VectorRecord]:
        # Temporary adapter: lexical LIKE search to preserve abstraction until Qdrant migration.
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT vector_id, namespace, text, metadata_json
                FROM vectors
                WHERE namespace=? AND text LIKE ?
                ORDER BY vector_id ASC
                LIMIT ?
                """,
                (namespace, f"%{text}%", limit),
            ).fetchall()
            return [
                VectorRecord(
                    vector_id=row["vector_id"],
                    namespace=row["namespace"],
                    text=row["text"],
                    metadata={"raw": row["metadata_json"]},
                )
                for row in rows
            ]
