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
from domain.entities import ArtifactRecord
from domain.enums import CandidateStatus
from infrastructure.persistence.sqlalchemy_repositories import SQLAlchemyCandidateRepository
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
        candidate = app.state.phase3_catalog_service.candidate_repository.list_for_artist("artist-1")[0]
        app.state.phase4_workflow_services.pipeline_service.add_candidate(
            candidate.candidate_id,
            actor_id="test-setup",
        )
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
                    if expected_type == "final_asset_approval":
                        app.state.artifact_repository.upsert(
                            ArtifactRecord(
                                artifact_id=f"candidate:{candidate_id}:final_video:v1",
                                owner_type="candidate",
                                owner_id=candidate_id,
                                artifact_type="final_video",
                                object_uri="local://phase4/final.mp4",
                                object_key="phase4/final.mp4",
                                bucket="phase4-test",
                                storage_provider="local",
                                candidate_id=candidate_id,
                                size_bytes=128,
                                checksum_sha256="phase4-checksum",
                            )
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
                self.assertEqual(pipeline.json()["pagination"]["total"], 0)

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

    def test_final_asset_approval_requires_ready_final_video_artifact(self):
        with TemporaryDirectory() as temp_root:
            app = self._create_app(temp_root)

            with TestClient(app) as client:
                expected_types = (
                    "transcript_review",
                    "taste_audit",
                    "manual_review",
                    "translation_review",
                    "final_asset_approval",
                )
                final_item = None
                for expected_type in expected_types:
                    queue_payload = client.get("/v1/audit-queue").json()
                    current_item = next(
                        item for item in queue_payload["items"] if item["review_type"] == expected_type
                    )
                    if expected_type == "final_asset_approval":
                        final_item = current_item
                        break
                    approve_response = client.post(
                        f"/v1/reviews/{current_item['review_id']}/approve",
                        headers={"X-Actor-Id": "frontend-tester"},
                        json={"expected_version": current_item["version"], "comment": "approved"},
                    )
                    self.assertEqual(approve_response.status_code, 200)

                self.assertIsNotNone(final_item)
                blocked_response = client.post(
                    f"/v1/reviews/{final_item['review_id']}/approve",
                    headers={"X-Actor-Id": "frontend-tester"},
                    json={"expected_version": final_item["version"], "comment": "approved"},
                )
                self.assertEqual(blocked_response.status_code, 409)
                self.assertEqual(
                    blocked_response.json()["error"]["message"],
                    "Final asset approval requires a ready final_video artifact.",
                )

    def test_candidate_can_be_manually_added_to_pipeline(self):
        with TemporaryDirectory() as temp_root:
            app = self._create_app(temp_root)

            with TestClient(app) as client:
                initial_queue = client.get("/v1/audit-queue").json()
                candidate_id = initial_queue["items"][0]["candidate_id"]
                candidate_repository = SQLAlchemyCandidateRepository(app.state.session_factory)
                candidate = candidate_repository.get(candidate_id)
                self.assertIsNotNone(candidate)
                candidate.status = CandidateStatus.DISCOVERED
                candidate_repository.upsert(candidate)

                response = client.post(
                    f"/v1/candidates/{candidate_id}/pipeline",
                    headers={"X-Actor-Id": "frontend-tester"},
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["candidate_status"], "downloading")
                self.assertEqual(response.json()["review_type"], "transcript_review")

    def test_render_job_is_reused_and_visible_in_pipeline(self):
        class StubCommandService:
            def __init__(self):
                self.enqueued_job_ids = []

            def enqueue_first_stage(self, job, candidate_id=None):
                self.enqueued_job_ids.append((job.job_id, candidate_id))

        with TemporaryDirectory() as temp_root:
            app = self._create_app(temp_root)
            command_service = StubCommandService()
            app.state.phase6_async_pipeline_services = (command_service, None)

            with TestClient(app) as client:
                initial_queue = client.get("/v1/audit-queue").json()
                candidate_id = initial_queue["items"][0]["candidate_id"]

                first_response = client.post(f"/v1/candidates/{candidate_id}/render")
                second_response = client.post(f"/v1/candidates/{candidate_id}/render")

                self.assertEqual(first_response.status_code, 200)
                self.assertEqual(second_response.status_code, 200)
                self.assertEqual(first_response.json()["task_id"], second_response.json()["task_id"])
                self.assertEqual(len(command_service.enqueued_job_ids), 1)

                pipeline_item = client.get("/v1/pipeline").json()["items"][0]
                self.assertEqual(pipeline_item["candidate_id"], candidate_id)
                self.assertEqual(pipeline_item["render_job"]["job_id"], first_response.json()["task_id"])
                self.assertEqual(pipeline_item["render_job"]["status"], "pending")

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

                detail_response = client.get(f"/v1/candidates/{candidate_id}/workflow-detail")
                self.assertEqual(detail_response.status_code, 200)
                detail = detail_response.json()
                self.assertEqual(detail["transcript"]["segment_count"], 2)
                self.assertEqual(detail["transcript"]["segments"][0]["text"], "Sunday again")
                self.assertEqual(detail["taste_audit"]["score"], 0.91)
                self.assertEqual(detail["taste_audit"]["key_lyrics"], ["Sunday again"])
                self.assertEqual(detail["translation"]["line_count"], 2)
                self.assertEqual(detail["translation"]["lines"][0]["translated_text"], "又到了星期天")

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
            self.assertTrue({"review_items", "audit_log_entries", "artifacts"}.issubset(tables))


if __name__ == "__main__":
    unittest.main()
