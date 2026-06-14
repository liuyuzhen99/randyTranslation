from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from application.services.cutover_readiness import (
    EntitySnapshot,
    CutoverReadinessService,
    CutoverReconciliationService,
    CutoverShadowTrafficReport,
    ShadowTrafficCaseResult,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="cutover readiness report.")
    parser.add_argument("--legacy-snapshot", required=True)
    parser.add_argument("--target-snapshot", required=True)
    parser.add_argument("--dual-write-report")
    parser.add_argument("--shadow-report")
    parser.add_argument("--read-source", default="legacy")
    parser.add_argument("--schema-freeze", action="store_true")
    parser.add_argument("--rollback-enabled", action="store_true")
    parser.add_argument("--stability-window-days", type=int, default=7)
    args = parser.parse_args()

    parity_report = CutoverReconciliationService().compare_snapshots(
        legacy_snapshots=_load_snapshots(args.legacy_snapshot),
        target_snapshots=_load_snapshots(args.target_snapshot),
    )
    dual_write_report = _load_json(args.dual_write_report) if args.dual_write_report else None
    shadow_report = _load_shadow_report(args.shadow_report) if args.shadow_report else None
    readiness = CutoverReadinessService(
        read_source=args.read_source,
        schema_freeze_enabled=args.schema_freeze,
        rollback_enabled=args.rollback_enabled,
        stability_window_days=args.stability_window_days,
    ).evaluate(
        dual_write_report=dual_write_report,
        parity_report=parity_report,
        shadow_report=shadow_report,
    )
    payload = {
        "parity": parity_report.to_dict(),
        "shadow_traffic": shadow_report.to_dict() if shadow_report else None,
        "cutover_readiness": readiness.to_dict(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if readiness.ready_for_cutover else 1


def _load_snapshots(path: str) -> dict[str, EntitySnapshot]:
    payload = _load_json(path)
    result: dict[str, EntitySnapshot] = {}
    for entity, value in payload.items():
        if isinstance(value, dict):
            # Record format: {"jobs": {"job-1": {"status": "done", ...}, ...}}
            result[entity] = EntitySnapshot.from_records(entity, value)
        else:
            # Key-list format: {"jobs": ["job-1", "job-2"]}
            result[entity] = EntitySnapshot.from_iterable(entity, value)
    return result


def _load_shadow_report(path: str) -> CutoverShadowTrafficReport:
    payload = _load_json(path)
    return CutoverShadowTrafficReport(
        generated_at=payload["generated_at"],
        cases=[
            ShadowTrafficCaseResult(
                name=item["name"],
                legacy_latency_ms=float(item["legacy_latency_ms"]),
                target_latency_ms=float(item["target_latency_ms"]),
                legacy_success=bool(item["legacy_success"]),
                target_success=bool(item["target_success"]),
                output_match=bool(item["output_match"]),
                mismatch_reason=item.get("mismatch_reason", ""),
            )
            for item in payload.get("cases", [])
        ],
    )


def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as file_obj:
        return json.load(file_obj)


if __name__ == "__main__":
    raise SystemExit(main())
