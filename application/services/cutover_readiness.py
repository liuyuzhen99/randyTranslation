from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from domain.time_utils import utc_now


@dataclass(frozen=True)
class EntitySnapshot:
    entity: str
    keys: set[str]
    payloads: dict[str, dict] = field(default_factory=dict, compare=False, hash=False)

    @classmethod
    def from_iterable(cls, entity: str, keys) -> "EntitySnapshot":
        return cls(entity=entity, keys={str(key) for key in keys})

    @classmethod
    def from_records(cls, entity: str, records: dict[str, dict]) -> "EntitySnapshot":
        return cls(entity=entity, keys=set(records.keys()), payloads=records)


@dataclass(frozen=True)
class EntityParityReport:
    entity: str
    legacy_count: int
    target_count: int
    missing_in_target: list[str] = field(default_factory=list)
    extra_in_target: list[str] = field(default_factory=list)
    field_mismatches: list[dict] = field(default_factory=list, compare=False, hash=False)

    @property
    def is_consistent(self) -> bool:
        return not self.missing_in_target and not self.extra_in_target and not self.field_mismatches

    def to_dict(self) -> dict:
        return {
            "entity": self.entity,
            "legacy_count": self.legacy_count,
            "target_count": self.target_count,
            "missing_in_target": self.missing_in_target,
            "extra_in_target": self.extra_in_target,
            "field_mismatches": self.field_mismatches,
            "is_consistent": self.is_consistent,
        }


@dataclass(frozen=True)
class CutoverParityReport:
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


class CutoverReconciliationService:
    def compare_snapshots(
        self,
        *,
        legacy_snapshots: dict[str, EntitySnapshot],
        target_snapshots: dict[str, EntitySnapshot],
    ) -> CutoverParityReport:
        entity_names = sorted(set(legacy_snapshots) | set(target_snapshots))
        reports: list[EntityParityReport] = []
        for entity_name in entity_names:
            legacy = legacy_snapshots.get(entity_name, EntitySnapshot(entity_name, set()))
            target = target_snapshots.get(entity_name, EntitySnapshot(entity_name, set()))
            missing = sorted(legacy.keys - target.keys)
            extra = sorted(target.keys - legacy.keys)
            field_mismatches = self._compare_payloads(legacy, target)
            reports.append(
                EntityParityReport(
                    entity=entity_name,
                    legacy_count=len(legacy.keys),
                    target_count=len(target.keys),
                    missing_in_target=missing,
                    extra_in_target=extra,
                    field_mismatches=field_mismatches,
                )
            )
        return CutoverParityReport(generated_at=utc_now().isoformat(), entities=reports)

    @staticmethod
    def _compare_payloads(
        legacy: EntitySnapshot,
        target: EntitySnapshot,
    ) -> list[dict]:
        if not legacy.payloads and not target.payloads:
            return []
        common_keys = legacy.keys & target.keys
        mismatches: list[dict] = []
        for key in sorted(common_keys):
            legacy_payload = legacy.payloads.get(key, {})
            target_payload = target.payloads.get(key, {})
            all_fields = set(legacy_payload) | set(target_payload)
            for field_name in sorted(all_fields):
                legacy_val = legacy_payload.get(field_name)
                target_val = target_payload.get(field_name)
                if legacy_val != target_val:
                    mismatches.append({
                        "key": key,
                        "field": field_name,
                        "legacy": legacy_val,
                        "target": target_val,
                    })
        return mismatches


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
class CutoverShadowTrafficReport:
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


class CutoverShadowTrafficValidator:
    def compare(
        self,
        *,
        cases: dict[str, tuple[Callable[[], object], Callable[[], object]]],
        normalizer: Callable[[object], object] | None = None,
    ) -> CutoverShadowTrafficReport:
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
        return CutoverShadowTrafficReport(generated_at=utc_now().isoformat(), cases=results)

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
class CutoverReadinessReport:
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


class CutoverReadinessService:
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
        parity_report: CutoverParityReport | None = None,
        shadow_report: CutoverShadowTrafficReport | None = None,
    ) -> CutoverReadinessReport:
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
        return CutoverReadinessReport(
            generated_at=utc_now().isoformat(),
            read_source=self.read_source,
            stability_window_days=self.stability_window_days,
            gates=gates,
        )
