import json
import os
import subprocess
import sys
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from application.services.cutover_readiness import (
    EntitySnapshot,
    CutoverReadinessService,
    CutoverReconciliationService,
    CutoverShadowTrafficValidator,
)
import api.service as api_service


class CutoverReadinessTests(unittest.TestCase):
    def test_reconciliation_reports_count_and_key_mismatches(self):
        report = CutoverReconciliationService().compare_snapshots(
            legacy_snapshots={
                "artists": EntitySnapshot.from_iterable("artists", ["a1", "a2"]),
                "videos": EntitySnapshot.from_iterable("videos", ["v1"]),
            },
            target_snapshots={
                "artists": EntitySnapshot.from_iterable("artists", ["a1", "a3"]),
                "reviews": EntitySnapshot.from_iterable("reviews", ["r1"]),
            },
        )

        payload = report.to_dict()
        by_entity = {item["entity"]: item for item in payload["entities"]}

        self.assertFalse(report.is_consistent)
        self.assertEqual(by_entity["artists"]["legacy_count"], 2)
        self.assertEqual(by_entity["artists"]["target_count"], 2)
        self.assertEqual(by_entity["artists"]["missing_in_target"], ["a2"])
        self.assertEqual(by_entity["artists"]["extra_in_target"], ["a3"])
        self.assertEqual(by_entity["videos"]["missing_in_target"], ["v1"])
        self.assertEqual(by_entity["reviews"]["extra_in_target"], ["r1"])

    def test_reconciliation_reports_field_level_mismatches(self):
        report = CutoverReconciliationService().compare_snapshots(
            legacy_snapshots={
                "jobs": EntitySnapshot.from_records("jobs", {
                    "j1": {"status": "done", "result": "s3://bucket/v1.mp4"},
                    "j2": {"status": "failed", "result": None},
                }),
            },
            target_snapshots={
                "jobs": EntitySnapshot.from_records("jobs", {
                    "j1": {"status": "done", "result": "s3://bucket/v1.mp4"},
                    "j2": {"status": "done", "result": "s3://bucket/v2.mp4"},
                }),
            },
        )

        payload = report.to_dict()
        jobs_report = next(e for e in payload["entities"] if e["entity"] == "jobs")
        self.assertFalse(report.is_consistent)
        self.assertEqual(len(jobs_report["field_mismatches"]), 2)
        mismatch_by_field = {m["field"]: m for m in jobs_report["field_mismatches"]}
        self.assertEqual(mismatch_by_field["result"]["key"], "j2")
        self.assertIsNone(mismatch_by_field["result"]["legacy"])
        self.assertEqual(mismatch_by_field["result"]["target"], "s3://bucket/v2.mp4")
        self.assertEqual(mismatch_by_field["status"]["legacy"], "failed")
        self.assertEqual(mismatch_by_field["status"]["target"], "done")

    def test_reconciliation_no_field_mismatches_when_payloads_match(self):
        report = CutoverReconciliationService().compare_snapshots(
            legacy_snapshots={
                "artists": EntitySnapshot.from_records("artists", {
                    "a1": {"name": "Kendrick", "followers": 1000},
                }),
            },
            target_snapshots={
                "artists": EntitySnapshot.from_records("artists", {
                    "a1": {"name": "Kendrick", "followers": 1000},
                }),
            },
        )
        self.assertTrue(report.is_consistent)
        artists_report = next(e for e in report.to_dict()["entities"] if e["entity"] == "artists")
        self.assertEqual(artists_report["field_mismatches"], [])

    def test_from_iterable_snapshots_have_no_payloads_and_skip_field_comparison(self):
        report = CutoverReconciliationService().compare_snapshots(
            legacy_snapshots={"jobs": EntitySnapshot.from_iterable("jobs", ["j1", "j2"])},
            target_snapshots={"jobs": EntitySnapshot.from_iterable("jobs", ["j1", "j2"])},
        )
        self.assertTrue(report.is_consistent)
        jobs_report = next(e for e in report.to_dict()["entities"] if e["entity"] == "jobs")
        self.assertEqual(jobs_report["field_mismatches"], [])

    def test_shadow_traffic_validator_compares_normalized_outputs(self):
        report = CutoverShadowTrafficValidator().compare(
            cases={
                "pipeline-list": (
                    lambda: {"items": [{"id": "job-1"}], "generated_at": "legacy"},
                    lambda: {"items": [{"id": "job-1"}], "generated_at": "target"},
                )
            },
            normalizer=lambda payload: payload["items"],
        )

        self.assertTrue(report.passed)
        self.assertEqual(report.success_rate, 1.0)
        self.assertTrue(report.cases[0].output_match)

    def test_shadow_traffic_validator_records_target_failure(self):
        def fail():
            raise RuntimeError("target unavailable")

        report = CutoverShadowTrafficValidator().compare(
            cases={"artists": (lambda: {"ok": True}, fail)}
        )

        self.assertFalse(report.passed)
        self.assertFalse(report.cases[0].target_success)
        self.assertIn("target unavailable", report.cases[0].mismatch_reason)

    def test_cutover_readiness_requires_all_gates(self):
        parity_report = CutoverReconciliationService().compare_snapshots(
            legacy_snapshots={"jobs": EntitySnapshot.from_iterable("jobs", ["j1"])},
            target_snapshots={"jobs": EntitySnapshot.from_iterable("jobs", ["j1"])},
        )
        shadow_report = CutoverShadowTrafficValidator().compare(
            cases={"jobs": (lambda: {"j1": "ok"}, lambda: {"j1": "ok"})}
        )

        report = CutoverReadinessService(
            read_source="legacy",
            schema_freeze_enabled=True,
            rollback_enabled=True,
            stability_window_days=7,
        ).evaluate(
            dual_write_report={"is_within_threshold": True},
            parity_report=parity_report,
            shadow_report=shadow_report,
        )

        self.assertTrue(report.ready_for_cutover)

    def test_cutover_readiness_blocks_when_parity_missing(self):
        report = CutoverReadinessService(
            read_source="legacy",
            schema_freeze_enabled=True,
            rollback_enabled=True,
            stability_window_days=7,
        ).evaluate(dual_write_report={"is_within_threshold": True})

        self.assertFalse(report.ready_for_cutover)
        gate_payload = {gate["name"]: gate for gate in report.to_dict()["gates"]}
        self.assertFalse(gate_payload["entity_parity"]["passed"])
        self.assertFalse(gate_payload["shadow_traffic"]["passed"])

    def test_cutover_report_script_returns_success_for_ready_report(self):
        with TemporaryDirectory() as temp_root:
            legacy_path = f"{temp_root}/legacy.json"
            target_path = f"{temp_root}/target.json"
            dual_write_path = f"{temp_root}/dual.json"
            shadow_path = f"{temp_root}/shadow.json"
            self._write_json(legacy_path, {"artists": ["a1"], "jobs": ["j1"]})
            self._write_json(target_path, {"artists": ["a1"], "jobs": ["j1"]})
            self._write_json(dual_write_path, {"is_within_threshold": True})
            self._write_json(
                shadow_path,
                {
                    "generated_at": "2026-04-29T00:00:00",
                    "cases": [
                        {
                            "name": "artists",
                            "legacy_latency_ms": 1,
                            "target_latency_ms": 1,
                            "legacy_success": True,
                            "target_success": True,
                            "output_match": True,
                        }
                    ],
                },
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/cutover_readiness_report.py",
                    "--legacy-snapshot",
                    legacy_path,
                    "--target-snapshot",
                    target_path,
                    "--dual-write-report",
                    dual_write_path,
                    "--shadow-report",
                    shadow_path,
                    "--schema-freeze",
                    "--rollback-enabled",
                ],
                cwd=".",
                env={"PYTHONPATH": "."},
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["cutover_readiness"]["ready_for_cutover"])

    def test_internal_cutover_readiness_endpoint_reports_blocked_gates(self):
        env = {
            "DEEPSEEK_API_KEY": "test-key",
            "DEEPSEEK_BASE_URL": "https://example.local",
            "JOB_REPOSITORY_BACKEND": "memory",
            "DATABASE_URL": "",
            "SHADOW_WRITE_ENABLED": "false",
            "DUAL_WRITE_RECONCILE_ENABLED": "false",
            "OUTBOX_DISPATCH_ENABLED": "false",
            "ASYNC_PIPELINE_ENABLED": "false",
            "PIPELINE_SERVICE_WORKER_ENABLED": "false",
            "SCHEMA_FREEZE_ENABLED": "true",
            "ROLLBACK_ENABLED": "true",
            "CUTOVER_READ_SOURCE": "legacy",
            "MEDIA_STORAGE_BACKEND": "local",
            "VECTOR_REPOSITORY_BACKEND": "sqlite",
            "QDRANT_URL": "",
            "RABBITMQ_URL": "",
            "POSTGRES_HOST": "",
            "POSTGRES_DB": "",
            "POSTGRES_USER": "",
            "POSTGRES_PASSWORD": "",
        }
        with patch.dict(os.environ, env, clear=False):
            app = api_service.create_app()
            with TestClient(app) as client:
                response = client.get("/internal/cutover/readiness")

        self.assertEqual(response.status_code, 200)
        payload = response.json()["report"]
        self.assertFalse(payload["ready_for_cutover"])
        self.assertEqual(payload["read_source"], "legacy")
        gate_payload = {gate["name"]: gate for gate in payload["gates"]}
        self.assertTrue(gate_payload["schema_freeze"]["passed"])
        self.assertFalse(gate_payload["dual_write"]["passed"])

    @staticmethod
    def _write_json(path: str, payload: dict) -> None:
        with open(path, "w", encoding="utf-8") as file_obj:
            json.dump(payload, file_obj)


if __name__ == "__main__":
    unittest.main()
