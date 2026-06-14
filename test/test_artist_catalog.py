import os
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect

import api.service as api_service
from application.services.artist_catalog_service import (
    ArtistListFilters,
    CandidateDiscoveryPayload,
    ArtistCatalogProviders,
)
from domain.entities import Artist
from domain.enums import CandidateStatus, SyncStatus
from infrastructure.persistence.sqlalchemy_repositories import (
    SQLAlchemyArtistRepository,
    SQLAlchemyArtistSyncRunRepository,
    SQLAlchemyCandidateRepository,
    SQLAlchemySessionFactory,
)


class ArtistCatalogTests(unittest.TestCase):
    PROJECT_ROOT = Path(__file__).resolve().parents[1]

    def test_catalog_resync_persists_run_and_candidates(self):
        with TemporaryDirectory() as temp_root:
            session_factory = SQLAlchemySessionFactory(f"sqlite:///{os.path.join(temp_root, 'artist-catalog.db')}")
            session_factory.create_schema()
            artist_repository = SQLAlchemyArtistRepository(session_factory)
            artist_repository.upsert(Artist(spotify_id="artist-1", name="Kendrick Lamar"))

            service = api_service.create_artist_catalog_service(
                providers=ArtistCatalogProviders(
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
                        "DATABASE_URL": f"sqlite:///{os.path.join(temp_root, 'artist-catalog.db')}",
                        "DATABASE_AUTO_CREATE_SCHEMA": "true",
                "VECTOR_REPOSITORY_BACKEND": "sqlite",
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

    def test_catalog_resync_marks_artist_failed_when_provider_fails(self):
        with TemporaryDirectory() as temp_root:
            session_factory = SQLAlchemySessionFactory(f"sqlite:///{os.path.join(temp_root, 'artist-catalog-failed.db')}")
            session_factory.create_schema()
            artist_repository = SQLAlchemyArtistRepository(session_factory)
            artist_repository.upsert(Artist(spotify_id="artist-fail", name="Failing Artist"))

            service = api_service.create_artist_catalog_service(
                providers=ArtistCatalogProviders(
                    followed_artists_lookup=lambda: [],
                    channel_lookup=lambda artist: "UC_FAILING",
                    candidate_lookup=lambda artist, days: (_ for _ in ()).throw(RuntimeError("rss unavailable")),
                ),
                runtime_settings=api_service.load_runtime_settings(
                    {
                        "JOB_REPOSITORY_BACKEND": "sqlalchemy",
                        "DATABASE_URL": f"sqlite:///{os.path.join(temp_root, 'artist-catalog-failed.db')}",
                        "DATABASE_AUTO_CREATE_SCHEMA": "true",
                "VECTOR_REPOSITORY_BACKEND": "sqlite",
                    }
                ),
                session_factory=session_factory,
            )

            with self.assertRaisesRegex(RuntimeError, "rss unavailable"):
                service.resync_artist("artist-fail", days=14, trigger="manual")

            refreshed_artist = artist_repository.get("artist-fail")
            self.assertEqual(refreshed_artist.sync_status, SyncStatus.FAILED)
            self.assertEqual(refreshed_artist.last_sync_error, "rss unavailable")
            self.assertIsNotNone(refreshed_artist.last_sync_completed_at)

    def test_catalog_resync_rejects_active_processing_artist(self):
        with TemporaryDirectory() as temp_root:
            session_factory = SQLAlchemySessionFactory(f"sqlite:///{os.path.join(temp_root, 'artist-catalog-lock.db')}")
            session_factory.create_schema()
            artist_repository = SQLAlchemyArtistRepository(session_factory)
            artist_repository.upsert(
                Artist(
                    spotify_id="artist-lock",
                    name="Locked Artist",
                    sync_status=SyncStatus.PROCESSING,
                    last_sync_started_at=datetime(2026, 5, 20, 10, 0, 0),
                )
            )

            service = api_service.create_artist_catalog_service(
                providers=ArtistCatalogProviders(
                    followed_artists_lookup=lambda: [],
                    channel_lookup=lambda artist: "UC_LOCKED",
                    candidate_lookup=lambda artist, days: [],
                ),
                runtime_settings=api_service.load_runtime_settings(
                    {
                        "JOB_REPOSITORY_BACKEND": "sqlalchemy",
                        "DATABASE_URL": f"sqlite:///{os.path.join(temp_root, 'artist-catalog-lock.db')}",
                        "DATABASE_AUTO_CREATE_SCHEMA": "true",
                "VECTOR_REPOSITORY_BACKEND": "sqlite",
                        "ARTIST_SYNC_STALE_AFTER_SECONDS": "999999999",
                    }
                ),
                session_factory=session_factory,
            )

            with self.assertRaisesRegex(RuntimeError, "Artist sync already in progress"):
                service.resync_artist("artist-lock", days=14, trigger="manual")

            runs = SQLAlchemyArtistSyncRunRepository(session_factory).list_for_artist("artist-lock")
            candidates = SQLAlchemyCandidateRepository(session_factory).list_for_artist("artist-lock")
            refreshed_artist = artist_repository.get("artist-lock")
            self.assertEqual(len(runs), 0)
            self.assertEqual(len(candidates), 0)
            self.assertEqual(refreshed_artist.sync_status, SyncStatus.PROCESSING)
            self.assertEqual(refreshed_artist.last_sync_started_at, datetime(2026, 5, 20, 10, 0, 0))

    def test_catalog_resync_can_take_over_stale_processing_artist(self):
        with TemporaryDirectory() as temp_root:
            session_factory = SQLAlchemySessionFactory(f"sqlite:///{os.path.join(temp_root, 'artist-catalog-stale.db')}")
            session_factory.create_schema()
            artist_repository = SQLAlchemyArtistRepository(session_factory)
            artist_repository.upsert(
                Artist(
                    spotify_id="artist-stale",
                    name="Stale Artist",
                    sync_status=SyncStatus.PROCESSING,
                    last_sync_started_at=datetime(2020, 1, 1, 0, 0, 0),
                )
            )

            service = api_service.create_artist_catalog_service(
                providers=ArtistCatalogProviders(
                    followed_artists_lookup=lambda: [],
                    channel_lookup=lambda artist: "UC_STALE",
                    candidate_lookup=lambda artist, days: [],
                ),
                runtime_settings=api_service.load_runtime_settings(
                    {
                        "JOB_REPOSITORY_BACKEND": "sqlalchemy",
                        "DATABASE_URL": f"sqlite:///{os.path.join(temp_root, 'artist-catalog-stale.db')}",
                        "DATABASE_AUTO_CREATE_SCHEMA": "true",
                "VECTOR_REPOSITORY_BACKEND": "sqlite",
                        "ARTIST_SYNC_STALE_AFTER_SECONDS": "1",
                    }
                ),
                session_factory=session_factory,
            )

            result = service.resync_artist("artist-stale", days=14, trigger="manual")

            refreshed_artist = artist_repository.get("artist-stale")
            self.assertEqual(result["status"], "completed")
            self.assertEqual(refreshed_artist.sync_status, SyncStatus.COMPLETED)
            self.assertEqual(refreshed_artist.yt_channel_id, "UC_STALE")
            self.assertGreater(refreshed_artist.last_sync_started_at, datetime(2020, 1, 1, 0, 0, 0))

    def test_artist_finish_sync_does_not_overwrite_newer_processing_run(self):
        with TemporaryDirectory() as temp_root:
            session_factory = SQLAlchemySessionFactory(f"sqlite:///{os.path.join(temp_root, 'artist-catalog-token.db')}")
            session_factory.create_schema()
            artist_repository = SQLAlchemyArtistRepository(session_factory)
            old_started_at = datetime(2026, 5, 20, 10, 0, 0)
            new_started_at = datetime(2026, 5, 20, 10, 5, 0)
            artist_repository.upsert(
                Artist(
                    spotify_id="artist-token",
                    name="Token Artist",
                    sync_status=SyncStatus.PROCESSING,
                    last_sync_started_at=new_started_at,
                )
            )

            finished = Artist(
                spotify_id="artist-token",
                name="Token Artist Old",
                yt_channel_id="UC_OLD",
                sync_status=SyncStatus.COMPLETED,
                last_sync_started_at=old_started_at,
                last_sync_completed_at=datetime(2026, 5, 20, 10, 1, 0),
            )

            self.assertFalse(artist_repository.try_finish_sync("artist-token", old_started_at, finished))
            refreshed_artist = artist_repository.get("artist-token")
            self.assertEqual(refreshed_artist.sync_status, SyncStatus.PROCESSING)
            self.assertEqual(refreshed_artist.last_sync_started_at, new_started_at)
            self.assertIsNone(refreshed_artist.yt_channel_id)

    def test_catalog_resync_raises_when_successful_result_loses_finish_token(self):
        with TemporaryDirectory() as temp_root:
            session_factory = SQLAlchemySessionFactory(f"sqlite:///{os.path.join(temp_root, 'artist-catalog-stale-result.db')}")
            session_factory.create_schema()
            artist_repository = SQLAlchemyArtistRepository(session_factory)
            artist_repository.upsert(Artist(spotify_id="artist-stale-result", name="Stale Result Artist"))
            takeover_started_at = datetime(2026, 5, 20, 10, 5, 0)

            def candidate_lookup(artist, days):
                artist_repository.upsert(
                    Artist(
                        spotify_id=artist.spotify_id,
                        name=artist.name,
                        yt_channel_id=artist.yt_channel_id,
                        status=artist.status,
                        sync_status=SyncStatus.PROCESSING,
                        last_sync_started_at=takeover_started_at,
                    )
                )
                return [
                    CandidateDiscoveryPayload(
                        video_id="video-stale-result",
                        title="stale result video",
                        source_url="https://youtube.test/watch?v=video-stale-result",
                        published_at=datetime(2026, 4, 18, 10, 0, 0),
                    )
                ]

            service = api_service.create_artist_catalog_service(
                providers=ArtistCatalogProviders(
                    followed_artists_lookup=lambda: [],
                    channel_lookup=lambda artist: "UC_STALE_RESULT",
                    candidate_lookup=candidate_lookup,
                ),
                runtime_settings=api_service.load_runtime_settings(
                    {
                        "JOB_REPOSITORY_BACKEND": "sqlalchemy",
                        "DATABASE_URL": f"sqlite:///{os.path.join(temp_root, 'artist-catalog-stale-result.db')}",
                        "DATABASE_AUTO_CREATE_SCHEMA": "true",
                "VECTOR_REPOSITORY_BACKEND": "sqlite",
                    }
                ),
                session_factory=session_factory,
            )

            with self.assertRaisesRegex(RuntimeError, "Artist sync result is stale"):
                service.resync_artist("artist-stale-result", days=14, trigger="manual")

            refreshed_artist = artist_repository.get("artist-stale-result")
            candidates = SQLAlchemyCandidateRepository(session_factory).list_for_artist("artist-stale-result")
            self.assertEqual(refreshed_artist.sync_status, SyncStatus.PROCESSING)
            self.assertEqual(refreshed_artist.last_sync_started_at, takeover_started_at)
            self.assertEqual(len(candidates), 1)

    def test_list_artists_supports_candidate_and_sync_sorting(self):
        with TemporaryDirectory() as temp_root:
            db_path = os.path.join(temp_root, "artist-catalog-sort.db")
            session_factory = SQLAlchemySessionFactory(f"sqlite:///{db_path}")
            session_factory.create_schema()
            artist_repository = SQLAlchemyArtistRepository(session_factory)
            artist_repository.upsert(
                Artist(
                    spotify_id="artist-1",
                    name="Zulu Artist",
                    sync_status=SyncStatus.COMPLETED,
                    last_sync_completed_at=datetime(2026, 4, 20, 10, 0, 0),
                )
            )
            artist_repository.upsert(
                Artist(
                    spotify_id="artist-2",
                    name="Alpha Artist",
                    sync_status=SyncStatus.FAILED,
                    last_sync_completed_at=datetime(2026, 4, 19, 10, 0, 0),
                )
            )

            service = api_service.create_artist_catalog_service(
                providers=ArtistCatalogProviders(
                    followed_artists_lookup=lambda: [],
                    channel_lookup=lambda artist: artist.yt_channel_id or f"UC_{artist.spotify_id.upper()}",
                    candidate_lookup=lambda artist, days: [
                        CandidateDiscoveryPayload(
                            video_id=f"{artist.spotify_id}-video-{index}",
                            title=f"{artist.name} drop {index}",
                            source_url=f"https://youtube.test/watch?v={artist.spotify_id}-{index}",
                            published_at=datetime(2026, 4, 18, 12, 0, 0),
                        )
                        for index in range(2 if artist.spotify_id == "artist-1" else 1)
                    ],
                ),
                runtime_settings=api_service.load_runtime_settings(
                    {
                        "JOB_REPOSITORY_BACKEND": "sqlalchemy",
                        "DATABASE_URL": f"sqlite:///{db_path}",
                        "DATABASE_AUTO_CREATE_SCHEMA": "true",
                "VECTOR_REPOSITORY_BACKEND": "sqlite",
                    }
                ),
                session_factory=session_factory,
            )

            service.resync_artist("artist-1", trigger="manual")
            service.resync_artist("artist-2", trigger="manual")
            artist_two = artist_repository.get("artist-2")
            artist_repository.upsert(
                Artist(
                    spotify_id="artist-2",
                    name="Alpha Artist",
                    yt_channel_id=artist_two.yt_channel_id if artist_two else None,
                    status="active",
                    sync_status=SyncStatus.FAILED,
                    last_sync_started_at=artist_two.last_sync_started_at if artist_two else None,
                    last_sync_completed_at=artist_two.last_sync_completed_at if artist_two else None,
                    last_sync_error="sync failed",
                    last_channel_resolved_at=artist_two.last_channel_resolved_at if artist_two else None,
                    last_discovery_at=artist_two.last_discovery_at if artist_two else None,
                )
            )

            default_items, _ = service.list_artists(ArtistListFilters())
            self.assertEqual(default_items[0]["artist_id"], "artist-1")
            self.assertEqual([item["artist_id"] for item in default_items[:2]], ["artist-1", "artist-2"])
            self.assertEqual(default_items[0]["candidate_count"], 2)

            candidate_repository = SQLAlchemyCandidateRepository(session_factory)
            first_candidate = candidate_repository.list_for_artist("artist-1")[0]
            first_candidate.status = CandidateStatus.PENDING_REVIEW
            candidate_repository.upsert(first_candidate)

            updated_items, _ = service.list_artists(ArtistListFilters(sort="candidate_count_desc"))
            updated_artist_one = next(item for item in updated_items if item["artist_id"] == "artist-1")
            self.assertEqual(updated_artist_one["candidate_count"], 1)

            sync_sorted_items, _ = service.list_artists(ArtistListFilters(sort="sync_status_asc"))
            self.assertEqual(sync_sorted_items[0]["artist_id"], "artist-2")

    def test_v1_artists_endpoints_expose_catalog_contract(self):
        with TemporaryDirectory() as temp_root:
            env = {
                "DEEPSEEK_API_KEY": "test-key",
                "DEEPSEEK_BASE_URL": "https://example.local",
                "JOB_REPOSITORY_BACKEND": "sqlalchemy",
                "DATABASE_URL": f"sqlite:///{os.path.join(temp_root, 'artist-catalog.db')}",
                "DATABASE_AUTO_CREATE_SCHEMA": "true",
                "VECTOR_REPOSITORY_BACKEND": "sqlite",
            }
            providers = ArtistCatalogProviders(
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
                app = api_service.create_app(artist_catalog_providers=providers)
                artist_repository = SQLAlchemyArtistRepository(app.state.session_factory)
                artist_repository.upsert(Artist(spotify_id="artist-1", name="Doechii"))
                artist_repository.upsert(Artist(spotify_id="artist-2", name="Little Simz"))
                app.state.artist_catalog_service.resync_artist("artist-1", trigger="manual")
                app.state.artist_catalog_service.resync_artist("artist-2", trigger="manual")

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
            self.assertEqual(candidates_payload["items"][0]["status"], "discovered")

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
                "DATABASE_URL": f"sqlite:///{os.path.join(temp_root, 'artist-catalog.db')}",
                "DATABASE_AUTO_CREATE_SCHEMA": "true",
                "VECTOR_REPOSITORY_BACKEND": "sqlite",
            }

            with patch.dict(os.environ, env, clear=False):
                app = api_service.create_app(
                    artist_catalog_providers=ArtistCatalogProviders(
                        followed_artists_lookup=lambda: [],
                        channel_lookup=lambda artist: "UC_NONE",
                        candidate_lookup=lambda artist, days: [],
                    )
                )
                with TestClient(app) as client:
                    response = client.get("/v1/artists/missing/candidates")

            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["error"]["code"], "artist_not_found")
            self.assertEqual(response.json()["error"]["message"], "Artist not found")

    def test_v1_artist_resync_returns_409_when_sync_in_progress(self):
        with TemporaryDirectory() as temp_root:
            env = {
                "DEEPSEEK_API_KEY": "test-key",
                "DEEPSEEK_BASE_URL": "https://example.local",
                "JOB_REPOSITORY_BACKEND": "sqlalchemy",
                "DATABASE_URL": f"sqlite:///{os.path.join(temp_root, 'artist-catalog.db')}",
                "DATABASE_AUTO_CREATE_SCHEMA": "true",
                "VECTOR_REPOSITORY_BACKEND": "sqlite",
                "ARTIST_SYNC_STALE_AFTER_SECONDS": "999999999",
            }

            with patch.dict(os.environ, env, clear=False):
                app = api_service.create_app(
                    artist_catalog_providers=ArtistCatalogProviders(
                        followed_artists_lookup=lambda: [],
                        channel_lookup=lambda artist: "UC_BUSY",
                        candidate_lookup=lambda artist, days: [],
                    )
                )
                artist_repository = SQLAlchemyArtistRepository(app.state.session_factory)
                artist_repository.upsert(
                    Artist(
                        spotify_id="artist-busy",
                        name="Busy Artist",
                        sync_status=SyncStatus.PROCESSING,
                        last_sync_started_at=datetime(2026, 5, 20, 10, 0, 0),
                    )
                )
                with TestClient(app) as client:
                    response = client.post("/v1/artists/artist-busy/resync")

            self.assertEqual(response.status_code, 409)
            payload = response.json()
            self.assertEqual(payload["error"]["code"], "artist_sync_already_in_progress")
            self.assertEqual(payload["error"]["message"], "Artist sync already in progress")

    def test_alembic_head_creates_artist_catalog_tables(self):
        with TemporaryDirectory() as temp_root:
            db_path = os.path.join(temp_root, "artist-catalog-migration.db")
            alembic_cfg = Config(str(self.PROJECT_ROOT / "alembic.ini"))
            alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
            alembic_cfg.set_main_option(
                "script_location",
                str(self.PROJECT_ROOT / "alembic"),
            )
            with patch.dict(os.environ, {"DATABASE_URL": f"sqlite:///{db_path}"}, clear=False):
                command.upgrade(alembic_cfg, "head")

            inspector = inspect(create_engine(f"sqlite:///{db_path}", future=True))
            tables = set(inspector.get_table_names())

            self.assertTrue({"artist_sync_runs", "video_candidates"}.issubset(tables))
            artist_columns = {column["name"] for column in inspector.get_columns("artists")}
            self.assertIn("sync_status", artist_columns)
            self.assertIn("last_sync_completed_at", artist_columns)

    def test_internal_artist_catalog_sync_and_batch_refresh_endpoints(self):
        with TemporaryDirectory() as temp_root:
            env = {
                "DEEPSEEK_API_KEY": "test-key",
                "DEEPSEEK_BASE_URL": "https://example.local",
                "JOB_REPOSITORY_BACKEND": "sqlalchemy",
                "DATABASE_URL": f"sqlite:///{os.path.join(temp_root, 'artist-catalog.db')}",
                "DATABASE_AUTO_CREATE_SCHEMA": "true",
                "VECTOR_REPOSITORY_BACKEND": "sqlite",
            }
            providers = ArtistCatalogProviders(
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
                app = api_service.create_app(artist_catalog_providers=providers)
                with TestClient(app) as client:
                    sync_response = client.post("/internal/artist-catalog/spotify/sync-followed-artists")
                    refresh_response = client.post(
                        "/internal/artist-catalog/resync-active-artists",
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

    def test_sync_followed_artists_marks_unfollowed_artists_inactive(self):
        with TemporaryDirectory() as temp_root:
            session_factory = SQLAlchemySessionFactory(f"sqlite:///{os.path.join(temp_root, 'artist-catalog.db')}")
            session_factory.create_schema()
            artist_repository = SQLAlchemyArtistRepository(session_factory)
            artist_repository.upsert(
                Artist(
                    spotify_id="artist-keep",
                    name="Doechii",
                    status="active",
                    sync_status=SyncStatus.COMPLETED,
                )
            )
            artist_repository.upsert(
                Artist(
                    spotify_id="artist-drop",
                    name="A.M.",
                    status="active",
                    sync_status=SyncStatus.COMPLETED,
                )
            )

            service = api_service.create_artist_catalog_service(
                providers=ArtistCatalogProviders(
                    followed_artists_lookup=lambda: [Artist(spotify_id="artist-keep", name="Doechii")],
                    channel_lookup=lambda artist: artist.yt_channel_id,
                    candidate_lookup=lambda artist, days: [],
                ),
                runtime_settings=api_service.load_runtime_settings(
                    {
                        "JOB_REPOSITORY_BACKEND": "sqlalchemy",
                        "DATABASE_URL": f"sqlite:///{os.path.join(temp_root, 'artist-catalog.db')}",
                        "DATABASE_AUTO_CREATE_SCHEMA": "true",
                "VECTOR_REPOSITORY_BACKEND": "sqlite",
                    }
                ),
                session_factory=session_factory,
            )

            result = service.sync_followed_artists(trigger="manual")

            self.assertEqual(result["synced_count"], 1)
            self.assertEqual(artist_repository.get("artist-keep").status, "active")
            self.assertEqual(artist_repository.get("artist-drop").status, "inactive")

            items, total = service.list_artists(ArtistListFilters())
            self.assertEqual(total, 1)
            self.assertEqual(items[0]["artist_id"], "artist-keep")


if __name__ == "__main__":
    unittest.main()
