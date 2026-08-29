"""Add P2 business automation and secure REST connectors.

Revision ID: 20260828_02
Revises: 20260828_01
Create Date: 2026-08-28
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260828_02"
down_revision = "20260828_01"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in set(inspector.get_table_names()):
        return set()
    return {str(item["name"]) for item in inspector.get_columns(table_name)}


def upgrade() -> None:
    from backend.app.database import Base
    from backend.app import models  # noqa: F401

    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)

    table_name = "agent_profile_versions"
    existing = _columns(table_name)
    if not existing:
        return
    json_list_default = sa.text("'[]'::json") if bind.dialect.name == "postgresql" else sa.text("'[]'")
    json_object_default = sa.text("'{}'::json") if bind.dialect.name == "postgresql" else sa.text("'{}'")
    additions = (
        sa.Column(
            "order_intake_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "automation_timeout_minutes",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("30"),
        ),
        sa.Column(
            "web_search_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "web_search_allowed_domains",
            sa.JSON(),
            nullable=False,
            server_default=json_list_default,
        ),
        sa.Column(
            "lead_qualification",
            sa.JSON(),
            nullable=False,
            server_default=json_object_default,
        ),
    )
    for column in additions:
        if column.name not in existing:
            op.add_column(table_name, column)


def downgrade() -> None:
    # P2 tables contain audit, identity-verification and automation history.
    # Downgrades deliberately preserve those records.
    pass
