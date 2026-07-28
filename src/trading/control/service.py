from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from pydantic import JsonValue
from sqlalchemy import desc, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from trading.control.contracts import (
    AdaptivePolicyDecision,
    CommanderRequest,
    DecisionKind,
    DecisionReceipt,
    SelectionSnapshot,
)
from trading.control.providers import (
    PROVIDER_REGISTRY,
    CommanderProvider,
    provider_descriptor,
)
from trading.domain.contracts import PolicyPatch, TypedCondition, model_payload
from trading.domain.enums import ConditionType
from trading.domain.hashing import canonical_hash, canonical_json, stable_id
from trading.domain.time import SystemClock, require_aware_utc
from trading.llm.policy_compiler import PolicyCompileError, PolicyCompiler, PolicyState
from trading.persistence.models import (
    CommanderDecisionResultRow,
    CommanderDecisionRow,
    CommanderRequestRow,
    CommanderSelectionRow,
    PaperCycleRow,
    PolicyPatchRow,
    PolicyVersionRow,
)

REQUEST_TTL = timedelta(hours=6)
MAX_POLICY_TTL = timedelta(hours=6)
MAX_CONTEXT_BYTES = 512 * 1024


class ControlPlaneError(RuntimeError):
    pass


class NoProviderSelected(ControlPlaneError):
    pass


class SelectionConflict(ControlPlaneError):
    pass


class RequestNotFound(ControlPlaneError):
    pass


class DecisionConflict(ControlPlaneError):
    pass


class ControlPlaneService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._clock = SystemClock()

    def current_selection(self) -> SelectionSnapshot | None:
        with self._session_factory() as session:
            row = self._latest_selection_row(session)
            return None if row is None else self._selection_from_row(row)

    def select_provider(
        self,
        provider: CommanderProvider,
        *,
        expected_version: int | None,
        now: datetime | None = None,
    ) -> tuple[SelectionSnapshot, bool]:
        selected_at = self._now(now)
        descriptor = provider_descriptor(provider)
        with self._session_factory() as session, session.begin():
            self._selection_write_lock(session)
            current = self._latest_selection_row(session)
            current_version = 0 if current is None else current.version
            if expected_version is not None and expected_version != current_version:
                raise SelectionConflict(
                    f"Selection version changed: expected {expected_version}, "
                    f"current {current_version}"
                )
            if current is not None and current.provider == provider.value:
                return self._selection_from_row(current), False

            version = current_version + 1
            config_payload = {
                "version": version,
                "provider": provider.value,
                "model": descriptor.model,
                "reasoning_profile": descriptor.reasoning_profile,
            }
            row = CommanderSelectionRow(
                selection_id=stable_id("selection", version, provider.value),
                version=version,
                provider=provider.value,
                model=descriptor.model,
                reasoning_profile=descriptor.reasoning_profile,
                created_at=selected_at,
                config_hash=canonical_hash(config_payload),
            )
            session.add(row)
            session.flush()
            return self._selection_from_row(row), True

    def create_request(
        self,
        *,
        arm_scope: str,
        scope_id: str = "legacy_global",
        context: dict[str, Any],
        as_of: datetime | None = None,
        data_available_cutoff: datetime | None = None,
        now: datetime | None = None,
    ) -> CommanderRequest:
        created_at = self._now(now)
        decision_time = self._now(as_of) if as_of is not None else created_at
        cutoff = (
            self._now(data_available_cutoff)
            if data_available_cutoff is not None
            else decision_time
        )
        if cutoff > decision_time:
            raise ControlPlaneError("data_available_cutoff must not exceed as_of")
        if arm_scope not in {"B3-RISK", "B3-FULL"}:
            raise ControlPlaneError("Only B3-RISK and B3-FULL accept adaptive requests")
        if not scope_id.strip() or len(scope_id) > 80:
            raise ControlPlaneError("scope_id must contain 1 to 80 characters")
        typed_arm_scope = cast(Literal["B3-RISK", "B3-FULL"], arm_scope)
        context_size = len(canonical_json(context).encode("utf-8"))
        if context_size > MAX_CONTEXT_BYTES:
            raise ControlPlaneError(
                f"Context exceeds {MAX_CONTEXT_BYTES} UTF-8 bytes"
            )

        with self._session_factory() as session, session.begin():
            selection = self._latest_selection_row(session)
            if selection is None:
                raise NoProviderSelected("Select a commander provider first")
            descriptor = provider_descriptor(CommanderProvider(selection.provider))
            self._policy_write_lock(session, scope_id, arm_scope)
            policy_state = self._resolve_policy_state(
                session,
                arm_scope=arm_scope,
                scope_id=scope_id,
                active_at=decision_time,
            )
            active_policy = policy_state.as_payload()
            manifest_payload = {
                "selection_version": selection.version,
                "provider": selection.provider,
                "scope_id": scope_id,
                "arm_scope": arm_scope,
                "base_policy_version": policy_state.version,
                "as_of": decision_time,
                "data_available_cutoff": cutoff,
                "context": context,
                "active_policy": active_policy,
            }
            context_manifest_hash = canonical_hash(manifest_payload)
            request_id = stable_id(
                "control-request",
                selection.selection_id,
                scope_id,
                arm_scope,
                policy_state.version,
                context_manifest_hash,
                created_at,
            )
            prompt = build_decision_prompt(
                request_id=request_id,
                provider=CommanderProvider(selection.provider),
                scope_id=scope_id,
                arm_scope=typed_arm_scope,
                base_policy_version=policy_state.version,
                context_manifest_hash=context_manifest_hash,
            )
            request = CommanderRequest(
                request_id=request_id,
                selection_version=selection.version,
                provider=CommanderProvider(selection.provider),
                model=descriptor.model,
                reasoning_profile=descriptor.reasoning_profile,
                scope_id=scope_id,
                arm_scope=typed_arm_scope,
                base_policy_version=policy_state.version,
                as_of=decision_time,
                data_available_cutoff=cutoff,
                expires_at=created_at + REQUEST_TTL,
                context=context,
                active_policy=cast(dict[str, JsonValue], active_policy),
                context_manifest_hash=context_manifest_hash,
                prompt_hash=canonical_hash(prompt),
                created_at=created_at,
            )
            existing = session.get(CommanderRequestRow, request.request_id)
            if existing is not None:
                stored = CommanderRequest.model_validate(existing.payload_json)
                if canonical_hash(stored) != canonical_hash(request):
                    raise DecisionConflict(
                        "Deterministic commander request ID has different content"
                    )
                return stored
            session.add(
                CommanderRequestRow(
                    request_id=request.request_id,
                    scope_id=request.scope_id,
                    selection_id=selection.selection_id,
                    selection_version=request.selection_version,
                    provider=request.provider.value,
                    arm_scope=request.arm_scope,
                    base_policy_version=request.base_policy_version,
                    created_at=request.created_at,
                    expires_at=request.expires_at,
                    context_manifest_hash=request.context_manifest_hash,
                    prompt_hash=request.prompt_hash,
                    payload_json=model_payload(request),
                )
            )
            return request

    def get_request(self, request_id: str) -> CommanderRequest:
        with self._session_factory() as session:
            row = session.get(CommanderRequestRow, request_id)
            if row is None:
                raise RequestNotFound(f"Unknown request_id: {request_id}")
            return CommanderRequest.model_validate(row.payload_json)

    def request_for_cycle(
        self,
        *,
        scope_id: str,
        arm_scope: str,
        cycle_id: str,
    ) -> CommanderRequest | None:
        with self._session_factory() as session:
            rows = list(
                session.scalars(
                    select(CommanderRequestRow)
                    .where(
                        CommanderRequestRow.scope_id == scope_id,
                        CommanderRequestRow.arm_scope == arm_scope,
                    )
                    .order_by(desc(CommanderRequestRow.created_at))
                )
            )
        for row in rows:
            request = CommanderRequest.model_validate(row.payload_json)
            cycle = request.context.get("cycle")
            if isinstance(cycle, dict) and cycle.get("cycle_id") == cycle_id:
                return request
        return None

    def receipt_for_request(self, request_id: str) -> DecisionReceipt | None:
        with self._session_factory() as session:
            decision = session.scalar(
                select(CommanderDecisionRow).where(
                    CommanderDecisionRow.request_id == request_id
                )
            )
            if decision is None:
                return None
            result = session.scalar(
                select(CommanderDecisionResultRow).where(
                    CommanderDecisionResultRow.decision_id == decision.decision_id
                )
            )
            if result is None:
                return None
            return self._receipt_from_result(
                result,
                CommanderProvider(decision.provider),
                idempotent_replay=True,
            )

    def active_policy_state(
        self,
        *,
        arm_scope: str,
        scope_id: str,
        active_at: datetime,
    ) -> PolicyState:
        instant = require_aware_utc(active_at, "active_at")
        with self._session_factory() as session, session.begin():
            self._policy_write_lock(session, scope_id, arm_scope)
            return self._resolve_policy_state(
                session,
                arm_scope=arm_scope,
                scope_id=scope_id,
                active_at=instant,
            )

    def submit_decision(
        self,
        *,
        request_id: str,
        provider: CommanderProvider,
        output: dict[str, Any],
        now: datetime | None = None,
        cycle_id: str | None = None,
        cycle_lease_owner: str | None = None,
        cycle_attempt_count: int | None = None,
    ) -> DecisionReceipt:
        received_at = self._now(now)
        decision = AdaptivePolicyDecision.model_validate(output)
        payload_hash = canonical_hash(decision)
        decision_id = stable_id("control-decision", request_id, payload_hash)

        with self._session_factory() as session, session.begin():
            if any(
                value is not None
                for value in (
                    cycle_id,
                    cycle_lease_owner,
                    cycle_attempt_count,
                )
            ):
                if (
                    cycle_id is None
                    or cycle_lease_owner is None
                    or cycle_attempt_count is None
                ):
                    raise ControlPlaneError("Commander cycle fence is incomplete")
                self._assert_cycle_fence(
                    session,
                    cycle_id=cycle_id,
                    lease_owner=cycle_lease_owner,
                    attempt_count=cycle_attempt_count,
                    fallback_now=received_at,
                )
            request_row = session.get(CommanderRequestRow, request_id)
            if request_row is None:
                raise RequestNotFound(f"Unknown request_id: {request_id}")
            request = CommanderRequest.model_validate(request_row.payload_json)

            previous = session.scalar(
                select(CommanderDecisionRow).where(
                    CommanderDecisionRow.request_id == request_id
                )
            )
            if previous is not None:
                if previous.payload_hash != payload_hash:
                    raise DecisionConflict("A different decision already consumed this request")
                result = session.scalar(
                    select(CommanderDecisionResultRow).where(
                        CommanderDecisionResultRow.decision_id == previous.decision_id
                    )
                )
                if result is None:
                    raise ControlPlaneError("Decision exists without a terminal result")
                return self._receipt_from_result(
                    result,
                    CommanderProvider(previous.provider),
                    idempotent_replay=True,
                )

            decision_row = CommanderDecisionRow(
                decision_id=decision_id,
                request_id=request_id,
                provider=provider.value,
                received_at=received_at,
                payload_hash=payload_hash,
                payload_json=model_payload(decision),
            )
            session.add(decision_row)
            session.flush()

            binding_error = self._binding_error(
                session=session,
                request=request,
                decision=decision,
                provider=provider,
                received_at=received_at,
                request_selection_id=request_row.selection_id,
            )
            if binding_error is not None:
                code, detail = binding_error
                return self._store_result(
                    session=session,
                    decision_id=decision_id,
                    request=request,
                    provider=provider,
                    status="REJECTED",
                    reason_code=code,
                    reason_detail=detail,
                    created_at=received_at,
                )

            if decision.decision is DecisionKind.NO_CHANGE:
                return self._store_result(
                    session=session,
                    decision_id=decision_id,
                    request=request,
                    provider=provider,
                    status="NO_CHANGE",
                    reason_code="MODEL_NO_CHANGE",
                    reason_detail=decision.rationale_summary,
                    created_at=received_at,
                )

            if (
                decision.effective_from is None
                or decision.expires_at is None
            ):
                raise ControlPlaneError(
                    "Validated APPLY_PATCH decision lost its time window"
                )
            if (
                decision.expires_at - decision.effective_from
                > MAX_POLICY_TTL
            ):
                return self._store_result(
                    session=session,
                    decision_id=decision_id,
                    request=request,
                    provider=provider,
                    status="REJECTED",
                    reason_code="POLICY_TTL_EXCEEDED",
                    reason_detail="B3 policy TTL cannot exceed six hours",
                    created_at=received_at,
                )
            if (
                not decision.rollback_conditions
                or any(
                    not _is_expiry_rollback(
                        condition,
                        expires_at=decision.expires_at,
                    )
                    for condition in decision.rollback_conditions
                )
            ):
                return self._store_result(
                    session=session,
                    decision_id=decision_id,
                    request=request,
                    provider=provider,
                    status="REJECTED",
                    reason_code="UNSUPPORTED_ROLLBACK_CONDITION",
                    reason_detail=(
                        "Forward paper currently supports TIME_REACHED "
                        "rollback conditions only"
                    ),
                    created_at=received_at,
                )

            self._policy_write_lock(session, request.scope_id, request.arm_scope)
            current = self._resolve_policy_state(
                session,
                arm_scope=request.arm_scope,
                scope_id=request.scope_id,
                active_at=received_at,
            )
            if current.version != request.base_policy_version:
                return self._store_result(
                    session=session,
                    decision_id=decision_id,
                    request=request,
                    provider=provider,
                    status="REJECTED",
                    reason_code="POLICY_VERSION_CONFLICT",
                    reason_detail=(
                        f"Request base version {request.base_policy_version} "
                        f"is stale; current version is {current.version}"
                    ),
                    created_at=received_at,
                )

            patch = PolicyPatch(
                patch_id=stable_id("policy-patch", decision_id),
                schema_version="policy_patch_v1",
                arm_scope=decision.arm_scope,
                base_policy_version=decision.base_policy_version,
                effective_from=decision.effective_from,
                expires_at=decision.expires_at,
                operations=decision.operations,
                evidence_news_event_ids=decision.evidence_news_event_ids,
                raw_confidence=decision.raw_confidence,
                rollback_conditions=decision.rollback_conditions,
                model_run_id=stable_id("model-run", provider.value, request_id),
                prompt_hash=request.prompt_hash,
                context_manifest_hash=request.context_manifest_hash,
                created_at=received_at,
            )
            try:
                compiled = PolicyCompiler().compile(
                    patch,
                    current,
                    now=received_at,
                    shadow_mode=True,
                )
            except PolicyCompileError as exc:
                return self._store_result(
                    session=session,
                    decision_id=decision_id,
                    request=request,
                    provider=provider,
                    status="REJECTED",
                    reason_code="POLICY_COMPILE_REJECTED",
                    reason_detail=str(exc),
                    created_at=received_at,
                )

            compiled_payload = compiled.as_payload()
            compiled_hash = canonical_hash(compiled_payload)
            session.add(
                PolicyPatchRow(
                    patch_id=patch.patch_id,
                    scope_id=request.scope_id,
                    arm_scope=patch.arm_scope,
                    base_policy_version=patch.base_policy_version,
                    effective_from=patch.effective_from,
                    expires_at=patch.expires_at,
                    payload_json=model_payload(patch),
                )
            )
            session.add(
                PolicyVersionRow(
                    policy_version_id=stable_id(
                        "policy",
                        request.scope_id,
                        compiled.arm_id,
                        compiled.version,
                        patch.patch_id,
                    ),
                    scope_id=request.scope_id,
                    arm_id=compiled.arm_id,
                    version=compiled.version,
                    source_patch_id=patch.patch_id,
                    payload_json=compiled_payload,
                    created_at=received_at,
                )
            )
            return self._store_result(
                session=session,
                decision_id=decision_id,
                request=request,
                provider=provider,
                status="ACCEPTED",
                reason_code="POLICY_APPLIED",
                reason_detail="Validated policy patch compiled into a new shadow policy version",
                created_at=received_at,
                applied_policy_version=compiled.version,
                compiled_policy_hash=compiled_hash,
            )

    def status(
        self,
        *,
        scope_id: str = "legacy_global",
        history_limit: int = 20,
        active_at: datetime | None = None,
    ) -> dict[str, Any]:
        status_at = self._now(active_at)
        policies = {
            arm: self.active_policy_state(
                arm_scope=arm,
                scope_id=scope_id,
                active_at=status_at,
            ).as_payload()
            for arm in ("B3-RISK", "B3-FULL")
        }
        with self._session_factory() as session:
            selection_row = self._latest_selection_row(session)
            selection = (
                None
                if selection_row is None
                else self._selection_from_row(selection_row).model_dump(mode="json")
            )
            result_rows = list(
                session.scalars(
                    select(CommanderDecisionResultRow)
                    .join(
                        CommanderDecisionRow,
                        CommanderDecisionResultRow.decision_id
                        == CommanderDecisionRow.decision_id,
                    )
                    .join(
                        CommanderRequestRow,
                        CommanderDecisionRow.request_id
                        == CommanderRequestRow.request_id,
                    )
                    .where(CommanderRequestRow.scope_id == scope_id)
                    .order_by(desc(CommanderDecisionResultRow.created_at))
                    .limit(history_limit)
                )
            )
            history: list[dict[str, Any]] = []
            for result in result_rows:
                decision = session.get(CommanderDecisionRow, result.decision_id)
                if decision is None:
                    continue
                receipt = self._receipt_from_result(
                    result,
                    CommanderProvider(decision.provider),
                )
                history.append(receipt.model_dump(mode="json"))
        return {
            "scope_id": scope_id,
            "selection": selection,
            "providers": [
                descriptor.as_payload() for descriptor in PROVIDER_REGISTRY.values()
            ],
            "policies": policies,
            "history": history,
            "output_schema": AdaptivePolicyDecision.model_json_schema(),
        }

    def _binding_error(
        self,
        *,
        session: Session,
        request: CommanderRequest,
        decision: AdaptivePolicyDecision,
        provider: CommanderProvider,
        received_at: datetime,
        request_selection_id: str,
    ) -> tuple[str, str] | None:
        if received_at >= request.expires_at:
            return "REQUEST_EXPIRED", "The decision request has expired"
        if decision.request_id != request.request_id:
            return "REQUEST_ID_MISMATCH", "Output request_id does not match its envelope"
        if decision.context_manifest_hash != request.context_manifest_hash:
            return (
                "CONTEXT_HASH_MISMATCH",
                "Output context_manifest_hash does not match the prepared request",
            )
        if decision.arm_scope != request.arm_scope:
            return "ARM_SCOPE_MISMATCH", "Output arm_scope differs from the request"
        if decision.base_policy_version != request.base_policy_version:
            return (
                "BASE_VERSION_MISMATCH",
                "Output base_policy_version differs from the request",
            )
        allowed_evidence_ids = _request_news_event_ids(request)
        unsupported_evidence_ids = sorted(
            set(decision.evidence_news_event_ids) - allowed_evidence_ids
        )
        if unsupported_evidence_ids:
            return (
                "EVIDENCE_NOT_IN_CONTEXT",
                "Output cites news-event evidence outside the prepared request",
            )
        if provider is not request.provider:
            return (
                "PROVIDER_MISMATCH",
                f"Request belongs to {request.provider.value}, not {provider.value}",
            )
        current = self._latest_selection_row(session)
        if current is None:
            return "NO_ACTIVE_PROVIDER", "No commander provider is currently selected"
        if (
            current.selection_id != request_selection_id
            or current.version != request.selection_version
            or current.provider != provider.value
        ):
            return (
                "STALE_SELECTION",
                "The UI selection changed after this request was prepared",
            )
        return None

    def _store_result(
        self,
        *,
        session: Session,
        decision_id: str,
        request: CommanderRequest,
        provider: CommanderProvider,
        status: str,
        reason_code: str,
        reason_detail: str,
        created_at: datetime,
        applied_policy_version: int | None = None,
        compiled_policy_hash: str | None = None,
    ) -> DecisionReceipt:
        receipt = DecisionReceipt(
            decision_id=decision_id,
            request_id=request.request_id,
            provider=provider,
            status=status,  # type: ignore[arg-type]
            reason_code=reason_code,
            reason_detail=reason_detail[:500],
            arm_scope=request.arm_scope,  # type: ignore[arg-type]
            base_policy_version=request.base_policy_version,
            applied_policy_version=applied_policy_version,
            compiled_policy_hash=compiled_policy_hash,
            created_at=created_at,
        )
        session.add(
            CommanderDecisionResultRow(
                result_id=stable_id("control-result", decision_id, status, reason_code),
                decision_id=decision_id,
                status=status,
                reason_code=reason_code,
                reason_detail=reason_detail[:500],
                arm_scope=request.arm_scope,
                base_policy_version=request.base_policy_version,
                applied_policy_version=applied_policy_version,
                compiled_policy_hash=compiled_policy_hash,
                created_at=created_at,
                payload_json=model_payload(receipt),
            )
        )
        return receipt

    def _receipt_from_result(
        self,
        row: CommanderDecisionResultRow,
        provider: CommanderProvider,
        *,
        idempotent_replay: bool = False,
    ) -> DecisionReceipt:
        receipt = DecisionReceipt.model_validate(row.payload_json)
        if receipt.provider is not provider:
            raise ControlPlaneError("Decision provider and result provider disagree")
        if idempotent_replay:
            receipt = receipt.model_copy(update={"idempotent_replay": True})
        return receipt

    @staticmethod
    def _assert_cycle_fence(
        session: Session,
        *,
        cycle_id: str,
        lease_owner: str,
        attempt_count: int,
        fallback_now: datetime,
    ) -> None:
        statement = select(PaperCycleRow).where(
            PaperCycleRow.cycle_id == cycle_id
        )
        is_postgresql = (
            session.bind is not None
            and session.bind.dialect.name == "postgresql"
        )
        if is_postgresql:
            statement = statement.with_for_update()
        cycle = session.scalar(statement)
        comparison_now = fallback_now
        if is_postgresql:
            database_now = session.scalar(select(func.clock_timestamp()))
            if database_now is None:
                raise ControlPlaneError("Database clock is unavailable")
            comparison_now = _database_utc(database_now)
        if (
            cycle is None
            or cycle.status != "RUNNING"
            or cycle.lease_owner != lease_owner
            or cycle.attempt_count != attempt_count
            or cycle.lease_expires_at is None
            or _database_utc(cycle.lease_expires_at) <= comparison_now
        ):
            raise ControlPlaneError("Commander cycle lease fence is stale")

    @staticmethod
    def _latest_selection_row(session: Session) -> CommanderSelectionRow | None:
        return session.scalar(
            select(CommanderSelectionRow)
            .order_by(desc(CommanderSelectionRow.version))
            .limit(1)
        )

    @staticmethod
    def _selection_from_row(row: CommanderSelectionRow) -> SelectionSnapshot:
        return SelectionSnapshot(
            selection_id=row.selection_id,
            version=row.version,
            provider=CommanderProvider(row.provider),
            model=row.model,
            reasoning_profile=row.reasoning_profile,
            created_at=_database_utc(row.created_at),
            config_hash=row.config_hash,
        )

    @staticmethod
    def _resolve_policy_state(
        session: Session,
        *,
        arm_scope: str,
        scope_id: str,
        active_at: datetime,
    ) -> PolicyState:
        latest = ControlPlaneService._load_latest_policy_state(
            session,
            arm_scope,
            scope_id,
        )
        stored = ControlPlaneService._load_latest_policy_state(
            session,
            arm_scope,
            scope_id,
            as_of=active_at,
        )
        rows = list(
            session.scalars(
                select(PolicyPatchRow).where(
                    PolicyPatchRow.scope_id == scope_id,
                    PolicyPatchRow.arm_scope == arm_scope,
                    PolicyPatchRow.effective_from <= active_at,
                    PolicyPatchRow.expires_at > active_at,
                )
            )
        )
        candidate_patches = (
            PolicyPatch.model_validate(row.payload_json) for row in rows
        )
        active_patches = sorted(
            (patch for patch in candidate_patches if patch.created_at <= active_at),
            key=lambda patch: (patch.created_at, patch.patch_id),
        )
        composed = PolicyCompiler().compose(
            arm_scope,
            active_patches,
            version=stored.version,
            now=active_at,
        )
        if _same_policy_effects(stored, composed):
            return stored
        if latest.version > stored.version:
            return composed
        recomposed = PolicyState(
            arm_id=composed.arm_id,
            version=stored.version + 1,
            portfolio_risk_multiplier=composed.portfolio_risk_multiplier,
            strategy_risk_deltas=composed.strategy_risk_deltas,
            blocked_targets=composed.blocked_targets,
            active_buckets=composed.active_buckets,
            source_patch_id=composed.source_patch_id,
        )
        active_patch_hash = canonical_hash(
            [patch.patch_id for patch in active_patches]
        )
        session.add(
            PolicyVersionRow(
                policy_version_id=stable_id(
                    "policy-recomposition",
                    scope_id,
                    recomposed.arm_id,
                    recomposed.version,
                    active_patch_hash,
                ),
                scope_id=scope_id,
                arm_id=recomposed.arm_id,
                version=recomposed.version,
                source_patch_id=recomposed.source_patch_id,
                payload_json=recomposed.as_payload(),
                created_at=active_at,
            )
        )
        session.flush()
        return recomposed

    @staticmethod
    def _load_latest_policy_state(
        session: Session,
        arm_scope: str,
        scope_id: str,
        *,
        as_of: datetime | None = None,
    ) -> PolicyState:
        statement = select(PolicyVersionRow).where(
            PolicyVersionRow.scope_id == scope_id,
            PolicyVersionRow.arm_id == arm_scope,
        )
        if as_of is not None:
            statement = statement.where(PolicyVersionRow.created_at <= as_of)
        row = session.scalar(
            statement.order_by(desc(PolicyVersionRow.version)).limit(1)
        )
        if row is None:
            return PolicyState.default(arm_scope)
        payload = row.payload_json
        return PolicyState(
            arm_id=str(payload["arm_id"]),
            version=int(payload["version"]),
            portfolio_risk_multiplier=float(payload["portfolio_risk_multiplier"]),
            strategy_risk_deltas={
                str(key): float(value)
                for key, value in dict(payload["strategy_risk_deltas"]).items()
            },
            blocked_targets=frozenset(str(item) for item in payload["blocked_targets"]),
            active_buckets=frozenset(str(item) for item in payload["active_buckets"]),
            source_patch_id=(
                None
                if payload.get("source_patch_id") is None
                else str(payload["source_patch_id"])
            ),
        )

    @staticmethod
    def _selection_write_lock(session: Session) -> None:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(84531201)"))

    @staticmethod
    def _policy_write_lock(
        session: Session,
        scope_id: str,
        arm_scope: str,
    ) -> None:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                {"lock_key": f"adaptive-policy:{scope_id}:{arm_scope}"},
            )

    def _now(self, value: datetime | None) -> datetime:
        return self._clock.now() if value is None else require_aware_utc(value)


def build_decision_prompt(
    *,
    request_id: str,
    provider: CommanderProvider,
    scope_id: str = "legacy_global",
    arm_scope: str,
    base_policy_version: int,
    context_manifest_hash: str,
) -> str:
    return f"""You are the selected adaptive-policy commander: {provider.value}.

Read the prepared request JSON and use only evidence inside that bounded request.
Return exactly one JSON object matching adaptive_policy_decision_v1.

Hard boundaries:
- You may return NO_CHANGE or a versioned PolicyPatch proposal only.
- Never emit orders, target shares, broker actions, credentials, code edits,
  or raw portfolio weights.
- Preserve request_id={request_id}.
- Preserve context_manifest_hash={context_manifest_hash}.
- Preserve the host-bound policy scope={scope_id}; never infer another run.
- Preserve arm_scope={arm_scope} and base_policy_version={base_policy_version}.
- If evidence is insufficient, contradictory, stale, or outside the request, return NO_CHANGE.
- B3-RISK risk reduction must use PORTFOLIO:TOTAL with risk_multiplier in [0.25, 1.00]
  and risk_budget_delta=null.
- B3-RISK entry blocking may target only SYMBOL:QQQ, FACTOR:US_EQUITY_BETA,
  or FACTOR:US_TECH_BETA. B3-FULL tilts remain shadow-only.
- Every APPLY_PATCH decision needs explicit evidence IDs, a TTL no longer than six
  hours, and a TIME_REACHED current_time rollback exactly equal to expires_at.

The host validates the JSON schema, selection version, context hash, policy base
version, operation allowlist, risk bounds, expiry, idempotency, and audit record.
The deterministic risk engine retains final veto authority.
"""


def _database_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return require_aware_utc(value)


def _request_news_event_ids(request: CommanderRequest) -> set[str]:
    raw_analyses = request.context.get("news_analyses")
    if not isinstance(raw_analyses, list):
        return set()
    evidence_ids: set[str] = set()
    for raw in raw_analyses:
        if not isinstance(raw, dict):
            continue
        event_id = raw.get("news_event_id")
        if isinstance(event_id, str) and event_id:
            evidence_ids.add(event_id)
    return evidence_ids


def _is_expiry_rollback(
    condition: TypedCondition,
    *,
    expires_at: datetime,
) -> bool:
    if (
        condition.condition_type is not ConditionType.TIME_REACHED
        or condition.field != "current_time"
        or not isinstance(condition.value, str)
    ):
        return False
    try:
        expected = datetime.fromisoformat(
            condition.value.replace("Z", "+00:00")
        )
    except ValueError:
        return False
    return require_aware_utc(expected) == expires_at


def _same_policy_effects(left: PolicyState, right: PolicyState) -> bool:
    return (
        left.arm_id == right.arm_id
        and left.portfolio_risk_multiplier == right.portfolio_risk_multiplier
        and left.strategy_risk_deltas == right.strategy_risk_deltas
        and left.blocked_targets == right.blocked_targets
        and left.active_buckets == right.active_buckets
        and left.source_patch_id == right.source_patch_id
    )
