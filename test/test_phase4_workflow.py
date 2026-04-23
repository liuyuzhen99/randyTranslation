import os
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect

import api.service as api_service
from application.services.phase3_catalog_service import CandidateDiscoveryPayload, Phase3Providers
from infrastructure.persistence.sqlalchemy_repositories import SQLAlchemySubtitleRepository


class Phase4WorkflowTests(unittest.TestCase):
    PROJECT_ROOT = Path(__file__).resolve().parents[1]

    def _create_app(self, temp_root: str):
        env = {
            "DEEPSEEK_API_KEY": "test-key",
            "DEEPSEEK_BASE_URL": "https://example.local",
            "JOB_REPOSITORY_BACKEND": "sqlalchemy",
            "DATABASE_URL": f"sqlite:///{os.path.join(temp_root, 'phase4.db')}",
            "PHASE2_AUTO_CREATE_SCHEMA": "true",
        }
        providers = Phase3Providers(
            followed_artists_lookup=lambda: [],
            channel_lookup=lambda artist: artist.yt_channel_id or "UC_PHASE4",
            candidate_lookup=lambda artist, days: [
                CandidateDiscoveryPayload(
                    video_id="video-phase4-1",
                    title="Phase 4 Official Video",
                    source_url="https://youtube.test/watch?v=video-phase4-1",
                    published_at=datetime(2026, 4, 20, 10, 0, 0),
                )
            ],
        )
        patcher = patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        app = api_service.create_app(phase3_providers=providers)
        app.state.phase3_catalog_service.sync_followed_artists(trigger="manual")
        if app.state.phase3_catalog_service.artist_repository.get("artist-1") is None:
            from domain.entities import Artist

            app.state.phase3_catalog_service.artist_repository.upsert(
                Artist(spotify_id="artist-1", name="Doechii")
            )
        app.state.phase3_catalog_service.resync_artist("artist-1", trigger="manual")
        return app

    def test_phase4_review_workflow_endpoints(self):
        with TemporaryDirectory() as temp_root:
            app = self._create_app(temp_root)

            with TestClient(app) as client:
                audit_queue = client.get("/v1/audit-queue")
                self.assertEqual(audit_queue.status_code, 200)
                queue_payload = audit_queue.json()
                self.assertEqual(queue_payload["pagination"]["total"], 1)
                self.assertEqual(queue_payload["meta"]["update_mode"], "polling")
                review_id = queue_payload["items"][0]["review_id"]
                candidate_id = queue_payload["items"][0]["candidate_id"]
                self.assertEqual(queue_payload["items"][0]["review_type"], "transcript_review")

                stale_attempt = client.post(
                    f"/v1/reviews/{review_id}/approve",
                    json={"expected_version": 0, "comment": "stale"},
                )
                self.assertEqual(stale_attempt.status_code, 409)
                self.assertEqual(
                    stale_attempt.json()["error"]["code"],
                    "stale_review_version_expected_0_current_1",
                )

                expected_types = (
                    "transcript_review",
                    "taste_audit",
                    "manual_review",
                    "translation_review",
                    "final_asset_approval",
                )
                for expected_type in expected_types:
                    queue_payload = client.get("/v1/audit-queue").json()
                    current_item = next(
                        item for item in queue_payload["items"] if item["review_type"] == expected_type
                    )
                    approve_response = client.post(
                        f"/v1/reviews/{current_item['review_id']}/approve",
                        headers={"X-Actor-Id": "frontend-tester"},
                        json={"expected_version": current_item["version"], "comment": "approved"},
                    )
                    self.assertEqual(approve_response.status_code, 200)

                queue_after_all = client.get("/v1/audit-queue").json()
                self.assertEqual(queue_after_all["pagination"]["total"], 1)
                self.assertEqual(queue_after_all["items"][0]["status"], "approved")
                self.assertEqual(queue_after_all["items"][0]["review_type"], "final_asset_approval")

                pending_queue_after_all = client.get("/v1/audit-queue", params={"status": "pending"}).json()
                self.assertEqual(pending_queue_after_all["pagination"]["total"], 0)

                pipeline = client.get("/v1/pipeline")
                self.assertEqual(pipeline.status_code, 200)
                pipeline_item = pipeline.json()["items"][0]
                self.assertEqual(pipeline_item["workflow_status"], "accepted")
                self.assertEqual(pipeline_item["current_stage"], "completed")
                self.assertEqual(pipeline_item["translation"]["status"], "approved")
                self.assertNotIn("current_owner_role", pipeline_item)

                audit_log = client.get(
                    "/v1/audit-log",
                    params={"aggregate_type": "candidate", "aggregate_id": candidate_id},
                )
                self.assertEqual(audit_log.status_code, 200)
                self.assertGreaterEqual(audit_log.json()["pagination"]["total"], 5)
                self.assertIn(
                    "workflow_promoted",
                    {item["action"] for item in audit_log.json()["items"]},
                )
                self.assertIn(
                    "frontend-tester",
                    {item["actor_id"] for item in audit_log.json()["items"]},
                )

                library_before = client.get("/v1/library")
                self.assertEqual(library_before.status_code, 200)
                self.assertEqual(library_before.json()["pagination"]["total"], 1)
                self.assertEqual(library_before.json()["items"][0]["curation_status"], "accepted")

    def test_phase4_v1_error_envelope_and_request_id(self):
        with TemporaryDirectory() as temp_root:
            app = self._create_app(temp_root)

            with TestClient(app) as client:
                response = client.get(
                    "/v1/audit-log",
                    params={"aggregate_type": "candidate"},
                )
                self.assertEqual(response.status_code, 422)
                self.assertIn("request_id", response.json()["meta"])

    def test_phase4_ai_stage_ingest_endpoints(self):
        with TemporaryDirectory() as temp_root:
            app = self._create_app(temp_root)

            with TestClient(app) as client:
                initial_queue = client.get("/v1/audit-queue").json()
                candidate_id = initial_queue["items"][0]["candidate_id"]

                transcript_response = client.post(
                    f"/v1/candidates/{candidate_id}/transcript",
                    headers={"X-Actor-Id": "ai-reviewer"},
                    json={
                        "segments": [
                            {"start_time": 0.0, "end_time": 2.5, "text": "Sunday again"},
                            {"start_time": 2.5, "end_time": 5.0, "text": "I need your light"},
                        ],
                        "auto_approve_review": True,
                        "comment": "transcript ready",
                    },
                )
                self.assertEqual(transcript_response.status_code, 200)
                self.assertEqual(transcript_response.json()["segment_count"], 2)
                self.assertEqual(transcript_response.json()["next_review_type"], "taste_audit")

                queue_after_transcript = client.get("/v1/audit-queue").json()["items"]
                self.assertEqual(queue_after_transcript[0]["review_type"], "taste_audit")

                taste_audit_response = client.post(
                    f"/v1/candidates/{candidate_id}/taste-audit",
                    headers={"X-Actor-Id": "ai-auditor"},
                    json={
                        "decision": "approved",
                        "score": 0.91,
                        "key_lyrics": ["Sunday again"],
                        "comment": "passes taste filter",
                    },
                )
                self.assertEqual(taste_audit_response.status_code, 200)
                self.assertEqual(taste_audit_response.json()["next_review_type"], "manual_review")

                manual_review = next(
                    item
                    for item in client.get("/v1/audit-queue").json()["items"]
                    if item["review_type"] == "manual_review"
                )
                manual_approve = client.post(
                    f"/v1/reviews/{manual_review['review_id']}/approve",
                    headers={"X-Actor-Id": "manual-tester"},
                    json={"expected_version": manual_review["version"], "comment": "manual pass"},
                )
                self.assertEqual(manual_approve.status_code, 200)
                self.assertEqual(manual_approve.json()["next_review_type"], "translation_review")

                translation_response = client.post(
                    f"/v1/candidates/{candidate_id}/translation",
                    headers={"X-Actor-Id": "ai-translator"},
                    json={
                        "translations": [
                            {"line_index": 0, "zh_text": "又到了星期天"},
                            {"line_index": 1, "zh_text": "我需要你的光"},
                        ],
                        "auto_approve_review": True,
                        "comment": "translation ready",
                    },
                )
                self.assertEqual(translation_response.status_code, 200)
                self.assertEqual(translation_response.json()["line_count"], 2)
                self.assertEqual(
                    translation_response.json()["next_review_type"],
                    "final_asset_approval",
                )

                subtitle_repository = SQLAlchemySubtitleRepository(app.state.session_factory)
                subtitles = subtitle_repository.list_for_video("video-phase4-1")
                self.assertEqual(len(subtitles), 2)
                self.assertEqual(subtitles[0].zh_text, "又到了星期天")
                self.assertEqual(subtitles[1].zh_text, "我需要你的光")

                queue_after_translation = client.get("/v1/audit-queue").json()["items"]
                self.assertEqual(queue_after_translation[0]["review_type"], "final_asset_approval")

                audit_log = client.get(
                    "/v1/audit-log",
                    params={"aggregate_type": "candidate", "aggregate_id": candidate_id},
                )
                self.assertEqual(audit_log.status_code, 200)
                actions = {item["action"] for item in audit_log.json()["items"]}
                self.assertTrue(
                    {"transcript_updated", "taste_audit_recorded", "translation_updated"}.issubset(
                        actions
                    )
                )

    def test_phase4_audit_queue_can_filter_rejected_reviews(self):
        with TemporaryDirectory() as temp_root:
            app = self._create_app(temp_root)

            with TestClient(app) as client:
                queue_payload = client.get("/v1/audit-queue").json()
                review = queue_payload["items"][0]

                reject_response = client.post(
                    f"/v1/reviews/{review['review_id']}/reject",
                    headers={"X-Actor-Id": "frontend-user-1"},
                    json={"expected_version": review["version"], "comment": "not a fit"},
                )
                self.assertEqual(reject_response.status_code, 200)

                pending_queue = client.get("/v1/audit-queue", params={"status": "pending"}).json()
                self.assertEqual(pending_queue["pagination"]["total"], 0)

                rejected_queue = client.get("/v1/audit-queue", params={"status": "rejected"}).json()
                self.assertEqual(rejected_queue["pagination"]["total"], 1)
                self.assertEqual(rejected_queue["items"][0]["status"], "rejected")
                self.assertEqual(rejected_queue["items"][0]["candidate_id"], review["candidate_id"])

                all_queue = client.get("/v1/audit-queue").json()
                self.assertEqual(all_queue["pagination"]["total"], 1)
                self.assertEqual(all_queue["items"][0]["status"], "rejected")

    def test_phase4_alembic_head_creates_workflow_tables(self):
        with TemporaryDirectory() as temp_root:
            db_path = os.path.join(temp_root, "phase4-migration.db")
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
            self.assertTrue({"review_items", "audit_log_entries"}.issubset(tables))


if __name__ == "__main__":
    unittest.main()
