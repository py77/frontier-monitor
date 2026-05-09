"""Baseline snapshot management for the rebased scoring model.

GET /api/baselines           — view current baselines + meta
POST /api/baselines/snapshot — capture today's raw inputs as the new baseline
"""
from fastapi import APIRouter

from app.services.baselines import load_baselines
from app.services.score_engine import snapshot_baselines_now

router = APIRouter()


@router.get("/baselines")
async def get_baselines() -> dict:
    return load_baselines()


@router.post("/baselines/snapshot")
async def snapshot() -> dict:
    """Re-capture today's raw inputs as the new baseline. All dimension scores will read 50
    on the next /api/scoreboard call, then drift from there as cadence changes."""
    baseline = await snapshot_baselines_now()
    return {"status": "ok", "baseline": baseline}
