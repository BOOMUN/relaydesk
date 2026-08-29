from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / "infra" / "evolution" / ".env", PROJECT_ROOT / ".env"),
        env_prefix="AGENTDESK_",
        extra="ignore",
    )

    app_name: str = "RelayDesk"
    environment: str = "local"
    host: str = "0.0.0.0"
    port: int = 8000
    database_url: str = "sqlite:///./data/agentdesk.db"
    # PostgreSQL pool defaults are sized for a small multi-worker deployment;
    # they are ignored for SQLite.  All values are configurable so the pool
    # can be matched to the server's max_connections and worker count.
    database_pool_size: int = Field(default=10, ge=1, le=200)
    database_max_overflow: int = Field(default=10, ge=0, le=500)
    database_pool_timeout_seconds: int = Field(default=30, ge=1, le=600)
    database_pool_recycle_seconds: int = Field(default=1800, ge=60, le=86400)
    database_pool_pre_ping: bool = True
    database_pool_use_lifo: bool = True
    database_connect_timeout_seconds: int = Field(default=10, ge=1, le=120)
    admin_email: str = "admin@local.test"
    admin_password: str = "replace-with-a-strong-password"
    session_hours: int = 12
    seed_demo_data: bool = True
    max_agent_seats: int = 5
    default_locale: Literal["zh-CN", "zh-TW"] = "zh-TW"

    ai_context_inactivity_minutes: int = Field(default=30, ge=1, le=1440)
    ai_context_max_messages: int = Field(default=40, ge=4, le=200)
    ai_context_max_characters: int = Field(default=12000, ge=1000, le=100000)
    ai_context_scheduler_interval_seconds: int = Field(default=15, ge=5, le=300)
    ai_context_close_retry_minutes: int = Field(default=5, ge=1, le=60)

    knowledge_queue_mode: Literal["redis", "inline"] = "redis"
    knowledge_redis_url: str = Field(default="redis://127.0.0.1:6380/7", repr=False)
    knowledge_sync_timezone: str = "Asia/Shanghai"
    knowledge_sync_hour: int = Field(default=3, ge=0, le=23)
    knowledge_sync_minute: int = Field(default=10, ge=0, le=59)

    openai_api_key: str | None = Field(default=None, repr=False)
    openai_base_url: str | None = None
    openai_model: str = ""
    openai_embedding_model: str = ""
    # Embeddings are intentionally explicit.  New deployments use the local
    # multilingual model by default; the legacy hash provider remains
    # available only for SQLite regression fixtures and must never be mixed
    # with a production index.
    embedding_provider: Literal["fastembed", "openai", "local_hash"] = "fastembed"
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_dimensions: int = Field(default=384, ge=1, le=4096)
    # The local batch sweep favored 128 texts per tokenizer call; four threads
    # kept query p95 lower than the larger, oversubscribed worker settings.
    embedding_batch_size: int = Field(default=128, ge=1, le=1024)
    embedding_threads: int = Field(default=4, ge=1, le=64)
    embedding_warmup_enabled: bool = True
    embedding_rebuild_on_mismatch: bool = True
    rag_min_similarity: float = Field(default=0.15, ge=-1.0, le=1.0)
    rag_min_retrieval_score: float = Field(default=0.46, ge=0.0, le=2.0)
    rag_min_lexical_score: float = Field(default=0.03, ge=0.0, le=1.0)
    rag_semantic_override_score: float = Field(default=0.55, ge=-1.0, le=1.0)
    # 64 candidates/ef_search is a quality-preserving starting point for the
    # current corpus and avoids sorting hundreds of rows before reranking.
    rag_vector_candidate_limit: int = Field(default=64, ge=10, le=2000)
    rag_hnsw_ef_search: int = Field(default=64, ge=10, le=10000)
    rag_hnsw_iterative_scan: Literal["off", "strict_order", "relaxed_order"] = (
        "strict_order"
    )
    rag_lexical_term_limit: int = Field(default=8, ge=2, le=32)
    # Final pairwise reranking is intentionally bounded after ANN/BM25 recall.
    rag_reranker_candidate_limit: int = Field(default=32, ge=3, le=200)
    rag_reranker_enabled: bool = True

    # Application-managed connector credentials are encrypted before they are
    # written to the database. This must be a Fernet key generated outside the
    # application and supplied through the deployment secret manager.
    secrets_encryption_key: str | None = Field(default=None, repr=False)
    web_search_provider: Literal["disabled", "brave"] = "disabled"
    web_search_api_key: str | None = Field(default=None, repr=False)
    web_search_timeout_seconds: int = Field(default=8, ge=2, le=30)
    web_search_max_results: int = Field(default=5, ge=1, le=10)

    meta_verify_token: str = Field(default="replace-with-a-random-token", repr=False)
    meta_app_secret: str | None = Field(default=None, repr=False)
    meta_access_token: str | None = Field(default=None, repr=False)
    meta_phone_number_id: str | None = None
    meta_business_account_id: str | None = None
    meta_graph_version: str = ""

    whatsapp_provider: Literal["demo", "meta", "evolution"] = "demo"
    evolution_api_url: str = "http://127.0.0.1:8081"
    evolution_api_key: str | None = Field(default=None, repr=False)
    evolution_instance_name: str = "agentdesk"
    evolution_webhook_url: str = (
        "http://host.docker.internal:8000/api/webhooks/evolution"
    )
    evolution_webhook_secret: str | None = Field(default=None, repr=False)

    @property
    def openai_enabled(self) -> bool:
        return bool(self.openai_api_key and self.openai_model)

    @property
    def openai_embeddings_enabled(self) -> bool:
        return bool(self.openai_api_key and self.openai_embedding_model)

    @property
    def configured_embedding_model(self) -> str:
        """Canonical model identifier persisted on every knowledge chunk."""

        if self.embedding_provider == "local_hash":
            return "local-hash-v1"
        if self.embedding_provider == "openai":
            if not self.openai_embedding_model:
                raise ValueError(
                    "AGENTDESK_OPENAI_EMBEDDING_MODEL is required when "
                    "AGENTDESK_EMBEDDING_PROVIDER=openai"
                )
            return self.openai_embedding_model
        return self.embedding_model

    @property
    def evolution_enabled(self) -> bool:
        return bool(
            self.evolution_api_url
            and self.evolution_api_key
            and self.evolution_instance_name
            and self.evolution_webhook_url
            and self.evolution_webhook_secret
        )

    @property
    def whatsapp_enabled(self) -> bool:
        if self.whatsapp_provider == "meta":
            return bool(
                self.meta_access_token and self.meta_phone_number_id and self.meta_graph_version
            )
        if self.whatsapp_provider == "evolution":
            return self.evolution_enabled
        return False

    @property
    def knowledge_sync_time(self) -> str:
        return f"{self.knowledge_sync_hour:02d}:{self.knowledge_sync_minute:02d}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
