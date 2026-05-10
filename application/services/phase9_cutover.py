from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from domain.time_utils import utc_now


@dataclass(frozen=True)
class EntitySnapshot:
    entity: str
    keys: set[str]

    @classmethod
    def from_iterable(cls, entity: str, keys) -> "EntitySnapshot":
        return cls(entity=entity, keys={str(key) for key in keys})


@dataclass(frozen=True)
class EntityParityReport:
    entity: str
    legacy_count: int
    target_count: int
    missing_in_target: list[str] = field(default_factory=list)
    extra_in_target: list[str] = field(default_factory=list)

    @property
    def is_consistent(self) -> bool:
        return not self.missing_in_target and not self.extra_in_target

    def to_dict(self) -> dict:
        return {
            "entity": self.entity,
            "legacy_count": self.legacy_count,
            "target_count": self.target_count,
            "missing_in_target": self.missing_in_target,
            "extra_in_target": self.extra_in_target,
            "is_consistent": self.is_consistent,
        }


@dataclass(frozen=True)
class Phase9ParityReport:
    generated_at: str
    entities: list[EntityParityReport]

    @property
    def is_consistent(self) -> bool:
        return all(entity.is_consistent for entity in self.entities)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "is_consistent": self.is_consistent,
            "entities": [entity.to_dict() for entity in self.entities],
        }


class Phase9ReconciliationService:
    def compare_snapshots(
        self,
        *,
        legacy_snapshots: dict[str, EntitySnapshot],
        target_snapshots: dict[str, EntitySnapshot],
    ) -> Phase9ParityReport:
        entity_names = sorted(set(legacy_snapshots) | set(target_snapshots))
        reports: list[EntityParityReport] = []
        for entity_name in entity_names:
            legacy = legacy_snapshots.get(entity_name, EntitySnapshot(entity_name, set()))
            target = target_snapshots.get(entity_name, EntitySnapshot(entity_name, set()))
            missing = sorted(legacy.keys - target.keys)
            extra = sorted(target.keys - legacy.keys)
            reports.append(
                EntityParityReport(
                    entity=entity_name,
                    legacy_count=len(legacy.keys),
                    target_count=len(target.keys),
                    missing_in_target=missing,
                    extra_in_target=extra,
                )
            )
        return Phase9ParityReport(generated_at=utc_now().isoformat(), entities=reports)


@dataclass(frozen=True)
class ShadowTrafficCaseResult:
    name: str
    legacy_latency_ms: float
    target_latency_ms: float
    legacy_success: bool
    target_success: bool
    output_match: bool
    mismatch_reason: str = ""

    @property
    def passed(self) -> bool:
        return self.legacy_success and self.target_success and self.output_match

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "legacy_latency_ms": round(self.legacy_latency_ms, 3),
            "target_latency_ms": round(self.target_latency_ms, 3),
            "legacy_success": self.legacy_success,
            "target_success": self.target_success,
            "output_match": self.output_match,
            "mismatch_reason": self.mismatch_reason,
            "passed": self.passed,
        }


@dataclass(frozen=True)
class Phase9ShadowTrafficReport:
    generated_at: str
    cases: list[ShadowTrafficCaseResult]

    @property
    def passed(self) -> bool:
        return all(case.passed for case in self.cases)

    @property
    def success_rate(self) -> float:
        if not self.cases:
            return 0.0
        return len([case for case in self.cases if case.passed]) / len(self.cases)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "passed": self.passed,
            "success_rate": self.success_rate,
            "cases": [case.to_dict() for case in self.cases],
        }


class Phase9ShadowTrafficValidator:
    def compare(
        self,
        *,
        cases: dict[str, tuple[Callable[[], object], Callable[[], object]]],
        normalizer: Callable[[object], object] | None = None,
    ) -> Phase9ShadowTrafficReport:
        normalize = normalizer or (lambda value: value)
        results: list[ShadowTrafficCaseResult] = []
        for name, (legacy_call, target_call) in cases.items():
            legacy_success, legacy_value, legacy_latency, legacy_error = self._run(legacy_call)
            target_success, target_value, target_latency, target_error = self._run(target_call)
            output_match = False
            mismatch_reason = ""
            if legacy_success and target_success:
                output_match = normalize(legacy_value) == normalize(target_value)
                if not output_match:
                    mismatch_reason = "normalized_output_mismatch"
            else:
                mismatch_reason = legacy_error or target_error or "request_failed"
            results.append(
                ShadowTrafficCaseResult(
                    name=name,
                    legacy_latency_ms=legacy_latency,
                    target_latency_ms=target_latency,
                    legacy_success=legacy_success,
                    target_success=target_success,
                    output_match=output_match,
                    mismatch_reason=mismatch_reason,
                )
            )
        return Phase9ShadowTrafficReport(generated_at=utc_now().isoformat(), cases=results)

    @staticmethod
    def _run(callable_obj: Callable[[], object]) -> tuple[bool, object | None, float, str]:
        start = time.perf_counter()
        try:
            value = callable_obj()
            return True, value, (time.perf_counter() - start) * 1000.0, ""
        except Exception as exc:
            return False, None, (time.perf_counter() - start) * 1000.0, str(exc)


@dataclass(frozen=True)
class CutoverGateResult:
    name: str
    passed: bool
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "details": self.details}


@dataclass(frozen=True)
class Phase9CutoverReadinessReport:
    generated_at: str
    read_source: str
    stability_window_days: int
    gates: list[CutoverGateResult]

    @property
    def ready_for_cutover(self) -> bool:
        return all(gate.passed for gate in self.gates)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "read_source": self.read_source,
            "stability_window_days": self.stability_window_days,
            "ready_for_cutover": self.ready_for_cutover,
            "gates": [gate.to_dict() for gate in self.gates],
        }


class Phase9CutoverReadinessService:
    def __init__(
        self,
        *,
        read_source: str,
        schema_freeze_enabled: bool,
        rollback_enabled: bool,
        stability_window_days: int,
    ) -> None:
        self.read_source = read_source
        self.schema_freeze_enabled = schema_freeze_enabled
        self.rollback_enabled = rollback_enabled
        self.stability_window_days = stability_window_days

    def evaluate(
        self,
        *,
        dual_write_report: dict | None = None,
        parity_report: Phase9ParityReport | None = None,
        shadow_report: Phase9ShadowTrafficReport | None = None,
    ) -> Phase9CutoverReadinessReport:
        gates = [
            CutoverGateResult(
                "schema_freeze",
                self.schema_freeze_enabled,
                {"required": True},
            ),
            CutoverGateResult(
                "rollback_window",
                self.rollback_enabled,
                {"required": "rollback must remain enabled during cutover"},
            ),
            CutoverGateResult(
                "dual_write",
                bool(dual_write_report and dual_write_report.get("is_within_threshold")),
                dual_write_report or {"reason": "dual-write report unavailable"},
            ),
            CutoverGateResult(
                "entity_parity",
                bool(parity_report and parity_report.is_consistent),
                parity_report.to_dict() if parity_report else {"reason": "parity report unavailable"},
            ),
            CutoverGateResult(
                "shadow_traffic",
                bool(shadow_report and shadow_report.passed),
                shadow_report.to_dict() if shadow_report else {"reason": "shadow traffic report unavailable"},
            ),
        ]
        return Phase9CutoverReadinessReport(
            generated_at=utc_now().isoformat(),
            read_source=self.read_source,
            stability_window_days=self.stability_window_days,
            gates=gates,
        )
