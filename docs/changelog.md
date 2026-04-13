# Changelog

## 2026-04-13 - Phase 0 Baseline and Safety Setup
- Added baseline API contract tests for `/create_task`, `/check_status/{task_id}`, and `/list_tasks`.
- Added startup environment validation to fail fast when required runtime config is missing.
- Added `.env.example` covering current and target infrastructure variables.
- Added CI workflow for lint, unit tests, and baseline API integration test trigger.
- Added branch strategy and PR guardrails documentation.

### Baseline Tag Note
- Baseline tag should be created in the `randyTranslation` Git repository before Phase 1 work.
