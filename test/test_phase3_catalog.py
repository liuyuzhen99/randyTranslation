import os
import unittest
from datetime import datetime, timedelta
from tempfile import TemporaryDirectory
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect

import api.service as api_service
from application.services.phase3_catalog_service import CandidateDiscoveryPayload, Phase3Providers
from domain.entities import Artist
from domain.enums import SyncStatus
from infrastructure.persistence.sqlalchemy_repositories import (
    SQLAlchemyArtistRepository,
    SQLAlchemyArtistSyncRunRepository,
    SQLAlchemyCandidateRepository,
    SQLAlchemySessionFactory,
)


class Phase3CatalogTests(unittest.TestCase):
    def test_catalog_resync_persists_run_and_candidates(self):
        with TemporaryDirectory() as temp_root:
            session_factory = SQLAlchemySessionFactory(f"sqlite:///{os.path.join(temp_root, 'phase3.db')}")
            session_factory.create_schema()
            artist_repository = SQLAlchemyArtistRepository(session_factory)
            artist_repository.upsert(Artist(spotify_id="artist-1", name="Kendrick Lamar"))

            service = api_service.create_phase3_catalog_service(
                providers=Phase3Providers(
                    followed_artists_lookup=lambda: [],
                    channel_lookup=lambda artist: "UC_TEST_CHANNEL",
                    candidate_lookup=lambda artist, days: [
                        CandidateDiscoveryPayload(
                            video_id="video-1",
                            title="squabble up (Official Video)",
                            source_url="https://youtube.test/watch?v=video-1",
                            published_at=datetime(2026, 4, 18, 10, 0, 0),
                        ),
                        CandidateDiscoveryPayload(
                            video_id="video-2",
                            title="tv off (Official Video)",
                            source_url="https://youtube.test/watch?v=video-2",
                            published_at=datetime(2026, 4, 17, 9, 0, 0),
                        ),
                    ],
                ),
                runtime_settings=api_service.load_runtime_settings(
                    {
                        "JOB_REPOSITORY_BACKEND": "sqlalchemy",
                        "DATABASE_URL": f"sqlite:///{os.path.join(temp_root, 'phase3.db')}",
                        "PHASE2_AUTO_CREATE_SCHEMA": "true",
                    }
                ),
                session_factory=session_factory,
            )

            result = service.resync_artist("artist-1", days=14, trigger="manual")

            refreshed_artist = artist_repository.get("artist-1")
            runs = SQLAlchemyArtistSyncRunRepository(session_factory).list_for_artist("artist-1")
            candidates = SQLAlchemyCandidateRepository(session_factory).list_for_artist("artist-1")

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["discovered_count"], 2)
            self.assertEqual(refreshed_artist.yt_channel_id, "UC_TEST_CHANNEL")
            self.assertEqual(refreshed_artist.sync_status, SyncStatus.COMPLETED)
            self.assertEqual(len(runs), 2)
            self.assertEqual({run.source_kind for run in runs}, {"youtube_channel", "youtube_rss"})
            self.assertEqual(next(run for run in runs if run.source_kind == "youtube_rss").discovered_count, 2)
            self.assertEqual(len(candidates), 2)
            self.assertEqual(candidates[0].video_id, "video-1")

    def test_v1_artists_endpoints_expose_catalog_contract(self):
        with TemporaryDirectory() as temp_root:
            env = {
                "DEEPSEEK_API_KEY": "test-key",
                "DEEPSEEK_BASE_URL": "https://example.local",
                "JOB_REPOSITORY_BACKEND": "sqlalchemy",
                "DATABASE_URL": f"sqlite:///{os.path.join(temp_root, 'phase3.db')}",
                "PHASE2_AUTO_CREATE_SCHEMA": "true",
            }
            providers = Phase3Providers(
                followed_artists_lookup=lambda: [],
                channel_lookup=lambda artist: artist.yt_channel_id or "UC_ARTIST_ONE",
                candidate_lookup=lambda artist, days: [
                    CandidateDiscoveryPayload(
                        video_id=f"{artist.spotify_id}-video-{index}",
                        title=f"{artist.name} official drop {index}",
                        source_url=f"https://youtube.test/watch?v={artist.spotify_id}-{index}",
                        published_at=datetime(2026, 4, 18, 12, 0, 0) - timedelta(days=index),
                    )
                    for index in range(2)
                ],
            )

            with patch.dict(os.environ, env, clear=False):
                app = api_service.create_app(phase3_providers=providers)
                artist_repository = SQLAlchemyArtistRepository(app.state.session_factory)
                artist_repository.upsert(Artist(spotify_id="artist-1", name="Doechii"))
                artist_repository.upsert(Artist(spotify_id="artist-2", name="Little Simz"))
                app.state.phase3_catalog_service.resync_artist("artist-1", trigger="manual")
                app.state.phase3_catalog_service.resync_artist("artist-2", trigger="manual")

                with TestClient(app) as client:
                    artists_response = client.get("/v1/artists", params={"page": 1, "page_size": 1})
                    candidates_response = client.get("/v1/artists/artist-1/candidates")
                    resync_response = client.post("/v1/artists/artist-1/resync", params={"days": 7})

            self.assertEqual(artists_response.status_code, 200)
            artists_payload = artists_response.json()
            self.assertEqual(
                artists_payload["pagination"],
                {"page": 1, "page_size": 1, "total": 2, "total_pages": 2},
            )
            self.assertIn("generated_at", artists_payload["meta"])
            self.assertEqual(len(artists_payload["items"]), 1)
            self.assertIn("latest_candidate", artists_payload["items"][0])
            self.assertIn("latest_run", artists_payload["items"][0])
            self.assertIn("source_health", artists_payload["items"][0])
            self.assertIn("retry_metadata", artists_payload["items"][0])

            self.assertEqual(candidates_response.status_code, 200)
            candidates_payload = candidates_response.json()
            self.assertEqual(candidates_payload["artist_id"], "artist-1")
            self.assertEqual(candidates_payload["pagination"]["total"], 2)
            self.assertEqual(candidates_payload["pagination"]["total_pages"], 1)
            self.assertEqual(candidates_payload["items"][0]["status"], "pending_review")

            self.assertEqual(resync_response.status_code, 200)
            resync_payload = resync_response.json()
            self.assertEqual(resync_payload["status"], "completed")
            self.assertIn("channel_run_id", resync_payload)
            self.assertIn("discovery_run_id", resync_payload)

    def test_v1_candidates_returns_404_for_unknown_artist(self):
        with TemporaryDirectory() as temp_root:
            env = {
                "DEEPSEEK_API_KEY": "test-key",
                "DEEPSEEK_BASE_URL": "https://example.local",
                "JOB_REPOSITORY_BACKEND": "sqlalchemy",
                "DATABASE_URL": f"sqlite:///{os.path.join(temp_root, 'phase3.db')}",
                "PHASE2_AUTO_CREATE_SCHEMA": "true",
            }

            with patch.dict(os.environ, env, clear=False):
                app = api_service.create_app(
                    phase3_providers=Phase3Providers(
                        followed_artists_lookup=lambda: [],
                        channel_lookup=lambda artist: "UC_NONE",
                        candidate_lookup=lambda artist, days: [],
                    )
                )
                with TestClient(app) as client:
                    response = client.get("/v1/artists/missing/candidates")

            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json(), {"detail": "Artist not found"})

    def test_alembic_head_creates_phase3_catalog_tables(self):
        with TemporaryDirectory() as temp_root:
            db_path = os.path.join(temp_root, "phase3-migration.db")
            alembic_cfg = Config("alembic.ini")
            alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
            with patch.dict(os.environ, {"DATABASE_URL": f"sqlite:///{db_path}"}, clear=False):
                command.upgrade(alembic_cfg, "head")

            inspector = inspect(create_engine(f"sqlite:///{db_path}", future=True))
            tables = set(inspector.get_table_names())

            self.assertTrue({"artist_sync_runs", "video_candidates"}.issubset(tables))
            artist_columns = {column["name"] for column in inspector.get_columns("artists")}
            self.assertIn("sync_status", artist_columns)
            self.assertIn("last_sync_completed_at", artist_columns)

    def test_internal_phase3_sync_and_batch_refresh_endpoints(self):
        with TemporaryDirectory() as temp_root:
            env = {
                "DEEPSEEK_API_KEY": "test-key",
                "DEEPSEEK_BASE_URL": "https://example.local",
                "JOB_REPOSITORY_BACKEND": "sqlalchemy",
                "DATABASE_URL": f"sqlite:///{os.path.join(temp_root, 'phase3.db')}",
                "PHASE2_AUTO_CREATE_SCHEMA": "true",
            }
            providers = Phase3Providers(
                followed_artists_lookup=lambda: [
                    Artist(spotify_id="artist-1", name="Doechii"),
                    Artist(spotify_id="artist-2", name="Little Simz"),
                ],
                channel_lookup=lambda artist: f"UC_{artist.spotify_id.upper()}",
                candidate_lookup=lambda artist, days: [
                    CandidateDiscoveryPayload(
                        video_id=f"{artist.spotify_id}-video-1",
                        title=f"{artist.name} official drop",
                        source_url=f"https://youtube.test/watch?v={artist.spotify_id}-1",
                        published_at=datetime(2026, 4, 18, 12, 0, 0),
                    )
                ],
            )

            with patch.dict(os.environ, env, clear=False):
                app = api_service.create_app(phase3_providers=providers)
                with TestClient(app) as client:
                    sync_response = client.post("/internal/phase3/spotify/sync-followed-artists")
                    refresh_response = client.post(
                        "/internal/phase3/catalog/resync-active-artists",
                        params={"days": 14, "limit": 2},
                    )
                    artists_response = client.get("/v1/artists", params={"sort": "last_synced_desc"})

            self.assertEqual(sync_response.status_code, 200)
            self.assertEqual(sync_response.json()["synced_count"], 2)

            self.assertEqual(refresh_response.status_code, 200)
            self.assertEqual(refresh_response.json()["requested"], 2)
            self.assertEqual(refresh_response.json()["refreshed"], 2)
            self.assertEqual(refresh_response.json()["failed"], 0)

            self.assertEqual(artists_response.status_code, 200)
            items = artists_response.json()["items"]
            self.assertEqual(len(items), 2)
            self.assertIn("spotify_following", items[0]["source_health"])
            self.assertIn("youtube_channel", items[0]["source_health"])
            self.assertIn("youtube_rss", items[0]["source_health"])


if __name__ == "__main__":
    unittest.main()
