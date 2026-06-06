import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db.poll import intent_poller_task
from app.img_proxy import router as img_router

logging.basicConfig(
    level=settings.orchestrator_log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("orchestrator")


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info("orchestrator starting; fixture_mode=%s", settings.fixture_mode)
    poller = asyncio.create_task(intent_poller_task())
    try:
        yield
    finally:
        poller.cancel()
        try:
            await poller
        except asyncio.CancelledError:
            pass
        log.info("orchestrator stopped")


app = FastAPI(title="shopper-orchestrator", lifespan=lifespan)

# Local dev only — the Next.js dashboard hits /img from a different origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.include_router(img_router)


@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "fixture_mode": settings.fixture_mode,
        "insforge_configured": bool(
            settings.insforge_project_url and settings.insforge_service_role_key
        ),
        "google_configured": bool(settings.google_api_key),
    }
