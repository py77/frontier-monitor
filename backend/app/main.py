import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.services.bootstrap import ensure_default_sources
    from app.tasks.scheduler import start_scheduler, stop_scheduler

    await ensure_default_sources()
    start_scheduler()
    logger.info("Frontier Monitor (Acceleration scoreboard) started")
    yield
    stop_scheduler()
    logger.info("Frontier Monitor stopped")


app = FastAPI(title="Frontier Monitor — AI Acceleration Index", version="0.2.0", lifespan=lifespan)

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

from app.api import ui, signals_api, sources_api, scoreboard_api, alerts_api, digests_api, baselines_api  # noqa: E402

app.include_router(ui.router, tags=["ui"])
app.include_router(signals_api.router, prefix="/api", tags=["signals"])
app.include_router(sources_api.router, prefix="/api", tags=["sources"])
app.include_router(scoreboard_api.router, prefix="/api", tags=["scoreboard"])
app.include_router(alerts_api.router, prefix="/api", tags=["alerts"])
app.include_router(digests_api.router, prefix="/api", tags=["digests"])
app.include_router(baselines_api.router, prefix="/api", tags=["baselines"])


@app.get("/health")
async def health():
    return {"status": "ok"}
