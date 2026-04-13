# Engineering Guardrails (Phase 0)

## Branch Strategy
- `master` is currently the default protected branch (rename target: `main`).
- All changes are made in feature branches (for example: `feature/phase0-baseline-tests`).
- Merges to the default branch happen through pull requests only.

## Required PR Checks
- CI `lint` must pass.
- CI `unit-tests` must pass.
- CI `integration-test-trigger` must pass.

## Review Policy
- At least one approving review before merge.
- No direct pushes to the default branch (`master`/`main`).
- If CI fails, merge is blocked until all required checks are green.
