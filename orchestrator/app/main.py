import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.db.poll import intent_poller_task

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
