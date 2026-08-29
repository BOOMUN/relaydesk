from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import PROJECT_ROOT, settings


class Base(DeclarativeBase):
    pass


def _prepare_sqlite_path(database_url: str) -> None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return
    raw_path = database_url.removeprefix(prefix)
    path = Path(raw_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)


_prepare_sqlite_path(settings.database_url)

_HNSW_CONNECTION_INFO_KEY = "agentdesk_hnsw_settings"


def _hnsw_settings_signature() -> tuple[int, str]:
    return int(settings.rag_hnsw_ef_search), settings.rag_hnsw_iterative_scan


def _configure_postgres_connection(
    dbapi_connection: object,
    connection_record: object,
) -> None:
    """Apply HNSW settings once per pooled PostgreSQL connection.

    Older pgvector releases do not know ``hnsw.iterative_scan`` and the
    extension is created after the first connection during startup.  Each
    setting is therefore attempted independently and failures are ignored;
    the SQL retrieval path still works with the server defaults.
    """

    signature = _hnsw_settings_signature()
    info = getattr(connection_record, "info", {})
    if info.get(_HNSW_CONNECTION_INFO_KEY) == signature:
        return

    cursor = None
    applied: list[str] = []
    try:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        statements = [("ef_search", f"SET SESSION hnsw.ef_search = {signature[0]}")]
        # Set the value explicitly even for ``off`` so a pooled connection
        # cannot retain a previous strict/relaxed setting after configuration
        # reload.  Older pgvector versions simply reject this statement.
        statements.append(
            (
                "iterative_scan",
                "SET SESSION hnsw.iterative_scan = " f"'{signature[1]}'",
            )
        )
        for name, statement in statements:
            try:
                cursor.execute(statement)
                # Commit each GUC independently.  On an older pgvector build,
                # an unknown second setting aborts the current transaction;
                # committing successes first keeps the supported setting.
                dbapi_connection.commit()  # type: ignore[attr-defined]
                applied.append(name)
            except Exception:
                dbapi_connection.rollback()  # type: ignore[attr-defined]
        if applied and len(applied) == len(statements):
            info[_HNSW_CONNECTION_INFO_KEY] = signature
    except Exception:
        try:
            dbapi_connection.rollback()  # type: ignore[attr-defined]
        except Exception:
            pass
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass


def _install_postgres_connection_tuning(db_engine: Engine) -> None:
    if db_engine.dialect.name != "postgresql":
        return

    @event.listens_for(db_engine, "checkout")
    def _on_checkout(dbapi_connection, connection_record, connection_proxy):
        del connection_proxy
        _configure_postgres_connection(dbapi_connection, connection_record)


def configure_hnsw_session(db: Session) -> None:
    """Best-effort fallback for sessions created outside ``create_engine``.

    The application and bundled scripts use the tuned engine above, so this
    normally does no SQL.  It keeps direct test/tools sessions compatible with
    the same candidate-pool settings without making unsupported GUCs fatal.
    """

    connection = db.connection()
    if connection.dialect.name != "postgresql":
        return
    signature = _hnsw_settings_signature()
    if connection.info.get(_HNSW_CONNECTION_INFO_KEY) == signature:
        return
    applied_count = 0
    for statement in (
        f"SET LOCAL hnsw.ef_search = {signature[0]}",
        "SET LOCAL hnsw.iterative_scan = " f"'{signature[1]}'",
    ):
        try:
            # A savepoint prevents an unknown GUC from aborting the outer
            # request transaction on older pgvector versions.
            with connection.begin_nested():
                connection.execute(text(statement))
            applied_count += 1
        except Exception:
            continue
    if applied_count == 2:
        connection.info[_HNSW_CONNECTION_INFO_KEY] = signature


def create_database_engine(database_url: str | None = None) -> Engine:
    """Create an engine with production-safe PostgreSQL pool settings."""

    url = database_url or settings.database_url
    if url.startswith("sqlite"):
        _prepare_sqlite_path(url)
        sqlite_args = {"check_same_thread": False}
        db_engine = create_engine(
            url,
            connect_args=sqlite_args,
            pool_pre_ping=False,
        )
    else:
        db_engine = create_engine(
            url,
            connect_args={
                "connect_timeout": settings.database_connect_timeout_seconds,
            },
            pool_pre_ping=settings.database_pool_pre_ping,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_timeout=settings.database_pool_timeout_seconds,
            pool_recycle=settings.database_pool_recycle_seconds,
            pool_use_lifo=settings.database_pool_use_lifo,
        )
        _install_postgres_connection_tuning(db_engine)
    return db_engine


engine = create_database_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as db:
        yield db


def create_tables() -> None:
    from . import models  # noqa: F401

    if engine.dialect.name == "postgresql":
        # The vector type must exist before SQLAlchemy emits the
        # ``knowledge_chunks.embedding vector(n)`` table definition.
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(engine)
    _ensure_additive_schema()
    if engine.dialect.name == "postgresql":
        _assert_postgres_embedding_index()


def _ensure_additive_schema() -> None:
    """Apply small additive changes for installations without Alembic yet.

    ``create_all`` intentionally does not alter existing tables. The app is
    distributed with a migration script as well, but this idempotent check
    keeps local/demo databases usable immediately after an upgrade.
    """

    additions: dict[str, dict[str, str]] = {
        "conversations": {
            "channel_account_id": "INTEGER NULL REFERENCES channel_accounts(id) ON DELETE SET NULL",
        },
        "messages": {
            "channel_account_id": "INTEGER NULL REFERENCES channel_accounts(id) ON DELETE SET NULL",
            "provider": "VARCHAR(30) NULL",
        },
        "message_delivery_attempts": {
            "channel_account_id": "INTEGER NULL REFERENCES channel_accounts(id) ON DELETE SET NULL",
        },
        "knowledge_chunks": {
            "page_title": "VARCHAR(255) NOT NULL DEFAULT ''",
            "source_url": "TEXT NOT NULL DEFAULT ''",
            "section_path": "TEXT NOT NULL DEFAULT ''",
            "source_updated_at": "TIMESTAMP NULL",
            "token_count": "INTEGER NOT NULL DEFAULT 0",
            "metadata_json": "JSON NOT NULL DEFAULT '{}'",
        },
        "agent_profile_versions": {
            "order_intake_enabled": "BOOLEAN NOT NULL DEFAULT TRUE",
            "automation_timeout_minutes": "INTEGER NOT NULL DEFAULT 30",
            "web_search_enabled": "BOOLEAN NOT NULL DEFAULT FALSE",
            "web_search_allowed_domains": "JSON NOT NULL DEFAULT '[]'",
            "lead_qualification": "JSON NOT NULL DEFAULT '{}'",
        },
    }
    inspector = inspect(engine)
    for table_name, columns in additions.items():
        if not inspector.has_table(table_name):
            continue
        existing = {column["name"] for column in inspector.get_columns(table_name)}
        for column_name, column_type in columns.items():
            if column_name in existing:
                continue
            # Identifiers and type strings are static constants above, not
            # user input. Quote the table/column names for PostgreSQL and
            # SQLite compatibility.
            statement = text(
                f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {column_type}'
            )
            with engine.begin() as connection:
                connection.execute(statement)

    # Index creation is separate because SQLite cannot add an indexed column
    # and PostgreSQL cannot express IF NOT EXISTS inside ADD COLUMN.
    index_statements = (
        "CREATE INDEX IF NOT EXISTS ix_conversations_channel_account_id "
        "ON conversations (channel_account_id)",
        "CREATE INDEX IF NOT EXISTS ix_messages_channel_account_id "
        "ON messages (channel_account_id)",
        "CREATE INDEX IF NOT EXISTS ix_messages_provider ON messages (provider)",
        "CREATE INDEX IF NOT EXISTS ix_message_delivery_attempts_channel_account_id "
        "ON message_delivery_attempts (channel_account_id)",
    )
    with engine.begin() as connection:
        for statement in index_statements:
            connection.execute(text(statement))


def _assert_postgres_embedding_index() -> None:
    """Fail closed when a PostgreSQL database still contains mixed models."""

    from .config import settings

    expected = settings.configured_embedding_model
    with engine.connect() as connection:
        column_type = connection.scalar(
            text(
                "SELECT format_type(a.atttypid, a.atttypmod) "
                "FROM pg_attribute a "
                "JOIN pg_class c ON c.oid = a.attrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = 'knowledge_chunks' "
                "AND a.attname = 'embedding' AND a.attnum > 0 AND NOT a.attisdropped"
            )
        )
        expected_type = f"vector({settings.embedding_dimensions})"
        if column_type is not None and str(column_type).casefold() != expected_type.casefold():
            raise RuntimeError(
                "knowledge_chunks.embedding has incompatible type "
                f"{column_type!r}; expected {expected_type!r}. "
                "Run scripts/migrate_pgvector.py before starting AgentDesk."
            )
        models = {
            str(value)
            for value in connection.execute(
                text("SELECT DISTINCT embedding_model FROM knowledge_chunks")
            ).scalars()
            if value
        }
    if models and models != {expected} and settings.embedding_rebuild_on_mismatch:
        raise RuntimeError(
            "knowledge_chunks contains an incompatible embedding model set "
            f"{sorted(models)}; expected only {expected!r}. "
            "Run scripts/migrate_pgvector.py before starting AgentDesk."
        )


def is_postgresql() -> bool:
    """Return whether the configured SQLAlchemy engine targets PostgreSQL."""

    return engine.dialect.name == "postgresql"
