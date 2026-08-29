from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_DB = PROJECT_ROOT / "data" / "test_agentdesk.db"
os.environ["AGENTDESK_DATABASE_URL"] = "sqlite:///./data/test_agentdesk.db"
os.environ["AGENTDESK_ADMIN_EMAIL"] = "admin@test.local"
os.environ["AGENTDESK_ADMIN_PASSWORD"] = "TestPassword123!"
os.environ["AGENTDESK_SEED_DEMO_DATA"] = "false"
os.environ["AGENTDESK_WHATSAPP_PROVIDER"] = "demo"
os.environ["AGENTDESK_EVOLUTION_WEBHOOK_SECRET"] = "test-evolution-secret"
os.environ["AGENTDESK_EVOLUTION_INSTANCE_NAME"] = "agentdesk"
os.environ["AGENTDESK_KNOWLEDGE_QUEUE_MODE"] = "inline"
os.environ["AGENTDESK_OPENAI_API_KEY"] = ""
os.environ["AGENTDESK_OPENAI_BASE_URL"] = ""
os.environ["AGENTDESK_OPENAI_MODEL"] = ""
os.environ["AGENTDESK_OPENAI_EMBEDDING_MODEL"] = ""
# Keep the unit-test database deterministic and offline.  Multilingual
# FastEmbed coverage is exercised by the migration/benchmark commands.
os.environ["AGENTDESK_EMBEDDING_PROVIDER"] = "local_hash"
os.environ.pop("AGENTDESK_META_ACCESS_TOKEN", None)

from backend.app.database import Base, engine  # noqa: E402
from backend.app.main import app  # noqa: E402


@pytest.fixture
def client():
    Base.metadata.drop_all(engine)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def authenticated_client(client: TestClient):
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@test.local", "password": "TestPassword123!"},
    )
    assert response.status_code == 200
    return client


def pytest_sessionfinish(session, exitstatus):
    engine.dispose()
    TEST_DB.unlink(missing_ok=True)
