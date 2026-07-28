"""Append-only adaptive research plane records.

Revision ID: 0009_research_plane_v1
Revises: 0008_alpaca_paper_canary
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_research_plane_v1"
down_revision: str | None = "0008_alpaca_paper_canary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = (
    "research_commander_selections",
    "research_cycles",
    "research_cycle_events",
    "research_evidence_sources",
    "algorithm_proposals",
    "challenger_manifests",
    "challenger_events",
    "experiment_budget_events",
    "falsification_reports",
    "research_replay_artifacts",
    "oos_lockbox_results",
    "research_shadow_arm_registrations",
    "research_promotion_decisions",
)


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    op.create_table(
        "research_commander_selections",
        sa.Column("selection_id", sa.String(100), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False, unique=True),
        sa.Column("selected_commander", sa.String(40), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_research_selection_version"),
        sa.CheckConstraint(
            "selected_commander IN ('CODEX_SOL_MAX', 'WEBGPT_SOL_PRO')",
            name="ck_research_selection_commander",
        ),
    )
    op.create_table(
        "research_cycles",
        sa.Column("research_cycle_id", sa.String(100), primary_key=True),
        sa.Column("request_id", sa.String(100), nullable=False, unique=True),
        sa.Column(
            "selection_id",
            sa.String(100),
            sa.ForeignKey(
                "research_commander_selections.selection_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("selection_version", sa.Integer(), nullable=False),
        sa.Column("selected_commander", sa.String(40), nullable=False),
        sa.Column("source_snapshot_commit", sa.String(64), nullable=False),
        sa.Column("champion_version", sa.String(80), nullable=False),
        sa.Column("experiment_family", sa.String(100), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "data_available_cutoff",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("context_manifest_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "selected_commander IN ('CODEX_SOL_MAX', 'WEBGPT_SOL_PRO')",
            name="ck_research_cycle_commander",
        ),
        sa.CheckConstraint(
            "selection_version >= 1",
            name="ck_research_cycle_selection_version",
        ),
        sa.CheckConstraint(
            "data_available_cutoff <= as_of",
            name="ck_research_cycle_cutoff",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_research_cycle_expiry",
        ),
    )
    op.create_index(
        "ix_research_cycle_family_created",
        "research_cycles",
        ("experiment_family", "created_at", "research_cycle_id"),
    )
    op.create_table(
        "research_cycle_events",
        sa.Column("event_id", sa.String(100), primary_key=True),
        sa.Column(
            "research_cycle_id",
            sa.String(100),
            sa.ForeignKey("research_cycles.research_cycle_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("actor_role", sa.String(40), nullable=False),
        sa.Column("artifact_hash", sa.String(64)),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "actor_role IN ('HOST', 'WEB_SCOUT', 'RESEARCH_COMMANDER', "
            "'CANDIDATE_BUILDER', 'VALIDATOR', 'HUMAN')",
            name="ck_research_cycle_event_actor",
        ),
        sa.UniqueConstraint(
            "research_cycle_id",
            "idempotency_key",
            name="uq_research_cycle_event_idempotency",
        ),
    )
    op.create_index(
        "ix_research_cycle_event_latest",
        "research_cycle_events",
        ("research_cycle_id", "created_at", "event_id"),
    )
    op.create_table(
        "research_evidence_sources",
        sa.Column("source_id", sa.String(160), primary_key=True),
        sa.Column(
            "research_cycle_id",
            sa.String(100),
            sa.ForeignKey("research_cycles.research_cycle_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("title", sa.String(600), nullable=False),
        sa.Column("source_name", sa.String(200), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column(
            "first_available_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_tier", sa.String(40), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("excerpt", sa.String(2000), nullable=False),
        sa.Column("license_note", sa.String(500), nullable=False),
        sa.Column("corroborated", sa.Boolean(), nullable=False),
        sa.Column("contradiction", sa.Boolean(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_tier IN ("
            "'TIER_1_OFFICIAL','TIER_2_PRIMARY_DATA','TIER_3_REPUTABLE_NEWS',"
            "'TIER_4_INDUSTRY_ANALYSIS','TIER_5_SOCIAL','TIER_6_UNVERIFIED')",
            name="ck_research_evidence_tier",
        ),
        sa.CheckConstraint(
            "first_available_at <= captured_at",
            name="ck_research_evidence_availability",
        ),
        sa.UniqueConstraint(
            "research_cycle_id",
            "content_hash",
            name="uq_research_evidence_cycle_content",
        ),
    )
    op.create_index(
        "ix_research_evidence_cycle_tier",
        "research_evidence_sources",
        ("research_cycle_id", "source_tier", "captured_at"),
    )
    op.create_table(
        "algorithm_proposals",
        sa.Column("proposal_id", sa.String(100), primary_key=True),
        sa.Column(
            "research_cycle_id",
            sa.String(100),
            sa.ForeignKey("research_cycles.research_cycle_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("hypothesis_id", sa.String(100), nullable=False),
        sa.Column("parent_strategy_id", sa.String(100), nullable=False),
        sa.Column("parent_strategy_version", sa.String(80), nullable=False),
        sa.Column("proposed_strategy_id", sa.String(100), nullable=False),
        sa.Column("proposed_strategy_version", sa.String(80), nullable=False),
        sa.Column("proposal_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("evidence_manifest_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "parent_strategy_version <> proposed_strategy_version",
            name="ck_algorithm_proposal_new_version",
        ),
        sa.UniqueConstraint(
            "proposed_strategy_id",
            "proposed_strategy_version",
            name="uq_algorithm_proposal_strategy_version",
        ),
    )
    op.create_table(
        "challenger_manifests",
        sa.Column("challenger_id", sa.String(100), primary_key=True),
        sa.Column(
            "proposal_id",
            sa.String(100),
            sa.ForeignKey("algorithm_proposals.proposal_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("strategy_id", sa.String(100), nullable=False),
        sa.Column("strategy_version", sa.String(80), nullable=False),
        sa.Column("parent_version", sa.String(80), nullable=False),
        sa.Column("experiment_family", sa.String(100), nullable=False),
        sa.Column("source_commit", sa.String(64), nullable=False),
        sa.Column("patch_hash", sa.String(64), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("test_manifest_hash", sa.String(64), nullable=False),
        sa.Column("initial_status", sa.String(40), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "strategy_version <> parent_version",
            name="ck_challenger_new_version",
        ),
        sa.CheckConstraint(
            "initial_status = 'PROPOSED'",
            name="ck_challenger_initial_status",
        ),
        sa.UniqueConstraint(
            "strategy_id",
            "strategy_version",
            name="uq_challenger_strategy_version",
        ),
    )
    op.create_table(
        "challenger_events",
        sa.Column("challenger_event_id", sa.String(100), primary_key=True),
        sa.Column(
            "challenger_id",
            sa.String(100),
            sa.ForeignKey("challenger_manifests.challenger_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(40), nullable=False),
        sa.Column("to_status", sa.String(40), nullable=False),
        sa.Column("reason_code", sa.String(100), nullable=False),
        sa.Column("artifact_hash", sa.String(64)),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "to_status IN ('BUILD_FAILED','TEST_FAILED','REPLAY_FAILED',"
            "'OOS_REJECTED','SHADOW_PENDING','SHADOW_RUNNING',"
            "'PROMOTION_ELIGIBLE','PROMOTED','REJECTED','RETIRED')",
            name="ck_challenger_event_to_status",
        ),
        sa.CheckConstraint(
            "sequence >= 1",
            name="ck_challenger_event_sequence",
        ),
        sa.UniqueConstraint(
            "challenger_id",
            "idempotency_key",
            name="uq_challenger_event_idempotency",
        ),
        sa.UniqueConstraint(
            "challenger_id",
            "sequence",
            name="uq_challenger_event_sequence",
        ),
    )
    op.create_index(
        "ix_challenger_event_latest",
        "challenger_events",
        ("challenger_id", "sequence"),
    )
    op.create_table(
        "experiment_budget_events",
        sa.Column("budget_event_id", sa.String(100), primary_key=True),
        sa.Column("experiment_family", sa.String(100), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("submission_delta", sa.Integer(), nullable=False),
        sa.Column("oos_budget_delta", sa.Integer(), nullable=False),
        sa.Column("hypothesis_delta", sa.Integer(), nullable=False),
        sa.Column("failure_delta", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('HYPOTHESIS_CREATED','CANDIDATE_SUBMITTED',"
            "'OOS_CONSUMED','CANDIDATE_FAILED')",
            name="ck_experiment_budget_event_type",
        ),
        sa.CheckConstraint(
            "submission_delta >= 0 AND oos_budget_delta >= 0 "
            "AND hypothesis_delta >= 0 AND failure_delta >= 0",
            name="ck_experiment_budget_nonnegative",
        ),
        sa.UniqueConstraint(
            "experiment_family",
            "idempotency_key",
            name="uq_experiment_budget_event_idempotency",
        ),
    )
    op.create_table(
        "falsification_reports",
        sa.Column("falsification_report_id", sa.String(100), primary_key=True),
        sa.Column(
            "challenger_id",
            sa.String(100),
            sa.ForeignKey("challenger_manifests.challenger_id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("mandatory_passed", sa.Boolean(), nullable=False),
        sa.Column("report_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "research_replay_artifacts",
        sa.Column("replay_artifact_id", sa.String(100), primary_key=True),
        sa.Column(
            "challenger_id",
            sa.String(100),
            sa.ForeignKey("challenger_manifests.challenger_id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("candidate_artifact_hash", sa.String(64), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("data_manifest_hash", sa.String(64), nullable=False),
        sa.Column("first_replay_hash", sa.String(64), nullable=False),
        sa.Column("second_replay_hash", sa.String(64), nullable=False),
        sa.Column("deterministic_match", sa.Boolean(), nullable=False),
        sa.Column("artifact_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "oos_lockbox_results",
        sa.Column("oos_result_id", sa.String(100), primary_key=True),
        sa.Column(
            "challenger_id",
            sa.String(100),
            sa.ForeignKey("challenger_manifests.challenger_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("experiment_family", sa.String(100), nullable=False),
        sa.Column("submission_number", sa.Integer(), nullable=False),
        sa.Column("candidate_artifact_hash", sa.String(64), nullable=False),
        sa.Column("evaluation_contract_hash", sa.String(64), nullable=False),
        sa.Column("verdict", sa.String(20), nullable=False),
        sa.Column("common_sessions", sa.Integer(), nullable=False),
        sa.Column("result_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "verdict IN ('PASS','FAIL')",
            name="ck_oos_lockbox_verdict",
        ),
        sa.CheckConstraint(
            "submission_number >= 1 AND common_sessions >= 0",
            name="ck_oos_lockbox_counts",
        ),
        sa.UniqueConstraint(
            "challenger_id",
            "submission_number",
            name="uq_oos_lockbox_challenger_submission",
        ),
    )
    op.create_table(
        "research_shadow_arm_registrations",
        sa.Column("shadow_registration_id", sa.String(100), primary_key=True),
        sa.Column("shadow_pair_id", sa.String(100), nullable=False),
        sa.Column(
            "challenger_id",
            sa.String(100),
            sa.ForeignKey("challenger_manifests.challenger_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "oos_result_id",
            sa.String(100),
            sa.ForeignKey("oos_lockbox_results.oos_result_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("arm_role", sa.String(20), nullable=False),
        sa.Column("arm_id", sa.String(100), nullable=False, unique=True),
        sa.Column("strategy_id", sa.String(100), nullable=False),
        sa.Column("strategy_version", sa.String(80), nullable=False),
        sa.Column("execution_contract_hash", sa.String(64), nullable=False),
        sa.Column(
            "real_order_routing",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "arm_role IN ('CHAMPION','CHALLENGER')",
            name="ck_research_shadow_arm_role",
        ),
        sa.CheckConstraint(
            "real_order_routing = false",
            name="ck_research_shadow_no_real_routing",
        ),
        sa.UniqueConstraint(
            "shadow_pair_id",
            "arm_role",
            name="uq_research_shadow_pair_role",
        ),
        sa.UniqueConstraint(
            "challenger_id",
            "arm_role",
            name="uq_research_shadow_challenger_role",
        ),
    )
    op.create_index(
        "ix_research_shadow_pair",
        "research_shadow_arm_registrations",
        ("shadow_pair_id", "arm_role"),
    )
    op.create_table(
        "research_promotion_decisions",
        sa.Column("promotion_decision_id", sa.String(100), primary_key=True),
        sa.Column(
            "challenger_id",
            sa.String(100),
            sa.ForeignKey("challenger_manifests.challenger_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("verdict", sa.String(50), nullable=False),
        sa.Column("automatic_promotion_enabled", sa.Boolean(), nullable=False),
        sa.Column("replay_hash", sa.String(64), nullable=False),
        sa.Column("decision_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "automatic_promotion_enabled = false",
            name="ck_research_promotion_no_auto",
        ),
        sa.CheckConstraint(
            "verdict IN ('INELIGIBLE','ELIGIBLE_REQUIRES_MANUAL_APPROVAL',"
            "'MANUALLY_APPROVED','REJECTED')",
            name="ck_research_promotion_verdict",
        ),
    )

    if dialect == "sqlite":
        _create_sqlite_guards(APPEND_ONLY_TABLES)
    elif dialect == "postgresql":
        _create_postgres_guards(APPEND_ONLY_TABLES)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        _drop_sqlite_guards(APPEND_ONLY_TABLES)
    elif dialect == "postgresql":
        _drop_postgres_guards(APPEND_ONLY_TABLES)

    op.drop_table("research_promotion_decisions")
    op.drop_index(
        "ix_research_shadow_pair",
        table_name="research_shadow_arm_registrations",
    )
    op.drop_table("research_shadow_arm_registrations")
    op.drop_table("oos_lockbox_results")
    op.drop_table("research_replay_artifacts")
    op.drop_table("falsification_reports")
    op.drop_table("experiment_budget_events")
    op.drop_index("ix_challenger_event_latest", table_name="challenger_events")
    op.drop_table("challenger_events")
    op.drop_table("challenger_manifests")
    op.drop_table("algorithm_proposals")
    op.drop_index(
        "ix_research_evidence_cycle_tier",
        table_name="research_evidence_sources",
    )
    op.drop_table("research_evidence_sources")
    op.drop_index(
        "ix_research_cycle_event_latest",
        table_name="research_cycle_events",
    )
    op.drop_table("research_cycle_events")
    op.drop_index("ix_research_cycle_family_created", table_name="research_cycles")
    op.drop_table("research_cycles")
    op.drop_table("research_commander_selections")


def _create_sqlite_guards(tables: Sequence[str]) -> None:
    for table in tables:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_no_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'append-only table cannot be updated');
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_no_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'append-only table cannot be deleted');
            END
            """
        )


def _drop_sqlite_guards(tables: Sequence[str]) -> None:
    for table in tables:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_update")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete")


def _create_postgres_guards(tables: Sequence[str]) -> None:
    for table in tables:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation()
            """
        )


def _drop_postgres_guards(tables: Sequence[str]) -> None:
    for table in tables:
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}"
        )
