"""Acceleration Index — six dimensional scores + composite, persisted into timeseries.

Rebased scoring model (2026-05-09): each dimension's score is centered at the baseline
captured in `config/baselines.json`. Today's raw inputs anchor at score=50; future deviation
from baseline drives the dial. This is a CADENCE tracker — the dashboard answers "is AI
acceleration faster or slower than the day we benchmarked?", not "is it above some absolute
ceiling".

Each dimension function follows the pattern:
    _<dim>_raw(...)  →  ({"raw_input_key": value, ...}, headlines)
    _<dim>_score(raw, baseline)  →  float (0-100, 50 at baseline)
"""
import logging
import math
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import async_session
from app.models import RawItem, Signal, TimeseriesPoint
from app.services.baselines import (
    is_uninitialized,
    load_baselines,
    rebased_delta,
    rebased_ratio,
    save_baselines,
)

logger = logging.getLogger(__name__)

_GW_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(?:gigawatts?|GW)\b", re.IGNORECASE)
_MW_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(?:megawatts?|MW)\b", re.IGNORECASE)


# Dimension → tags considered for that dimension. Signals are matched if ANY tag overlaps.
DIMENSION_TAGS: dict[str, set[str]] = {
    "capability": {
        "scaling", "reasoning", "agentic", "agentic-research", "ai-driven-rd",
        "architectures", "self-play", "synthetic-data", "interpretability",
        "diffusion-lm", "rl",
    },
    "recursive_ai": {
        "recursive-self-improvement", "self-evolution", "instrumental-convergence",
        "alignment", "safety",
    },
    "infrastructure": {
        "compute", "hardware", "data-center", "power", "gpu", "infra",
    },
    "inference_cost": {
        "efficiency", "moe", "edge-inference", "ternary", "inference-cost",
        "cost-reduction",
    },
    "hyperscaler": {
        "capex", "hyperscaler", "infrastructure-investment", "earnings",
        "developer-tools", "lab-transparency",
    },
    "enterprise_roi": {
        "roi", "adoption", "enterprise-deployment", "productivity",
    },
}

DIMENSION_WEIGHTS: dict[str, float] = {
    "capability": 0.25,
    "recursive_ai": 0.20,
    "infrastructure": 0.15,
    "inference_cost": 0.15,
    "hyperscaler": 0.13,
    "enterprise_roi": 0.12,
}

DIMENSION_LABELS: dict[str, str] = {
    "capability": "Capability",
    "recursive_ai": "Recursive AI",
    "infrastructure": "Infrastructure",
    "inference_cost": "Inference Cost",
    "hyperscaler": "Hyperscaler $",
    "enterprise_roi": "Enterprise ROI",
}

WINDOW_DAYS = 30


def _signal_score(payload: dict, tags: set[str]) -> float:
    """0-1 contribution if signal matches; weighted by importance/5."""
    sig_tags = set(payload.get("pillar_tags") or [])
    if not (sig_tags & tags):
        return 0.0
    try:
        imp = float(payload.get("importance_0_5", 0))
    except (TypeError, ValueError):
        imp = 0.0
    if imp <= 0:
        return 0.0
    return imp / 5.0


def _extract_gigawatts(payload: dict) -> float:
    """GW + MW/1000 from a signal. tldr and citations often quote the same deal,
    so we take the max across the two scans rather than summing — avoids
    double-counting when '5 gigawatts' appears in both fields."""
    def _scan(text: str) -> float:
        if not text:
            return 0.0
        gws = sum(float(m.group(1)) for m in _GW_PATTERN.finditer(text))
        mws = sum(float(m.group(1)) for m in _MW_PATTERN.finditer(text))
        return gws + mws / 1000.0
    tldr_gw = _scan(payload.get("tldr") or "")
    cite_gw = _scan(" ".join(payload.get("citations") or []))
    return max(tldr_gw, cite_gw)


# Merchant AI-silicon series feeding the supply-side capex component.
_MERCHANT_AI_SERIES = {
    "NVDA": "nvda_dc_revenue_quarterly",
    "AMD": "amd_dc_revenue_quarterly",
    "AVGO": "avgo_ai_revenue_quarterly",
}


async def _ticker_latest_yoy(db, series: str) -> tuple[float, float] | None:
    """Return (latest_revenue_m, yoy_pct) for a per-ticker quarterly series, or None
    if no comparable prior-year quarter exists."""
    rows = (
        await db.execute(
            select(TimeseriesPoint.ts, TimeseriesPoint.value)
            .where(TimeseriesPoint.series == series)
            .order_by(desc(TimeseriesPoint.ts)).limit(8)
        )
    ).all()
    if not rows:
        return None
    latest = rows[0]
    prior = next(
        (r for r in rows[1:] if r.ts.year == latest.ts.year - 1 and r.ts.month == latest.ts.month),
        None,
    )
    if not prior or not prior.value:
        return None
    yoy_pct = (float(latest.value) - float(prior.value)) / float(prior.value) * 100
    return float(latest.value), yoy_pct


# ─── Per-dimension RAW collectors (return raw inputs + headlines) ─────────────────────

async def _capability_raw(recent_signals: list[Signal]) -> tuple[dict, list[str]]:
    # signal_sum stays cross-cutting (a cross-tagged signal genuinely contributes to
    # multiple dimensions' cadence). Headlines filter to s.pillar so a signal only
    # appears under its primary dimension — no duplicates across panels.
    sig_sum = sum(_signal_score(s.payload, DIMENSION_TAGS["capability"]) for s in recent_signals)
    matched = [
        s for s in recent_signals
        if s.pillar == "capability"
        and float(s.payload.get("importance_0_5", 0) or 0) >= 2
    ]
    matched.sort(key=lambda s: float(s.payload.get("importance_0_5", 0) or 0), reverse=True)
    headlines = [s.payload["tldr"] for s in matched[:2] if s.payload.get("tldr")]
    return {"signal_sum": sig_sum}, headlines


async def _recursive_ai_raw(recent_signals: list[Signal]) -> tuple[dict, list[str]]:
    sig_sum = sum(_signal_score(s.payload, DIMENSION_TAGS["recursive_ai"]) for s in recent_signals)
    matched = [
        s for s in recent_signals
        if s.pillar == "recursive_ai"
        and float(s.payload.get("importance_0_5", 0)) >= 3
    ]
    matched.sort(key=lambda s: float(s.payload.get("importance_0_5", 0) or 0), reverse=True)
    headlines = [s.payload.get("tldr") or "" for s in matched[:2] if s.payload.get("tldr")]
    return {"signal_sum": sig_sum}, headlines


async def _infrastructure_raw(db, recent_signals: list[Signal]) -> tuple[dict, list[str]]:
    headlines: list[str] = []
    yoy_weighted_pct: float | None = None
    per_ticker: list[tuple[str, float, float]] = []
    for tkr, series in _MERCHANT_AI_SERIES.items():
        result = await _ticker_latest_yoy(db, series)
        if result:
            latest_m, yoy_pct = result
            per_ticker.append((tkr, latest_m, yoy_pct))
    if per_ticker:
        total_dollars = sum(latest for _, latest, _ in per_ticker)
        yoy_weighted_pct = sum(latest * yoy for _, latest, yoy in per_ticker) / total_dollars
        bits = " · ".join(f"{t} ${m/1000:.1f}B {y:+.0f}%" for t, m, y in per_ticker)
        headlines.append(
            f"Merchant AI silicon ${total_dollars/1000:.1f}B (latest), {yoy_weighted_pct:+.0f}% YoY weighted"
        )
        headlines.append(bits)

    gw_total = sum(
        _extract_gigawatts(s.payload)
        for s in recent_signals
        if set(s.payload.get("pillar_tags") or []) & DIMENSION_TAGS["infrastructure"]
    )
    if gw_total > 0:
        headlines.append(f"Power commitments (30d): {gw_total:.1f} GW total")

    sig_sum = sum(_signal_score(s.payload, DIMENSION_TAGS["infrastructure"]) for s in recent_signals)
    return {
        "yoy_weighted_pct": yoy_weighted_pct,
        "gw_30d": gw_total,
        "signal_sum": sig_sum,
    }, headlines


async def _inference_cost_raw(db, recent_signals: list[Signal]) -> tuple[dict, list[str]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    median_row = (
        await db.execute(
            select(TimeseriesPoint.value)
            .where(TimeseriesPoint.series == "oprt_frontier_median", TimeseriesPoint.ts >= cutoff)
            .order_by(desc(TimeseriesPoint.ts)).limit(1)
        )
    ).first()
    min_row = (
        await db.execute(
            select(TimeseriesPoint.value)
            .where(TimeseriesPoint.series == "oprt_frontier_min", TimeseriesPoint.ts >= cutoff)
            .order_by(desc(TimeseriesPoint.ts)).limit(1)
        )
    ).first()

    headlines: list[str] = []
    median: float | None = None
    if median_row:
        median = float(median_row.value)
        if min_row:
            headlines.append(f"frontier median ${median:.2f}/MTok · cheapest ${float(min_row.value):.2f}/MTok")
        else:
            headlines.append(f"frontier median ${median:.2f}/MTok")
    else:
        headlines.append("OpenRouter pricing pending")

    matched = [s for s in recent_signals if s.pillar == "inference_cost"]
    if matched and matched[0].payload.get("tldr"):
        headlines.append(matched[0].payload["tldr"])

    return {"frontier_median_usd_mtok": median}, headlines


async def _hyperscaler_raw(db, recent_signals: list[Signal]) -> tuple[dict, list[str]]:
    headlines: list[str] = []
    capex_yoy_pct: float | None = None

    rows = (
        await db.execute(
            select(TimeseriesPoint.ts, TimeseriesPoint.value, TimeseriesPoint.meta)
            .where(TimeseriesPoint.series == "capex_total_quarterly")
            .order_by(desc(TimeseriesPoint.ts)).limit(8)
        )
    ).all()
    if rows:
        latest = rows[0]
        prior = next(
            (r for r in rows[1:] if r.ts.year == latest.ts.year - 1 and r.ts.month == latest.ts.month),
            None,
        )
        latest_total = float(latest.value)
        if prior:
            prior_total = float(prior.value)
            capex_yoy_pct = (latest_total - prior_total) / prior_total * 100 if prior_total else 0
            headlines.append(
                f"Q{(latest.ts.month - 1) // 3 + 1} {latest.ts.year} hyperscaler capex "
                f"${latest_total/1000:.1f}B, {capex_yoy_pct:+.0f}% YoY"
            )
        else:
            headlines.append(f"Latest aggregate capex ${latest_total/1000:.1f}B (no YoY comparable yet)")
    else:
        headlines.append("Capex tracking pending")

    ticker_rows = (
        await db.execute(
            select(TimeseriesPoint.series, TimeseriesPoint.value, TimeseriesPoint.ts)
            .where(TimeseriesPoint.series.like("capex_%_quarterly"),
                   TimeseriesPoint.series.notlike("capex_total_%"))
            .order_by(desc(TimeseriesPoint.ts))
        )
    ).all()
    latest_by_tkr: dict[str, tuple[datetime, float]] = {}
    for r in ticker_rows:
        tkr = r.series.removeprefix("capex_").removesuffix("_quarterly")
        if tkr not in latest_by_tkr:
            latest_by_tkr[tkr] = (r.ts, float(r.value))
    if latest_by_tkr:
        bits = " · ".join(
            f"{t} ${v/1000:.1f}B"
            for t, (_, v) in sorted(latest_by_tkr.items(), key=lambda x: -x[1][1])[:4]
        )
        headlines.append(bits)

    sig_sum = sum(_signal_score(s.payload, DIMENSION_TAGS["hyperscaler"]) for s in recent_signals)
    return {"capex_yoy_pct": capex_yoy_pct, "signal_sum": sig_sum}, headlines


async def _enterprise_roi_raw(db, recent_signals: list[Signal]) -> tuple[dict, list[str]]:
    headlines: list[str] = []
    roi_pct: float | None = None

    rows = (
        await db.execute(
            select(TimeseriesPoint.ts, TimeseriesPoint.value, TimeseriesPoint.meta)
            .where(TimeseriesPoint.series == "roi_pct")
            .order_by(desc(TimeseriesPoint.ts)).limit(4)
        )
    ).all()
    if rows:
        latest = rows[0]
        roi_pct = float(latest.value)
        meta = latest.meta or {}
        sample = meta.get("sample_size")
        source = (meta.get("source") or "").split("—")[0].strip() or "survey"
        date_str = meta.get("date") or latest.ts.date().isoformat()
        if sample:
            headlines.append(f"{roi_pct:.0f}% report measurable ROI · n={sample} ({date_str}, {source})")
        else:
            headlines.append(f"{roi_pct:.0f}% report measurable ROI ({date_str}, {source})")
        if len(rows) >= 2:
            delta = roi_pct - float(rows[1].value)
            if abs(delta) >= 1:
                headlines.append(f"vs prior survey: {delta:+.0f} pts")

    sig_sum = sum(_signal_score(s.payload, DIMENSION_TAGS["enterprise_roi"]) for s in recent_signals)
    return {"roi_pct": roi_pct, "signal_sum": sig_sum}, headlines


# ─── Per-dimension SCORE mappers (raw inputs + baseline → 0-100) ─────────────────────

def _score_capability(raw: dict, baseline: dict) -> float:
    return rebased_ratio(raw.get("signal_sum", 0.0), baseline.get("signal_sum"))


def _score_recursive_ai(raw: dict, baseline: dict) -> float:
    return rebased_ratio(raw.get("signal_sum", 0.0), baseline.get("signal_sum"))


def _score_infrastructure(raw: dict, baseline: dict) -> float:
    """70% YoY-delta + 30% GW-ratio. Falls back to signal-sum ratio if no merchant-AI data."""
    yoy = raw.get("yoy_weighted_pct")
    gw = raw.get("gw_30d") or 0.0
    if yoy is not None:
        yoy_score = rebased_delta(yoy, baseline.get("yoy_weighted_pct"), scale=1.0)
        gw_score = rebased_ratio(gw, baseline.get("gw_30d"))
        return 0.7 * yoy_score + 0.3 * gw_score
    return rebased_ratio(raw.get("signal_sum", 0.0), baseline.get("signal_sum"))


def _score_inference_cost(raw: dict, baseline: dict) -> float:
    median = raw.get("frontier_median_usd_mtok")
    if median is None:
        return 50.0
    return rebased_ratio(median, baseline.get("frontier_median_usd_mtok"), inverse=True)


def _score_hyperscaler(raw: dict, baseline: dict) -> float:
    yoy = raw.get("capex_yoy_pct")
    sig = raw.get("signal_sum", 0.0)
    if yoy is not None:
        yoy_score = rebased_delta(yoy, baseline.get("capex_yoy_pct"), scale=1.0)
        sig_score = rebased_ratio(sig, baseline.get("signal_sum"))
        return 0.7 * yoy_score + 0.3 * sig_score
    return rebased_ratio(sig, baseline.get("signal_sum"))


def _score_enterprise_roi(raw: dict, baseline: dict) -> float:
    roi = raw.get("roi_pct")
    sig = raw.get("signal_sum", 0.0)
    if roi is not None:
        survey_score = rebased_delta(roi, baseline.get("roi_pct"), scale=1.0)
        sig_score = rebased_ratio(sig, baseline.get("signal_sum"))
        return 0.7 * survey_score + 0.3 * sig_score
    return rebased_ratio(sig, baseline.get("signal_sum"))


_SCORERS = {
    "capability": _score_capability,
    "recursive_ai": _score_recursive_ai,
    "infrastructure": _score_infrastructure,
    "inference_cost": _score_inference_cost,
    "hyperscaler": _score_hyperscaler,
    "enterprise_roi": _score_enterprise_roi,
}


# ─── Orchestration ─────────────────────────────────────────────────────────────────

async def collect_all_raws() -> tuple[dict[str, dict], dict[str, list[str]]]:
    """Compute all dimensions' raw inputs (no scoring). Used both by compute_scores() and
    by the baselines snapshot endpoint."""
    # "Recent" means the underlying article was published (or first fetched, when no
    # publish date is available) within WINDOW_DAYS — NOT when the signal was scored.
    # Otherwise a fresh /refresh on a backlog of year-old articles inflates dimension
    # signal_sum and surfaces ancient news as "recent headlines."
    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    article_ts = func.coalesce(RawItem.published_at, RawItem.fetched_at)
    async with async_session() as db:
        recent_signals = (
            await db.execute(
                select(Signal)
                .join(RawItem, Signal.raw_item_id == RawItem.id)
                .where(Signal.analyst_version == "v1", article_ts >= cutoff)
            )
        ).scalars().all()

        raws: dict[str, dict] = {}
        heads: dict[str, list[str]] = {}
        raws["capability"], heads["capability"] = await _capability_raw(recent_signals)
        raws["recursive_ai"], heads["recursive_ai"] = await _recursive_ai_raw(recent_signals)
        raws["infrastructure"], heads["infrastructure"] = await _infrastructure_raw(db, recent_signals)
        raws["inference_cost"], heads["inference_cost"] = await _inference_cost_raw(db, recent_signals)
        raws["hyperscaler"], heads["hyperscaler"] = await _hyperscaler_raw(db, recent_signals)
        raws["enterprise_roi"], heads["enterprise_roi"] = await _enterprise_roi_raw(db, recent_signals)
    return raws, heads


def _autosnap_missing(raws: dict[str, dict], baselines: dict) -> dict:
    """If any baseline keys are unset, populate from current raw inputs and persist."""
    changed = False
    for dim, raw in raws.items():
        for key, value in raw.items():
            if value is None:
                continue
            if is_uninitialized(baselines, dim, key):
                baselines.setdefault(dim, {})[key] = value
                changed = True
    if changed:
        save_baselines(baselines)
        logger.info("Auto-snapshotted missing baseline keys from current raw inputs.")
    return baselines


async def compute_scores() -> dict:
    """Compute current dimensional scores + composite. Rebased against baselines.json."""
    raws, heads = await collect_all_raws()
    baselines = load_baselines()
    baselines = _autosnap_missing(raws, baselines)

    dims: dict[str, dict] = {}
    for dim in DIMENSION_WEIGHTS:
        scorer = _SCORERS[dim]
        score = scorer(raws[dim], baselines.get(dim, {}))
        dims[dim] = {
            "score": round(score, 1),
            "headlines": heads[dim],
            "label": DIMENSION_LABELS[dim],
            "raw": raws[dim],
            "baseline": baselines.get(dim, {}),
        }

    index = sum(dims[d]["score"] * w for d, w in DIMENSION_WEIGHTS.items())

    # Rebased verdict thresholds: 60+ ACCELERATING, 40-59 STEADY, <40 SLOWING.
    if index >= 60:
        verdict = "ACCELERATING"
    elif index >= 40:
        verdict = "STEADY"
    else:
        verdict = "SLOWING"

    return {
        "index": round(index, 1),
        "verdict": verdict,
        "dimensions": dims,
        "weights": DIMENSION_WEIGHTS,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


async def snapshot_baselines_now() -> dict:
    """Capture current raw inputs as the new baseline. Returns the saved baselines dict."""
    raws, _ = await collect_all_raws()
    baseline = {dim: dict(raw) for dim, raw in raws.items()}
    save_baselines(baseline)
    return baseline


async def compute_and_persist() -> dict:
    """Compute scores AND persist composite + each dimension into timeseries (for sparklines)."""
    result = await compute_scores()
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        rows = [
            ("score_index", result["index"], {"verdict": result["verdict"]}),
        ]
        for dim, payload in result["dimensions"].items():
            rows.append((f"score_{dim}", payload["score"], {"label": payload["label"]}))

        for series, value, meta in rows:
            stmt = pg_insert(TimeseriesPoint).values(
                series=series, ts=now, value=value, meta=meta,
            ).on_conflict_do_update(
                index_elements=["series", "ts"],
                set_={"value": value, "meta": meta},
            )
            await db.execute(stmt)
        await db.commit()
    return result


async def sparkline(series: str, days: int = 30) -> list[dict]:
    """Daily-bucketed sparkline. One value per calendar day (UTC), averaged across that day's
    hourly recordings."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    async with async_session() as db:
        day_col = func.date_trunc("day", TimeseriesPoint.ts).label("day")
        rows = (
            await db.execute(
                select(day_col, func.avg(TimeseriesPoint.value).label("val"))
                .where(TimeseriesPoint.series == series, TimeseriesPoint.ts >= cutoff)
                .group_by(day_col)
                .order_by(day_col)
            )
        ).all()
    return [{"day": r.day.date().isoformat(), "value": float(r.val)} for r in rows]
