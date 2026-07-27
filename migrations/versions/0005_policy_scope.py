"""Scope adaptive policy state to one paper run.

Revision ID: 0005_policy_scope
Revises: 0004_paper_runtime
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_policy_scope"
down_revision: str | None = "0004_paper_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_SCOPE = "legacy_global"
AFFECTED_APPEND_ONLY_TABLES = (
    "policy_patches",
    "policy_versions",
    "commander_requests",
)


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        _drop_sqlite_guards()

    _add_scope_column("policy_patches")
    with op.batch_alter_table("policy_versions") as batch:
        batch.add_column(
            sa.Column(
                "scope_id",
                sa.String(80),
                nullable=False,
                server_default=LEGACY_SCOPE,
            )
        )
        batch.drop_constraint("uq_arm_policy_version", type_="unique")
        batch.create_unique_constraint(
            "uq_scope_arm_policy_version",
            ["scope_id", "arm_id", "version"],
        )
        batch.alter_column("scope_id", server_default=None)
    _add_scope_column("commander_requests")

    if dialect == "sqlite":
        _create_sqlite_guards()


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        _drop_sqlite_guards()

    with op.batch_alter_table("commander_requests") as batch:
        batch.drop_column("scope_id")
    with op.batch_alter_table("policy_versions") as batch:
        batch.drop_constraint("uq_scope_arm_policy_version", type_="unique")
        batch.create_unique_constraint(
            "uq_arm_policy_version",
            ["arm_id", "version"],
        )
        batch.drop_column("scope_id")
    with op.batch_alter_table("policy_patches") as batch:
        batch.drop_column("scope_id")

    if dialect == "sqlite":
        _create_sqlite_guards()


def _add_scope_column(table: str) -> None:
    with op.batch_alter_table(table) as batch:
        batch.add_column(
            sa.Column(
                "scope_id",
                sa.String(80),
                nullable=False,
                server_default=LEGACY_SCOPE,
            )
        )
        batch.alter_column("scope_id", server_default=None)


def _drop_sqlite_guards() -> None:
    for table in AFFECTED_APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_update")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete")


def _create_sqlite_guards() -> None:
    for table in AFFECTED_APPEND_ONLY_TABLES:
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
