from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from trading.domain.contracts import PolicyOperation, PolicyPatch, TypedCondition
from trading.domain.enums import (
    ComparisonOperator,
    ConditionType,
    PolicyAction,
    PolicyTargetKind,
)
from trading.domain.time import require_aware_utc

MAX_PAPER_RISK_DELTA = 0.15
ALLOWED_SYMBOLS = {
    "SPY",
    "QQQ",
    "IWM",
    "SOXX",
    "SMH",
    "XLK",
    "TLT",
    "HYG",
    "GLD",
}
ALLOWED_FACTORS = {
    "US_EQUITY_BETA",
    "US_TECH_BETA",
    "SEMICONDUCTOR_BETA",
    "DURATION",
    "CREDIT",
    "GOLD_DEFENSIVE",
}
CONDITION_FIELDS: dict[ConditionType, set[str]] = {
    ConditionType.TIME_REACHED: {"current_time"},
    ConditionType.SOURCE_RETRACTION: {"source_id"},
    ConditionType.OFFICIAL_CONFIRMATION: {"event_status"},
    ConditionType.MARKET_FEATURE_THRESHOLD: {
        "QQQ_RETURN_BPS",
        "SOXX_RETURN_BPS",
        "VIX_LEVEL",
    },
    ConditionType.EVENT_STATUS_CHANGED: {"event_status"},
    ConditionType.PATCH_ATTRIBUTED_PNL: {"attributed_pnl_usd"},
    ConditionType.DATA_STALENESS: {"data_age_seconds"},
}


class PolicyCompileError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PolicyState:
    arm_id: str
    version: int
    portfolio_risk_multiplier: float
    strategy_risk_deltas: dict[str, float]
    blocked_targets: frozenset[str]
    active_buckets: frozenset[str]
    source_patch_id: str | None

    @classmethod
    def default(cls, arm_id: str) -> PolicyState:
        return cls(
            arm_id=arm_id,
            version=0,
            portfolio_risk_multiplier=1.0,
            strategy_risk_deltas={},
            blocked_targets=frozenset(),
            active_buckets=frozenset(),
            source_patch_id=None,
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "arm_id": self.arm_id,
            "version": self.version,
            "portfolio_risk_multiplier": self.portfolio_risk_multiplier,
            "strategy_risk_deltas": dict(sorted(self.strategy_risk_deltas.items())),
            "blocked_targets": sorted(self.blocked_targets),
            "active_buckets": sorted(self.active_buckets),
            "source_patch_id": self.source_patch_id,
        }


class PolicyCompiler:
    def compile(
        self,
        patch: PolicyPatch,
        current: PolicyState,
        *,
        now: datetime,
        shadow_mode: bool = True,
    ) -> PolicyState:
        now = require_aware_utc(now, "now")
        if patch.arm_scope != current.arm_id:
            raise PolicyCompileError("Patch arm_scope does not match policy state")
        if patch.base_policy_version != current.version:
            raise PolicyCompileError("Patch base policy version mismatch")
        if not patch.effective_from <= now < patch.expires_at:
            raise PolicyCompileError("Patch is not active at compile time")
        if patch.arm_scope not in {"B3-RISK", "B3-FULL"}:
            raise PolicyCompileError("Only B3 arms accept LLM policy patches")

        for condition in patch.rollback_conditions:
            self._validate_condition(condition)

        multiplier = current.portfolio_risk_multiplier
        deltas = dict(current.strategy_risk_deltas)
        blocked = set(current.blocked_targets)
        buckets = set(current.active_buckets)

        for operation in patch.operations:
            self._validate_operation(patch.arm_scope, operation, shadow_mode=shadow_mode)
            bucket = f"{operation.target_kind.value}:{operation.target_id}"
            if operation.action is PolicyAction.RESTORE_DEFAULT:
                multiplier = 1.0
                deltas.clear()
                blocked.clear()
                buckets.clear()
                continue
            if bucket in buckets:
                raise PolicyCompileError(f"An active patch already controls bucket {bucket}")
            if operation.action is PolicyAction.BLOCK_NEW_ENTRIES:
                blocked.add(bucket)
            if operation.action in {
                PolicyAction.REDUCE_RISK_BUDGET,
                PolicyAction.APPLY_STRATEGY_TILT,
            }:
                if operation.risk_multiplier is not None:
                    multiplier = min(multiplier, operation.risk_multiplier)
                if operation.risk_budget_delta is not None:
                    deltas[operation.target_id] = (
                        deltas.get(operation.target_id, 0.0) + operation.risk_budget_delta
                    )
            buckets.add(bucket)

        return PolicyState(
            arm_id=current.arm_id,
            version=current.version + 1,
            portfolio_risk_multiplier=multiplier,
            strategy_risk_deltas=deltas,
            blocked_targets=frozenset(blocked),
            active_buckets=frozenset(buckets),
            source_patch_id=patch.patch_id,
        )

    def compose(
        self,
        arm_id: str,
        patches: Sequence[PolicyPatch],
        *,
        version: int,
        now: datetime,
        shadow_mode: bool = True,
    ) -> PolicyState:
        """Rebuild the policy effects of all patches active at one point in time."""
        instant = require_aware_utc(now, "now")
        if version < 0:
            raise PolicyCompileError("Policy version cannot be negative")
        default = PolicyState.default(arm_id)
        multiplier = default.portfolio_risk_multiplier
        deltas: dict[str, float] = {}
        blocked: set[str] = set()
        buckets: set[str] = set()
        source_patch_id: str | None = None

        for patch in patches:
            if patch.arm_scope != arm_id:
                raise PolicyCompileError("Patch arm_scope does not match composition")
            if not patch.effective_from <= instant < patch.expires_at:
                raise PolicyCompileError("Policy composition received an inactive patch")
            for condition in patch.rollback_conditions:
                self._validate_condition(condition)
            for operation in patch.operations:
                self._validate_operation(
                    arm_id,
                    operation,
                    shadow_mode=shadow_mode,
                )
                bucket = f"{operation.target_kind.value}:{operation.target_id}"
                if operation.action is PolicyAction.RESTORE_DEFAULT:
                    multiplier = 1.0
                    deltas.clear()
                    blocked.clear()
                    buckets.clear()
                    continue
                if bucket in buckets:
                    raise PolicyCompileError(
                        f"More than one active patch controls bucket {bucket}"
                    )
                if operation.action is PolicyAction.BLOCK_NEW_ENTRIES:
                    blocked.add(bucket)
                if operation.action in {
                    PolicyAction.REDUCE_RISK_BUDGET,
                    PolicyAction.APPLY_STRATEGY_TILT,
                }:
                    if operation.risk_multiplier is not None:
                        multiplier = min(multiplier, operation.risk_multiplier)
                    if operation.risk_budget_delta is not None:
                        deltas[operation.target_id] = (
                            deltas.get(operation.target_id, 0.0)
                            + operation.risk_budget_delta
                        )
                buckets.add(bucket)
            source_patch_id = patch.patch_id

        return PolicyState(
            arm_id=arm_id,
            version=version,
            portfolio_risk_multiplier=multiplier,
            strategy_risk_deltas=deltas,
            blocked_targets=frozenset(blocked),
            active_buckets=frozenset(buckets),
            source_patch_id=source_patch_id,
        )

    def expire(self, current: PolicyState, *, now: datetime, expires_at: datetime) -> PolicyState:
        now = require_aware_utc(now, "now")
        expires_at = require_aware_utc(expires_at, "expires_at")
        if now < expires_at:
            raise PolicyCompileError("Policy cannot be restored before patch expiry")
        restored = PolicyState.default(current.arm_id)
        return PolicyState(
            arm_id=restored.arm_id,
            version=current.version + 1,
            portfolio_risk_multiplier=restored.portfolio_risk_multiplier,
            strategy_risk_deltas=restored.strategy_risk_deltas,
            blocked_targets=restored.blocked_targets,
            active_buckets=restored.active_buckets,
            source_patch_id=None,
        )

    def _validate_condition(self, condition: TypedCondition) -> None:
        allowed = CONDITION_FIELDS.get(condition.condition_type, set())
        if condition.field not in allowed:
            raise PolicyCompileError(
                f"Condition field {condition.field!r} is not allowed for "
                f"{condition.condition_type.value}"
            )

    def _validate_operation(
        self,
        arm_id: str,
        operation: PolicyOperation,
        *,
        shadow_mode: bool,
    ) -> None:
        if arm_id == "B3-RISK" and operation.action not in {
            PolicyAction.BLOCK_NEW_ENTRIES,
            PolicyAction.REDUCE_RISK_BUDGET,
            PolicyAction.RESTORE_DEFAULT,
        }:
            raise PolicyCompileError("B3-RISK cannot apply strategy tilts")
        if arm_id == "B3-RISK":
            self._validate_forward_b3_operation(operation)
        if (
            arm_id == "B3-FULL"
            and operation.action is PolicyAction.APPLY_STRATEGY_TILT
            and not shadow_mode
        ):
            raise PolicyCompileError("B3-FULL strategy tilt is shadow-only")
        if operation.target_kind is PolicyTargetKind.SYMBOL:
            if operation.target_id not in ALLOWED_SYMBOLS:
                raise PolicyCompileError("Symbol is not in the execution universe")
            if operation.action is not PolicyAction.BLOCK_NEW_ENTRIES:
                raise PolicyCompileError("Symbol targets may only block new entries")
        if (
            operation.target_kind is PolicyTargetKind.FACTOR
            and operation.target_id not in ALLOWED_FACTORS
        ):
            raise PolicyCompileError("Factor is not in the fixed registry")
        if operation.risk_budget_delta is not None:
            if abs(operation.risk_budget_delta) > MAX_PAPER_RISK_DELTA:
                raise PolicyCompileError("Risk budget delta exceeds paper limit")
            if arm_id == "B3-RISK" and operation.risk_budget_delta > 0:
                raise PolicyCompileError("B3-RISK cannot increase risk")
        if operation.risk_multiplier is not None and not (
            0.25 <= operation.risk_multiplier <= 1.0
        ):
            raise PolicyCompileError("Paper risk multiplier must be within [0.25, 1.00]")
        if (
            operation.action is PolicyAction.REDUCE_RISK_BUDGET
            and operation.risk_budget_delta is None
            and operation.risk_multiplier is None
        ):
            raise PolicyCompileError("Risk reduction needs a delta or multiplier")

    @staticmethod
    def _validate_forward_b3_operation(operation: PolicyOperation) -> None:
        if operation.action is PolicyAction.REDUCE_RISK_BUDGET:
            if (
                operation.target_kind is not PolicyTargetKind.PORTFOLIO
                or operation.target_id != "TOTAL"
            ):
                raise PolicyCompileError(
                    "B3-RISK risk reduction must target PORTFOLIO:TOTAL"
                )
            if operation.risk_multiplier is None:
                raise PolicyCompileError(
                    "B3-RISK risk reduction requires risk_multiplier"
                )
            if operation.risk_budget_delta is not None:
                raise PolicyCompileError(
                    "B3-RISK forward core does not consume risk_budget_delta"
                )
            return
        if operation.action is PolicyAction.BLOCK_NEW_ENTRIES:
            supported_target = (
                operation.target_kind is PolicyTargetKind.SYMBOL
                and operation.target_id == "QQQ"
            ) or (
                operation.target_kind is PolicyTargetKind.FACTOR
                and operation.target_id
                in {"US_EQUITY_BETA", "US_TECH_BETA"}
            )
            if not supported_target:
                raise PolicyCompileError(
                    "B3-RISK entry block must target QQQ or a consumed equity factor"
                )
            if operation.blocked is False:
                raise PolicyCompileError("BLOCK_NEW_ENTRIES cannot set blocked=false")
            return
        if operation.action is PolicyAction.RESTORE_DEFAULT and (
            operation.target_kind is not PolicyTargetKind.PORTFOLIO
            or operation.target_id != "TOTAL"
        ):
            raise PolicyCompileError(
                "B3-RISK restore must target PORTFOLIO:TOTAL"
            )


class TypedConditionEvaluator:
    def evaluate(self, condition: TypedCondition, context: dict[str, object]) -> bool:
        if condition.field not in CONDITION_FIELDS.get(condition.condition_type, set()):
            raise PolicyCompileError("Condition is not allowlisted")
        actual = context.get(condition.field)
        expected = condition.value
        if condition.condition_type is ConditionType.TIME_REACHED:
            if not isinstance(actual, datetime) or not isinstance(expected, str):
                return False
            expected_time = datetime.fromisoformat(expected.replace("Z", "+00:00"))
            actual = require_aware_utc(actual)
            expected = require_aware_utc(expected_time)
        return _compare(actual, expected, condition.operator)


def _compare(actual: object, expected: object, operator: ComparisonOperator) -> bool:
    if operator is ComparisonOperator.EQ:
        return actual == expected
    try:
        if operator is ComparisonOperator.LT:
            return actual < expected  # type: ignore[operator]
        if operator is ComparisonOperator.LTE:
            return actual <= expected  # type: ignore[operator]
        if operator is ComparisonOperator.GTE:
            return actual >= expected  # type: ignore[operator]
        if operator is ComparisonOperator.GT:
            return actual > expected  # type: ignore[operator]
    except TypeError:
        return False
    return False
