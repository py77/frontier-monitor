"""Per-dimension raw-input baselines for the rebased scoring model.

The dashboard scores each dimension on cadence (deviation from a snapshot of "today's"
raw inputs), not absolute level — so the dial stays informative even when underlying
metrics like NVDA YoY growth or hyperscaler capex YoY are pegged at high values.

Snapshot file lives at `config/baselines.json` (mounted read-only at /config in the container).
The first call to `compute_scores()` after deploy auto-snapshots from current raw inputs;
subsequent re-snapshots are explicit (POST /api/baselines/snapshot).
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_PATH = next(
    (p for p in [
        Path("/config/baselines.json"),
        Path(__file__).resolve().parent.parent.parent.parent / "config" / "baselines.json",
    ] if p.exists()),
    Path("/config/baselines.json"),
)


def _empty_baseline() -> dict:
    return {
        "capability": {"signal_sum": None},
        "recursive_ai": {"signal_sum": None},
        "infrastructure": {"yoy_weighted_pct": None, "gw_30d": None, "signal_sum": None},
        "inference_cost": {"frontier_median_usd_mtok": None},
        "hyperscaler": {"capex_yoy_pct": None, "signal_sum": None},
        "enterprise_roi": {"roi_pct": None, "signal_sum": None},
    }


def load_baselines() -> dict:
    """Load baselines.json. Strips _meta. Returns dict with possibly-null values."""
    if not CONFIG_PATH.exists():
        return _empty_baseline()
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg.pop("_meta", None)
    out = _empty_baseline()
    for k, v in cfg.items():
        if k in out and isinstance(v, dict):
            out[k].update({kk: vv for kk, vv in v.items() if kk in out[k]})
    return out


def is_uninitialized(b: dict, dim: str, key: str) -> bool:
    return b.get(dim, {}).get(key) is None


def save_baselines(baselines: dict, *, captured_at: datetime | None = None) -> None:
    """Persist baselines + meta. Container mount may be read-only; we write the host path
    via the resolved CONFIG_PATH (works in dev with bind-mount; on read-only mount the
    snapshot lives in process memory only)."""
    payload = {
        "_meta": {
            "description": "Per-dimension raw-input baselines. Score = 50 at baseline, deviation drives the dial.",
            "captured_at": (captured_at or datetime.now(timezone.utc)).isoformat(),
            "scoring_model": "Rebased deltas: ratio for level metrics, additive pp delta for rate metrics",
            "verdict_thresholds_post_rebase": {
                "ACCELERATING": "score >= 60 (faster cadence than today)",
                "STEADY": "score 40-59 (around today's pace)",
                "SLOWING": "score < 40 (slower than today)",
            },
        },
        **baselines,
    }
    try:
        CONFIG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except (OSError, PermissionError) as e:
        logger.warning("Could not persist baselines (mount may be read-only): %s", e)


def rebased_ratio(current: float, baseline: float | None, *, inverse: bool = False) -> float:
    """Score = 50 * (current / baseline). 0.5x → 25, 1.0x → 50, 2.0x → 100. Clamped 0-100.
    If baseline is None or 0, returns 50 (no baseline yet)."""
    if baseline is None or baseline <= 0:
        return 50.0
    ratio = current / baseline
    if inverse:
        ratio = 1.0 / ratio if ratio > 0 else 0.0
    return max(0.0, min(100.0, 50.0 * ratio))


def rebased_delta(current: float, baseline: float | None, *, scale: float = 1.0) -> float:
    """Score = 50 + (current - baseline) * scale. Use for additive-pp metrics like YoY %.
    If baseline is None, returns 50."""
    if baseline is None:
        return 50.0
    return max(0.0, min(100.0, 50.0 + (current - baseline) * scale))
