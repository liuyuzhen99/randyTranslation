# Phase 9 Summary

## Scope Started

Phase 9 starts the dual-write validation and final cutover readiness layer. This implementation does not remove legacy paths yet; it adds the evidence and guardrails needed before a production read-source switch.

Implemented:

- Generic entity parity reports for legacy vs target snapshots.
- Shadow traffic comparator for legacy/new read paths.
- Cutover readiness gate evaluation.
- Phase 9 runtime controls:
  - `PHASE9_CUTOVER_READ_SOURCE`
  - `PHASE9_SCHEMA_FREEZE_ENABLED`
  - `PHASE9_ROLLBACK_ENABLED`
  - `PHASE9_STABILITY_WINDOW_DAYS`
  - `PHASE9_SHADOW_TRAFFIC_ENABLED`
- Internal cutover readiness endpoint:
  - `GET /internal/phase9/cutover-readiness`
- Cutover report CLI:
  - `scripts/phase9_cutover_report.py`
- Cutover runbook and legacy decommission plan.

## Validation

Commands run:

```bash
PYTHONPATH=. .venv/bin/python test/test_phase9_cutover.py
PYTHONPATH=. .venv/bin/python test/test_phase0_config_validation.py
PYTHONPATH=. .venv/bin/python test/test_phase0_env_template_contract.py
PYTHONPATH=. .venv/bin/python -m py_compile application/services/phase9_cutover.py scripts/phase9_cutover_report.py api/config.py api/service.py
PYTHONPATH=. .venv/bin/python scripts/phase9_cutover_report.py --legacy-snapshot /var/folders/zm/v890ygzs5gg2mb4jsn8qgr1m0000gn/T/phase9-cutover-7t46taqm/legacy.json --target-snapshot /var/folders/zm/v890ygzs5gg2mb4jsn8qgr1m0000gn/T/phase9-cutover-7t46taqm/target.json --dual-write-report /var/folders/zm/v890ygzs5gg2mb4jsn8qgr1m0000gn/T/phase9-cutover-7t46taqm/dual.json --shadow-report /var/folders/zm/v890ygzs5gg2mb4jsn8qgr1m0000gn/T/phase9-cutover-7t46taqm/shadow.json --schema-freeze --rollback-enabled
```

Results:

- Phase 9 cutover tests: 7 passed.
- Config validation tests: 29 passed.
- Env template contract: 1 passed.
- Phase 9 py_compile: passed.
- Phase 9 cutover report CLI passed with synthetic parity/shadow evidence.

## Cutover Gate Semantics

`Phase9CutoverReadinessService` requires all gates to pass:

- `schema_freeze`: schema/interface changes are frozen for the final cutover window.
- `rollback_window`: rollback remains enabled during cutover.
- `dual_write`: Phase 2 dual-write reconcile report is within threshold.
- `entity_parity`: legacy and target key snapshots match.
- `shadow_traffic`: legacy and target read-path outputs match after normalization.

The internal endpoint intentionally returns a blocked report, not a server error, when reconcile evidence is unavailable or fails to run.

## External Limits

- A true 7-day stability window cannot be completed in this coding session.
- Production cutover and legacy decommission are not executed automatically; the implementation provides readiness evidence and rollback-aware controls.
- Frontend joint testing is still required before real cutover because Phase 9 changes product-level read-source behavior.

## Next Work

- Generate real snapshots for artists, videos, subtitles, jobs, reviews, artifacts, and Qdrant vector points.
- Run shadow traffic against real `/v1` screen APIs and legacy endpoints.
- Execute four-screen frontend joint testing before and after read-source switch.
- After stability window, remove legacy SQLite/Chroma write paths and legacy task APIs in a dedicated deprecation PR.
