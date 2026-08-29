from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api.ai_agent import router as ai_agent_router
from .api.actions import router as actions_router
from .api.automation import router as automation_router
from .api.auth import router as auth_router
from .api.channels import router as channels_router
from .api.contacts import router as contacts_router
from .api.conversations import router as conversations_router
from .api.dashboard import router as dashboard_router
from .api.events import router as events_router
from .api.knowledge import router as knowledge_router
from .api.product_prices import router as product_prices_router
from .api.quality_evaluation import router as quality_evaluation_router
from .api.integrations import router as integrations_router
from .api.webhooks import router as webhooks_router
from .config import PROJECT_ROOT, settings
from .database import SessionLocal, create_tables, engine
from .seed import seed_database
from .services.embeddings import warmup_embeddings


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_tables()
    with SessionLocal() as db:
        seed_database(db)
    # Load the multilingual tokenizer/runtime before the first customer
    # request.  A failed local model warmup is intentionally fatal so the
    # process never starts with an index it cannot query consistently.
    warmup_embeddings()
    yield


app = FastAPI(
    title=f"{settings.app_name} API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Total-Count"],
)
app.include_router(auth_router)
app.include_router(channels_router)
app.include_router(actions_router)
app.include_router(automation_router)
app.include_router(ai_agent_router)
app.include_router(conversations_router)
app.include_router(contacts_router)
app.include_router(knowledge_router)
app.include_router(product_prices_router)
app.include_router(quality_evaluation_router)
app.include_router(dashboard_router)
app.include_router(events_router)
app.include_router(integrations_router)
app.include_router(webhooks_router)


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "app": settings.app_name,
        "environment": settings.environment,
        "database": engine.dialect.name,
        "embedding_provider": settings.embedding_provider,
        "embedding_model": settings.configured_embedding_model,
        "embedding_dimensions": settings.embedding_dimensions,
        "openai": settings.openai_enabled,
        "whatsapp": settings.whatsapp_enabled,
        "whatsapp_provider": settings.whatsapp_provider,
    }


frontend_dist = PROJECT_ROOT / "frontend" / "dist"
if frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
