from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from trading.domain.contracts import DomainModel
from trading.domain.hashing import canonical_hash
from trading.domain.time import require_aware_utc
from trading.persistence.research import (
    ResearchPersistenceError,
    ResearchRepository,
)
from trading.persistence.research_shadow import (
    ResearchShadowInitialization,
    ResearchShadowRuntimeRepository,
)
from trading.research.config import (
    ResearchConfigBundle,
    oos_process_evaluation_config_v2,
    shadow_paper_parameters,
)
from trading.research.contracts import (
    HASH_PATTERN,
    IDENTIFIER_PATTERN,
    VERSION_PATTERN,
    ChallengerStatus,
    OosVerdict,
)
from trading.research.lifecycle import (
    OosLifecycleResultV2,
    ResearchLifecycleService,
)
from trading.research.oos_lockbox import (
    OosEvaluationRequest,
    OosLockboxServiceV2,
)
from trading.research.oos_v2 import (
    MAX_PRIVATE_DATASET_BYTES_V2,
    OosLockboxResultV2,
    PrivateOosDatasetManifestV2,
)
from trading.research.shadow import (
    ShadowArmIdentity,
    ShadowExecutionContract,
)
from trading.research.shadow_runtime import (
    MatchedQuoteBundleV1,
    MatchedShadowCycleResultV1,
    ShadowArmRole,
    ShadowTargetDecisionV1,
)

PRIVATE_OOS_ROOT_ENV = "TRADING_OOS_PRIVATE_ROOT"


class OosShadowOperationError(RuntimeError):
    """Raised when a trusted OOS or shadow operation fails closed."""


class ShadowArmPlanV1(DomainModel):
    role: ShadowArmRole
    arm_id: str = Field(pattern=IDENTIFIER_PATTERN, max_length=30)
    strategy_id: str = Field(pattern=IDENTIFIER_PATTERN)
    strategy_version: str = Field(pattern=VERSION_PATTERN)


class ShadowExecutionPlanV1(DomainModel):
    market_input_manifest_hash: str = Field(pattern=HASH_PATTERN)
    decision_schedule_version: str = Field(pattern=VERSION_PATTERN)
    execution_scenario_version: str = Field(pattern=VERSION_PATTERN)
    cost_model_version: str = Field(pattern=VERSION_PATTERN)
    starting_capital_usd: Decimal = Field(gt=0)
    liquidity_policy_version: str = Field(pattern=VERSION_PATTERN)

    def as_domain_contract(self) -> ShadowExecutionContract:
        return ShadowExecutionContract(
            market_input_manifest_hash=self.market_input_manifest_hash,
            decision_schedule_version=self.decision_schedule_version,
            execution_scenario_version=self.execution_scenario_version,
            cost_model_version=self.cost_model_version,
            starting_capital_usd=str(self.starting_capital_usd),
            liquidity_policy_version=self.liquidity_policy_version,
        )


class OosV2ShadowPlanV1(DomainModel):
    schema_version: Literal["oos_v2_shadow_plan_v1"] = (
        "oos_v2_shadow_plan_v1"
    )
    plan_id: str = Field(pattern=IDENTIFIER_PATTERN)
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    experiment_family: str = Field(pattern=IDENTIFIER_PATTERN)
    submission_number: int = Field(ge=1)
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    evaluation_contract_hash: str = Field(pattern=HASH_PATTERN)
    private_dataset_manifest: PrivateOosDatasetManifestV2
    champion: ShadowArmPlanV1
    challenger: ShadowArmPlanV1
    execution_contract: ShadowExecutionPlanV1
    data_available_cutoff: datetime
    created_at: datetime
    expires_at: datetime
    automatic_promotion_enabled: Literal[False] = False
    real_order_routing: Literal[False] = False
    plan_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator(
        "data_available_cutoff",
        "created_at",
        "expires_at",
        mode="after",
    )
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        manifest = self.private_dataset_manifest
        if self.data_available_cutoff > self.created_at:
            raise ValueError("OOS plan was created before its data cutoff")
        if self.expires_at <= self.created_at:
            raise ValueError("OOS plan must expire after creation")
        if self.champion.role is not ShadowArmRole.CHAMPION:
            raise ValueError("OOS plan Champion arm has the wrong role")
        if self.challenger.role is not ShadowArmRole.CHALLENGER:
            raise ValueError("OOS plan Challenger arm has the wrong role")
        if self.champion.arm_id == self.challenger.arm_id:
            raise ValueError("OOS plan requires independent shadow arms")
        if (
            self.champion.strategy_version
            == self.challenger.strategy_version
        ):
            raise ValueError("OOS plan requires distinct strategy versions")
        if (
            manifest.candidate_artifact_hash
            != self.candidate_artifact_hash
            or manifest.evaluation_contract_hash
            != self.evaluation_contract_hash
        ):
            raise ValueError("OOS plan does not match its private manifest")
        payload = self.model_dump(mode="python", exclude={"plan_hash"})
        if canonical_hash(payload) != self.plan_hash:
            raise ValueError("OOS V2 shadow plan hash mismatch")
        return self

    def shadow_identities(
        self,
    ) -> tuple[ShadowArmIdentity, ShadowArmIdentity]:
        contract = self.execution_contract.as_domain_contract()
        return (
            ShadowArmIdentity(
                arm_id=self.champion.arm_id,
                strategy_id=self.champion.strategy_id,
                strategy_version=self.champion.strategy_version,
                contract=contract,
            ),
            ShadowArmIdentity(
                arm_id=self.challenger.arm_id,
                strategy_id=self.challenger.strategy_id,
                strategy_version=self.challenger.strategy_version,
                contract=contract,
            ),
        )


class ShadowActivationPlanV1(DomainModel):
    schema_version: Literal["shadow_activation_plan_v1"] = (
        "shadow_activation_plan_v1"
    )
    activation_id: str = Field(pattern=IDENTIFIER_PATTERN)
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    submission_number: int = Field(ge=1)
    oos_result_hash: str = Field(pattern=HASH_PATTERN)
    shadow_pair_id: str = Field(pattern=IDENTIFIER_PATTERN)
    champion_artifact_hash: str = Field(pattern=HASH_PATTERN)
    code_version: str = Field(pattern=VERSION_PATTERN)
    idempotency_key: str = Field(pattern=IDENTIFIER_PATTERN)
    created_at: datetime
    expires_at: datetime
    automatic_promotion_enabled: Literal[False] = False
    real_order_routing: Literal[False] = False
    activation_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("created_at", "expires_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("shadow activation must expire after creation")
        payload = self.model_dump(
            mode="python",
            exclude={"activation_hash"},
        )
        if canonical_hash(payload) != self.activation_hash:
            raise ValueError("shadow activation plan hash mismatch")
        return self


class MatchedShadowCycleCommitV1(DomainModel):
    schema_version: Literal["matched_shadow_cycle_commit_v1"] = (
        "matched_shadow_cycle_commit_v1"
    )
    input_id: str = Field(pattern=IDENTIFIER_PATTERN)
    run_id: str = Field(pattern=IDENTIFIER_PATTERN)
    champion_target: ShadowTargetDecisionV1
    challenger_target: ShadowTargetDecisionV1
    quote_bundle: MatchedQuoteBundleV1
    created_at: datetime
    automatic_promotion_enabled: Literal[False] = False
    real_order_routing: Literal[False] = False
    input_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("created_at", mode="after")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_input(self) -> Self:
        if (
            self.champion_target.role is not ShadowArmRole.CHAMPION
            or self.challenger_target.role
            is not ShadowArmRole.CHALLENGER
        ):
            raise ValueError("matched shadow cycle roles are invalid")
        if (
            self.champion_target.run_id != self.run_id
            or self.challenger_target.run_id != self.run_id
        ):
            raise ValueError("matched shadow cycle run binding mismatch")
        if self.created_at < self.quote_bundle.as_of:
            raise ValueError("matched shadow cycle predates its quote bundle")
        payload = self.model_dump(mode="python", exclude={"input_hash"})
        if canonical_hash(payload) != self.input_hash:
            raise ValueError("matched shadow cycle input hash mismatch")
        return self


@dataclass(frozen=True, slots=True)
class OosV2Preflight:
    plan_hash: str
    status: str
    challenger_status: ChallengerStatus
    existing_result: OosLockboxResultV2 | None
    private_dataset_verified: bool


@dataclass(frozen=True, slots=True)
class OosV2OperationResult:
    plan_hash: str
    lifecycle: OosLifecycleResultV2
    private_dataset_verified: bool


@dataclass(frozen=True, slots=True)
class ShadowActivationResult:
    activation_hash: str
    lifecycle_created: bool
    initialization: ResearchShadowInitialization


@dataclass(frozen=True, slots=True)
class MatchedShadowCycleCommitResult:
    input_hash: str
    cycle: MatchedShadowCycleResultV1
    replay_hash: str


class TrustedOosShadowOperations:
    """Gate-aware host operations for aggregate OOS and paper-only shadow."""

    real_order_routing = False
    automatic_promotion_enabled = False

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        config: ResearchConfigBundle,
        private_root: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._private_root = private_root
        self._clock = clock
        self._research = ResearchRepository(session_factory)
        self._shadow = ResearchShadowRuntimeRepository(session_factory)

    def preflight_oos_v2(
        self,
        plan: OosV2ShadowPlanV1,
    ) -> OosV2Preflight:
        existing = self._research.oos_result_v2(
            challenger_id=plan.challenger_id,
            submission_number=plan.submission_number,
        )
        self._validate_oos_bindings(plan, existing=existing)
        verified = False
        if existing is None:
            now = self._database_clock()
            if now >= plan.expires_at:
                raise OosShadowOperationError("OOS_V2_PLAN_EXPIRED")
            if plan.created_at > now:
                raise OosShadowOperationError(
                    "OOS_V2_PLAN_CREATED_IN_FUTURE"
                )
            if plan.data_available_cutoff > now:
                raise OosShadowOperationError(
                    "OOS_V2_DATA_CUTOFF_IN_FUTURE"
                )
            root = self._require_private_root()
            _verify_private_dataset_file(
                root,
                plan.private_dataset_manifest,
            )
            verified = True
            status = "READY"
        else:
            status = "ALREADY_EVALUATED"
        return OosV2Preflight(
            plan_hash=plan.plan_hash,
            status=status,
            challenger_status=self._research.challenger_status(
                plan.challenger_id
            ),
            existing_result=existing,
            private_dataset_verified=verified,
        )

    def evaluate_oos_v2(
        self,
        plan: OosV2ShadowPlanV1,
    ) -> OosV2OperationResult:
        preflight = self.preflight_oos_v2(plan)
        champion, challenger = plan.shadow_identities()
        if preflight.existing_result is not None:
            lifecycle = ResearchLifecycleService(
                repository=self._research
            ).evaluate_oos_and_register_shadow_v2(
                _oos_request(plan),
                champion_shadow=champion,
                challenger_shadow=challenger,
                evaluated_at=preflight.existing_result.evaluated_at,
                persisted_at=preflight.existing_result.evaluated_at,
            )
            return OosV2OperationResult(
                plan_hash=plan.plan_hash,
                lifecycle=lifecycle,
                private_dataset_verified=False,
            )

        comparison = self._comparison_contract(plan.challenger_id)
        manifest = plan.private_dataset_manifest
        evaluated_at = self._database_clock()
        lockbox = OosLockboxServiceV2.production(
            repository=self._research,
            private_root=self._require_private_root(),
            config=oos_process_evaluation_config_v2(
                self._config,
                dataset_id=manifest.dataset_id,
                dataset_manifest_hash=manifest.dataset_hash,
                data_available_cutoff=plan.data_available_cutoff,
                expected_source_data_manifest_hash=(
                    manifest.source_data_manifest_hash
                ),
                expected_candidate_replay_hash=(
                    manifest.candidate_replay_hash
                ),
                portfolio_comparison_contract=comparison,
            ),
            clock=self._clock,
        )
        lifecycle = ResearchLifecycleService(
            repository=self._research,
            oos_lockbox_v2=lockbox,
        ).evaluate_oos_and_register_shadow_v2(
            _oos_request(plan),
            champion_shadow=champion,
            challenger_shadow=challenger,
            evaluated_at=evaluated_at,
            persisted_at=self._database_clock(),
        )
        return OosV2OperationResult(
            plan_hash=plan.plan_hash,
            lifecycle=lifecycle,
            private_dataset_verified=True,
        )

    def preflight_shadow_activation(
        self,
        plan: ShadowActivationPlanV1,
    ) -> dict[str, object]:
        status = self._research.challenger_status(plan.challenger_id)
        if status not in {
            ChallengerStatus.SHADOW_PENDING,
            ChallengerStatus.SHADOW_RUNNING,
        }:
            raise OosShadowOperationError(
                "SHADOW_ACTIVATION_REQUIRES_PENDING_OR_RUNNING"
            )
        result = self._research.oos_result_v2(
            challenger_id=plan.challenger_id,
            submission_number=plan.submission_number,
        )
        if (
            result is None
            or result.verdict is not OosVerdict.PASS
            or result.result_hash != plan.oos_result_hash
        ):
            raise OosShadowOperationError(
                "SHADOW_ACTIVATION_OOS_BINDING_MISMATCH"
            )
        comparison = self._comparison_contract(plan.challenger_id)
        if (
            plan.champion_artifact_hash
            != comparison.champion_portfolio_manifest_hash
        ):
            raise OosShadowOperationError(
                "SHADOW_ACTIVATION_CHAMPION_ARTIFACT_MISMATCH"
            )
        pair = self._research.shadow_pair(plan.challenger_id)
        if (
            len(pair) != 2
            or {str(item.get("shadow_pair_id")) for item in pair}
            != {plan.shadow_pair_id}
            or any(bool(item.get("real_order_routing")) for item in pair)
        ):
            raise OosShadowOperationError(
                "SHADOW_ACTIVATION_PAIR_BINDING_MISMATCH"
            )
        now = self._database_clock()
        if status is ChallengerStatus.SHADOW_PENDING:
            if plan.created_at > now:
                raise OosShadowOperationError(
                    "SHADOW_ACTIVATION_CREATED_IN_FUTURE"
                )
            if now >= plan.expires_at:
                raise OosShadowOperationError(
                    "SHADOW_ACTIVATION_PLAN_EXPIRED"
                )
        else:
            start = self._research.shadow_start_event(
                plan.challenger_id
            )
            if (
                start is None
                or start.get("idempotency_key")
                != plan.idempotency_key
            ):
                raise OosShadowOperationError(
                    "SHADOW_ACTIVATION_IDEMPOTENCY_MISMATCH"
                )
        return {
            "status": "READY",
            "challenger_status": status.value,
            "shadow_pair_id": plan.shadow_pair_id,
            "oos_result_hash": plan.oos_result_hash,
            "activation_hash": plan.activation_hash,
            "real_order_routing": False,
        }

    def activate_shadow(
        self,
        plan: ShadowActivationPlanV1,
    ) -> ShadowActivationResult:
        self.preflight_shadow_activation(plan)
        now = self._database_clock()
        lifecycle = ResearchLifecycleService(
            repository=self._research
        ).start_shadow(
            challenger_id=plan.challenger_id,
            idempotency_key=plan.idempotency_key,
            created_at=now,
        )
        initialization = self._shadow.initialize_from_lifecycle(
            challenger_id=plan.challenger_id,
            champion_artifact_hash=plan.champion_artifact_hash,
            paper_parameters=shadow_paper_parameters(self._config),
            code_version=plan.code_version,
            created_at=now,
        )
        if lifecycle.status is not ChallengerStatus.SHADOW_RUNNING:
            raise OosShadowOperationError(
                "SHADOW_ACTIVATION_STATUS_MISMATCH"
            )
        return ShadowActivationResult(
            activation_hash=plan.activation_hash,
            lifecycle_created=lifecycle.created,
            initialization=initialization,
        )

    def preview_cycle(
        self,
        request: MatchedShadowCycleCommitV1,
    ) -> MatchedShadowCycleResultV1:
        return self._shadow.preview_matched_cycle(
            run_id=request.run_id,
            champion_target=request.champion_target,
            challenger_target=request.challenger_target,
            quote_bundle=request.quote_bundle,
        )

    def commit_cycle(
        self,
        request: MatchedShadowCycleCommitV1,
    ) -> MatchedShadowCycleCommitResult:
        del request
        raise OosShadowOperationError(
            "UNATTESTED_MANUAL_SHADOW_CYCLE_COMMIT_DISABLED"
        )

    def _validate_oos_bindings(
        self,
        plan: OosV2ShadowPlanV1,
        *,
        existing: OosLockboxResultV2 | None,
    ) -> None:
        manifest = self._research.challenger_manifest(
            plan.challenger_id
        )
        artifact = self._research.candidate_artifact(
            plan.challenger_id
        )
        comparison = self._comparison_contract(plan.challenger_id)
        private = plan.private_dataset_manifest
        if artifact is None:
            raise OosShadowOperationError(
                "OOS_V2_CANDIDATE_ARTIFACT_MISSING"
            )
        if (
            manifest.experiment_family != plan.experiment_family
            or artifact.bundle_hash != plan.candidate_artifact_hash
            or manifest.strategy_id != plan.challenger.strategy_id
            or manifest.strategy_version
            != plan.challenger.strategy_version
            or private.portfolio_comparison_contract_hash
            != comparison.contract_hash
            or private.source_data_manifest_hash
            != comparison.market_data_manifest_hash
            or private.candidate_artifact_hash
            != comparison.candidate_artifact_hash
        ):
            raise OosShadowOperationError(
                "OOS_V2_PLAN_DATABASE_BINDING_MISMATCH"
            )
        required_sessions = (
            self._config.config.recursive_improvement.portfolio_sharpe
            .minimum_common_sessions
        )
        required_trades = (
            self._config.config.recursive_improvement.portfolio_sharpe
            .minimum_independent_trades
        )
        if (
            private.common_session_count < required_sessions
            or private.independent_trade_count < required_trades
        ):
            raise OosShadowOperationError(
                "OOS_V2_PRIVATE_MANIFEST_BELOW_PREDECLARED_MINIMUM"
            )
        configured_capital = Decimal(
            self._config.config.shadow.starting_capital_usd
        )
        if plan.execution_contract.starting_capital_usd != configured_capital:
            raise OosShadowOperationError(
                "OOS_V2_SHADOW_CAPITAL_CONFIG_MISMATCH"
            )
        champion, challenger = plan.shadow_identities()
        try:
            self._research.validate_shadow_pair(
                challenger_id=plan.challenger_id,
                champion=champion,
                challenger=challenger,
            )
        except ResearchPersistenceError as exc:
            raise OosShadowOperationError(str(exc)) from exc
        if not self._research.has_passed_falsification(
            challenger_id=plan.challenger_id,
            candidate_artifact_hash=plan.candidate_artifact_hash,
            evaluation_contract_hash=plan.evaluation_contract_hash,
        ):
            raise OosShadowOperationError(
                "OOS_V2_MANDATORY_FALSIFICATION_NOT_PASSED"
            )
        if not self._research.has_passed_replay(
            challenger_id=plan.challenger_id,
            candidate_artifact_hash=plan.candidate_artifact_hash,
        ):
            raise OosShadowOperationError(
                "OOS_V2_DETERMINISTIC_REPLAY_NOT_PASSED"
            )
        if existing is None:
            if (
                self._research.challenger_status(plan.challenger_id)
                is not ChallengerStatus.PROPOSED
            ):
                raise OosShadowOperationError(
                    "OOS_V2_CHALLENGER_NOT_PROPOSED"
                )
            return
        if (
            existing.challenger_id != plan.challenger_id
            or existing.experiment_family != plan.experiment_family
            or existing.candidate_artifact_hash
            != plan.candidate_artifact_hash
            or existing.evaluation_contract_hash
            != plan.evaluation_contract_hash
            or existing.portfolio_comparison_contract_hash
            != comparison.contract_hash
        ):
            raise OosShadowOperationError(
                "OOS_V2_EXISTING_RESULT_BINDING_MISMATCH"
            )
        if existing.verdict is OosVerdict.PASS:
            _require_registered_pair_matches_plan(
                self._research.shadow_pair(plan.challenger_id),
                plan,
            )

    def _comparison_contract(self, challenger_id: str):
        comparison = self._research.portfolio_sharpe().comparison_contract(
            challenger_id=challenger_id
        )
        if comparison is None:
            raise OosShadowOperationError(
                "OOS_V2_PORTFOLIO_CONTRACT_MISSING"
            )
        return comparison

    def _require_private_root(self) -> Path:
        if self._private_root is None:
            raise OosShadowOperationError(
                "OOS_V2_PRIVATE_ROOT_NOT_CONFIGURED"
            )
        return _validated_private_root(self._private_root)

    def _database_clock(self) -> datetime:
        if self._clock is not None:
            return require_aware_utc(self._clock())
        with self._session_factory() as session:
            value = session.scalar(
                select(
                    func.clock_timestamp()
                    if session.get_bind().dialect.name == "postgresql"
                    else func.current_timestamp()
                )
            )
        if not isinstance(value, datetime):
            raise OosShadowOperationError(
                "RESEARCH_DATABASE_CLOCK_UNAVAILABLE"
            )
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def private_oos_root_from_environment() -> Path | None:
    raw = os.environ.get(PRIVATE_OOS_ROOT_ENV)
    if raw is None or not raw.strip():
        return None
    return _validated_private_root(Path(raw))


def _oos_request(plan: OosV2ShadowPlanV1) -> OosEvaluationRequest:
    return OosEvaluationRequest(
        challenger_id=plan.challenger_id,
        experiment_family=plan.experiment_family,
        submission_number=plan.submission_number,
        candidate_artifact_hash=plan.candidate_artifact_hash,
        evaluation_contract_hash=plan.evaluation_contract_hash,
    )


def _validated_private_root(path: Path) -> Path:
    if path.is_symlink():
        raise OosShadowOperationError("OOS_V2_PRIVATE_ROOT_UNSAFE")
    try:
        root = path.resolve(strict=True)
    except OSError as exc:
        raise OosShadowOperationError(
            "OOS_V2_PRIVATE_ROOT_UNAVAILABLE"
        ) from exc
    is_junction = getattr(root, "is_junction", None)
    if (
        not root.is_dir()
        or (callable(is_junction) and is_junction())
    ):
        raise OosShadowOperationError("OOS_V2_PRIVATE_ROOT_UNSAFE")
    return root


def _verify_private_dataset_file(
    private_root: Path,
    manifest: PrivateOosDatasetManifestV2,
) -> None:
    root = _validated_private_root(private_root)
    unresolved = root / f"{manifest.dataset_id}.json"
    if unresolved.is_symlink():
        raise OosShadowOperationError("OOS_V2_PRIVATE_DATASET_UNSAFE")
    try:
        path = unresolved.resolve(strict=True)
    except OSError as exc:
        raise OosShadowOperationError(
            "OOS_V2_PRIVATE_DATASET_UNAVAILABLE"
        ) from exc
    if (
        not path.is_relative_to(root)
        or not path.is_file()
        or path.stat().st_size <= 0
        or path.stat().st_size > MAX_PRIVATE_DATASET_BYTES_V2
    ):
        raise OosShadowOperationError("OOS_V2_PRIVATE_DATASET_UNSAFE")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise OosShadowOperationError(
            "OOS_V2_PRIVATE_DATASET_UNAVAILABLE"
        ) from exc
    if digest.hexdigest() != manifest.private_file_hash:
        raise OosShadowOperationError(
            "OOS_V2_PRIVATE_FILE_HASH_MISMATCH"
        )


def _require_registered_pair_matches_plan(
    pair: tuple[dict[str, object], ...],
    plan: OosV2ShadowPlanV1,
) -> None:
    if len(pair) != 2:
        raise OosShadowOperationError(
            "OOS_V2_REGISTERED_SHADOW_PAIR_MISMATCH"
        )
    by_role = {str(item.get("arm_role")): item for item in pair}
    contract = asdict(plan.execution_contract.as_domain_contract())
    expected = {
        "CHAMPION": plan.champion,
        "CHALLENGER": plan.challenger,
    }
    if set(by_role) != set(expected):
        raise OosShadowOperationError(
            "OOS_V2_REGISTERED_SHADOW_PAIR_MISMATCH"
        )
    for role, arm in expected.items():
        stored = by_role[role]
        if (
            stored.get("arm_id") != arm.arm_id
            or stored.get("strategy_id") != arm.strategy_id
            or stored.get("strategy_version") != arm.strategy_version
            or stored.get("execution_contract") != contract
            or bool(stored.get("real_order_routing"))
        ):
            raise OosShadowOperationError(
                "OOS_V2_REGISTERED_SHADOW_PAIR_MISMATCH"
            )


__all__ = [
    "PRIVATE_OOS_ROOT_ENV",
    "MatchedShadowCycleCommitResult",
    "MatchedShadowCycleCommitV1",
    "OosShadowOperationError",
    "OosV2OperationResult",
    "OosV2Preflight",
    "OosV2ShadowPlanV1",
    "ShadowActivationPlanV1",
    "ShadowActivationResult",
    "ShadowArmPlanV1",
    "ShadowExecutionPlanV1",
    "TrustedOosShadowOperations",
    "private_oos_root_from_environment",
]
