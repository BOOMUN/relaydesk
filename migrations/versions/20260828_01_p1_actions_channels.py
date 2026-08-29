"""Add P1 action and channel infrastructure.

Revision ID: 20260828_01
Revises:
Create Date: 2026-08-28
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260828_01"
down_revision = None
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table_name: str) -> set[str]:
    return {
        str(value["name"])
        for value in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _indexes(table_name: str) -> set[str]:
    return {
        str(value["name"])
        for value in sa.inspect(op.get_bind()).get_indexes(table_name)
        if value.get("name")
    }


def _add_existing_table_columns() -> None:
    tables = _tables()
    additions = {
        "conversations": [
            sa.Column("channel_account_id", sa.Integer(), nullable=True),
        ],
        "messages": [
            sa.Column("channel_account_id", sa.Integer(), nullable=True),
            sa.Column("provider", sa.String(length=30), nullable=True),
        ],
        "message_delivery_attempts": [
            sa.Column("channel_account_id", sa.Integer(), nullable=True),
        ],
    }
    for table_name, columns in additions.items():
        if table_name not in tables:
            continue
        existing = _columns(table_name)
        for column in columns:
            if column.name not in existing:
                op.add_column(table_name, column)

    foreign_keys = {
        "conversations": "fk_conversations_channel_account_id",
        "messages": "fk_messages_channel_account_id",
        "message_delivery_attempts": "fk_delivery_attempts_channel_account_id",
    }
    for table_name, constraint_name in foreign_keys.items():
        if table_name not in tables or "channel_accounts" not in tables:
            continue
        existing = {
            str(value.get("name") or "")
            for value in sa.inspect(op.get_bind()).get_foreign_keys(table_name)
        }
        if constraint_name not in existing:
            op.create_foreign_key(
                constraint_name,
                table_name,
                "channel_accounts",
                ["channel_account_id"],
                ["id"],
                ondelete="SET NULL",
            )

    indexes = {
        "conversations": ("ix_conversations_channel_account_id", ["channel_account_id"]),
        "messages": ("ix_messages_channel_account_id", ["channel_account_id"]),
        "message_delivery_attempts": (
            "ix_message_delivery_attempts_channel_account_id",
            ["channel_account_id"],
        ),
    }
    for table_name, (index_name, columns) in indexes.items():
        if table_name in tables and index_name not in _indexes(table_name):
            op.create_index(index_name, table_name, columns)
    if "messages" in tables and "ix_messages_provider" not in _indexes("messages"):
        op.create_index("ix_messages_provider", "messages", ["provider"])


def upgrade() -> None:
    # SQLAlchemy metadata owns the complete new-table definitions. This keeps
    # the first migration idempotent for installations where create_all ran
    # before Alembic was introduced.
    from backend.app.database import Base
    from backend.app import models  # noqa: F401

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    Base.metadata.create_all(bind=bind)
    _add_existing_table_columns()


def downgrade() -> None:
    # P1 stores audit and channel-delivery history. Downgrade is intentionally
    # non-destructive; rolling back the application version preserves data.
    pass
