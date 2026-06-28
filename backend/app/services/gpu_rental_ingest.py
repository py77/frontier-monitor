"""GPU rental-rate ingest → compute-demand / scarcity signal (Infrastructure dimension).

Polls GPU rental sources, normalizes heterogeneous offers to **$/GPU/hr** via the
canonical-model dictionary (`config/gpu_models.json`), and writes scalar demand-signal
series into the `timeseries` table — mirroring `openrouter_ingest.py`. Each poll's raw
response is archived (gzipped) under `settings.gpu_raw_dir` for later reprocessing.

Two entry points:
- `ingest_source(source_id)` — fetch+parse ONE source, write its `gpu_<src>_<model>_*`
  series. Dispatched by `source_id` via COLLECTORS. (Called per-source by the scheduler
  and by `POST /api/ingest/<id>`.)
- `recompute_aggregates()` — after all sources polled, blend the latest per-source series
  into headline cross-source `gpu_<model>_*` series (marketplace + neocloud venues only;
  hyperscaler and the aggregator stay separate to avoid blending 3-6x price levels).

Design notes:
- Each collector parses its OWN provider schema into a clean (label, vram_gb, interface)
  triple, then `canonicalize()` resolves the canonical key. Unmapped labels are COUNTED
  and logged per source so coverage gaps are observable (not silently dropped).
- Per-source prices are stored as a representative median; per-host marketplaces compute
  the median across hosts inside the collector, pre-aggregated sources map fields directly.
- `null` price means NO stock — recorded as available=0, never zero-filled into a price.
"""
import gzip
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.database import async_session
from app.models import Source, TimeseriesPoint

logger = logging.getLogger(__name__)

_UA = {"User-Agent": "Frontier-Monitor/1.0 (GPU rental monitor)"}

# Venue tier per source — a SOURCE property, not a GPU property. The same A100 is a
# neocloud price on Akash but a hyperscaler price on Azure; never blend across venues.
# Cross-source headline aggregates use only {"marketplace", "neocloud"}. "aggregator"
# (ComputePrices) is itself a cross-provider consensus → kept separate, not folded in.
SOURCE_VENUE: dict[str, str] = {
    "gpu_akash": "neocloud",
    "gpu_vast": "marketplace",
    "gpu_runpod": "marketplace",
    "gpu_clore": "marketplace",
    "gpu_tensordock": "marketplace",
    "gpu_computeprices": "aggregator",
    "gpu_azure": "hyperscaler",
    "gpu_lambda": "neocloud",
    "gpu_coreweave": "neocloud",
}
_BLEND_VENUES = {"marketplace", "neocloud"}


# ─── Canonical-model dictionary ──────────────────────────────────────────────────────

# Mounted at /config in the container; falls back to the repo path for non-Docker runs
# (same pattern as capex_ingest.py).
_MODELS_PATH = next(
    (p for p in [
        Path("/config/gpu_models.json"),
        Path(__file__).resolve().parent.parent.parent.parent / "config" / "gpu_models.json",
    ] if p.exists()),
    Path("/config/gpu_models.json"),
)


def _load_models() -> dict:
    if not _MODELS_PATH.exists():
        logger.error("gpu_models.json not found at %s", _MODELS_PATH)
        return {"models": {}, "aliases": {}, "variants": {}}
    return json.loads(_MODELS_PATH.read_text(encoding="utf-8"))


_DICT = _load_models()
MODELS: dict = _DICT.get("models", {})
ALIASES: dict = _DICT.get("aliases", {})
VARIANTS: dict = _DICT.get("variants", {})

_VENDOR_WORDS = ("nvidia", "geforce", "tesla", "instinct", "amd", "radeon")


def _normalize(label: str) -> str:
    s = (label or "").lower()
    s = re.sub(r"[^a-z0-9]+", "", s)
    for w in _VENDOR_WORDS:
        s = s.replace(w, "")
    return s


def canonicalize(label: str, vram_gb: float | None = None, interface: str | None = None) -> str | None:
    """Resolve a provider GPU label to a canonical key, or None if unmapped.

    Order: direct alias hit → variant disambiguation (bare a100/h100/v100, by interface
    then vram) → miss. Callers should COUNT misses for coverage observability.
    """
    norm = _normalize(label)
    if not norm:
        return None
    if norm in ALIASES:
        return ALIASES[norm]
    if norm in VARIANTS:
        v = VARIANTS[norm]
        if interface:
            inorm = re.sub(r"[^a-z0-9]+", "", interface.lower())
            hit = v.get("by_interface", {}).get(inorm)
            if hit:
                return hit
        if vram_gb is not None:
            hit = v.get("by_vram", {}).get(str(int(vram_gb)))
            if hit:
                return hit
        return v.get("default")
    return None


def gpu_class(canonical: str) -> str:
    return MODELS.get(canonical, {}).get("class", "unknown")


def _parse_vram(ram: str | None) -> float | None:
    """'80Gi' / '141Gi' / '24 GB' → 80 / 141 / 24."""
    if not ram:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", str(ram))
    return float(m.group(1)) if m else None


# ─── Per-model aggregate accumulator ─────────────────────────────────────────────────

class ModelAgg:
    """Accumulates one source's offers for one canonical model. available/total/providers are
    None until a source reports them — so 'not tracked' is distinguishable from a real 0
    (e.g. TensorDock reports free-units but no capacity total → avail written, occupancy not)."""
    __slots__ = ("ondemand", "spot", "available", "total", "providers")

    def __init__(self) -> None:
        self.ondemand: list[float] = []   # $/GPU/hr on-demand prices
        self.spot: list[float] = []       # $/GPU/hr spot/bid prices
        self.available: int | None = None  # available GPU units
        self.total: int | None = None      # total GPU units (available + rented)
        self.providers: int | None = None  # distinct providers/hosts/offers for the model

    def add(self, *, ondemand=None, spot=None, available=None, total=None, providers=None) -> None:
        if ondemand is not None and ondemand > 0:
            self.ondemand.append(float(ondemand))
        if spot is not None and spot > 0:
            self.spot.append(float(spot))
        if available is not None:
            self.available = (self.available or 0) + int(available)
        if total is not None:
            self.total = (self.total or 0) + int(total)
        if providers is not None:
            self.providers = (self.providers or 0) + int(providers)


# ─── Collectors ──────────────────────────────────────────────────────────────────────
# Each returns (per_model, unmapped, raw_obj):
#   per_model: dict[canonical_key, ModelAgg]
#   unmapped:  dict[label, count]  (labels that didn't canonicalize, for observability)
#   raw_obj:   JSON-serializable raw response (archived gzipped)


async def _collect_akash(url: str) -> tuple[dict[str, ModelAgg], dict[str, int], object]:
    """Akash console-api /v1/gpu-prices — pre-aggregated bid marketplace.

    Per model: price{min,max,avg,weightedAverage,med} (USD $/GPU/hr, already correct unit;
    null = no current offers), availability{total,available}, providerAvailability. We use
    `med` as the representative on-demand price and `min` as a spot/low-bid proxy. No unit
    conversion needed — this is the clean vertical-slice source.
    """
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers=_UA) as c:
        resp = await c.get(url)
        resp.raise_for_status()
        data = resp.json()

    per_model: dict[str, ModelAgg] = {}
    unmapped: dict[str, int] = {}
    for m in data.get("models", []):
        label = m.get("model", "")
        vram = _parse_vram(m.get("ram"))
        iface = m.get("interface")
        key = canonicalize(label, vram, iface)
        if key is None:
            unmapped[label] = unmapped.get(label, 0) + 1
            continue
        agg = per_model.setdefault(key, ModelAgg())
        price = m.get("price")  # may be None = no offers
        avail = (m.get("availability") or {})
        prov = (m.get("providerAvailability") or {})
        # Use weightedAverage (supply-weighted, representative of transacting price) rather
        # than med — on a thin bid marketplace med can sit at the cheap end. Fall back to med.
        od = (price.get("weightedAverage") or price.get("med")) if price else None
        agg.add(
            ondemand=od,
            spot=(price or {}).get("min") if price else None,
            available=avail.get("available"),
            total=avail.get("total"),
            providers=prov.get("available"),
        )
    return per_model, unmapped, data


_NX_RE = re.compile(r"^\s*(\d+)\s*[xX]\s*(.+)$")  # "8x NVIDIA GeForce RTX 4090"
_IFACE_TOKENS = ("sxm5", "sxm4", "sxm2", "sxm", "pcie", "nvl", "oam")


async def _collect_clore(url: str) -> tuple[dict[str, ModelAgg], dict[str, int], object]:
    """Clore.ai /v1/marketplace — per-host marketplace, no auth. UNIT TRAP:
    price.usd.on_demand_usd is PER-DAY for the whole server → /24 AND /num_gpus = $/GPU/hr.
    specs.gpu = 'Nx NAME'. `rented` bool → occupancy. Public `spot` is a host floor (often 0)
    → ignored; use the BTC-derived rails never (garbage outliers) — only on_demand_usd."""
    async with httpx.AsyncClient(timeout=40.0, follow_redirects=True, headers=_UA) as c:
        resp = await c.get(url)
        resp.raise_for_status()
        data = resp.json()
    per_model: dict[str, ModelAgg] = {}
    unmapped: dict[str, int] = {}
    for s in data.get("servers", []):
        gpu = s.get("specs", {}).get("gpu", "")
        m = _NX_RE.match(gpu or "")
        if not m:
            continue
        n, name = int(m.group(1)), m.group(2)
        key = canonicalize(name)
        if key is None:
            unmapped[name] = unmapped.get(name, 0) + 1
            continue
        od_day = ((s.get("price") or {}).get("usd") or {}).get("on_demand_usd")
        agg = per_model.setdefault(key, ModelAgg())
        if od_day and n > 0:
            agg.add(ondemand=od_day / 24.0 / n)
        rented = bool(s.get("rented"))
        agg.add(total=n, available=(0 if rented else n))
    raw = {"code": data.get("code"), "servers": data.get("servers")}  # drop my_servers echo
    return per_model, unmapped, raw


def _first(d: dict, *keys):
    """First non-None value among the candidate keys (tolerates schema field-name variance)."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return None


async def _collect_tensordock_v2(url: str) -> tuple[dict[str, ModelAgg], dict[str, int], object]:
    """TensorDock v2 hostnodes — AUTHENTICATED (Bearer API key). The legacy no-auth host
    marketplace.tensordock.com/api/v0/client/deploy/hostnodes was decommissioned in 2026 (404),
    so this source now requires a key (TensorDock Developer Settings) the same opt-in way as
    Vast: set TENSORDOCK_API_KEY in .env, then enable via POST /api/sources/gpu_tensordock/toggle.

    Envelope is {"data": {...}} (confirmed on the open /api/v2/locations endpoint). The exact
    per-hostnode GPU/price field names are PROVISIONAL — the v2 hostnodes payload is auth-gated
    and undocumented publicly, so the parser tries the plausible names and, if no price is
    extracted, logs a sample node (the raw payload is also archived) so the mapping can be
    finalized against a real authed response. Verify field names on first authed poll.
    """
    if not settings.tensordock_api_key:
        raise RuntimeError(
            "TENSORDOCK_API_KEY not set — TensorDock v2 requires an API key "
            "(legacy no-auth v0 marketplace API was decommissioned 2026)"
        )
    headers = {**_UA, "Authorization": f"Bearer {settings.tensordock_api_key}"}
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers=headers) as c:
        resp = await c.get(url)
        resp.raise_for_status()
        data = resp.json()

    nodes = (data.get("data") or {}).get("hostnodes", data.get("hostnodes"))
    # v2 may return a list of nodes or a dict keyed by id — normalize to a list of node dicts.
    if isinstance(nodes, dict):
        nodes = list(nodes.values())
    elif not isinstance(nodes, list):
        nodes = []

    per_model: dict[str, ModelAgg] = {}
    unmapped: dict[str, int] = {}
    parsed_any = False
    for h in nodes:
        if not isinstance(h, dict):
            continue
        if not (h.get("status") or {}).get("online", True):
            continue
        gpus = (h.get("specs") or {}).get("gpu", h.get("gpu"))
        # gpu spec is either a dict {model_key: {...}} (v0 carryover) or a list of gpu dicts.
        if isinstance(gpus, dict):
            items = list(gpus.items())
        elif isinstance(gpus, list):
            items = [(_first(g, "model", "name", "type") or "", g) for g in gpus if isinstance(g, dict)]
        else:
            items = []
        for mkey, info in items:
            if not isinstance(info, dict):
                continue
            mkey = str(mkey)
            parts = mkey.split("-")
            label = parts[0] if parts and parts[0] else (_first(info, "model", "name") or "")
            vram = _parse_vram(_first(info, "vram", "vram_gb", "gpu_vram"))
            iface = "pcie" if info.get("pcie") else next((p for p in parts if p in _IFACE_TOKENS), None)
            key = canonicalize(label, vram, iface)
            if key is None:
                unmapped[mkey] = unmapped.get(mkey, 0) + 1
                continue
            price = _first(info, "price", "price_per_hour", "pricePerHour", "hourly_price", "cost")
            amount = _first(info, "amount", "available", "count", "available_count")
            agg = per_model.setdefault(key, ModelAgg())
            agg.add(ondemand=price, available=(int(amount) if amount is not None else 0), providers=1)
            if price:
                parsed_any = True
    if nodes and not parsed_any:
        logger.warning(
            "gpu_tensordock v2: %d nodes returned but no price parsed — verify field mapping. "
            "Sample node: %s", len(nodes), json.dumps(nodes[0], default=str)[:800],
        )
    return per_model, unmapped, data


_RUNPOD_QUERY = (
    '{"query":"query { gpuTypes { id displayName memoryInGb communityCloud '
    'lowestPrice(input:{gpuCount:1,secureCloud:false}) '
    '{ uninterruptablePrice minimumBidPrice stockStatus } } }"}'
)


async def _collect_runpod(url: str) -> tuple[dict[str, ModelAgg], dict[str, int], object]:
    """RunPod GraphQL gpuTypes, community pool (secureCloud:false), no auth. Per-GPU on-demand
    (uninterruptablePrice) + bid (minimumBidPrice). MUST pass secureCloud:false (no-arg default
    is bogus). null on-demand = no community stock (a signal) → skipped. stockStatus is coarse
    (no numeric inventory without a key) → not stored as a count."""
    headers = {**_UA, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30.0, headers=headers) as c:
        resp = await c.post(url, content=_RUNPOD_QUERY)
        resp.raise_for_status()
        data = resp.json()
    per_model: dict[str, ModelAgg] = {}
    unmapped: dict[str, int] = {}
    for g in (data.get("data") or {}).get("gpuTypes", []):
        if not g.get("communityCloud"):
            continue
        lp = g.get("lowestPrice") or {}
        od = lp.get("uninterruptablePrice")
        if not od:
            continue  # null = no community stock right now
        label = g.get("displayName") or g.get("id") or ""
        key = canonicalize(label, g.get("memoryInGb"))
        if key is None:
            unmapped[label] = unmapped.get(label, 0) + 1
            continue
        per_model.setdefault(key, ModelAgg()).add(ondemand=od, spot=lp.get("minimumBidPrice"))
    return per_model, unmapped, data


async def _collect_computeprices(url: str) -> tuple[dict[str, ModelAgg], dict[str, int], object]:
    """computeprices.com /api/v1/gpu-prices — cross-provider consensus (aggregator venue, kept
    OUT of the blended median). Covers managed providers (Lambda/CoreWeave/etc.) in clean JSON
    so no fragile site scraping needed. Per row: gpu_slug, vram_gb, gpu_count, total_hourly_usd,
    pricing_type. Per-GPU = total_hourly_usd/gpu_count. Skips reserved/committed rows."""
    headers = dict(_UA)
    if settings.computeprices_api_key:
        headers["Authorization"] = f"Bearer {settings.computeprices_api_key}"
    async with httpx.AsyncClient(timeout=40.0, follow_redirects=True, headers=headers) as c:
        resp = await c.get(url)
        resp.raise_for_status()
        data = resp.json()
    per_model: dict[str, ModelAgg] = {}
    unmapped: dict[str, int] = {}
    for r in data.get("data", []):
        if r.get("commitment_months"):
            continue  # reserved/committed — not a live on-demand/spot signal
        label = r.get("gpu_slug") or r.get("gpu") or ""
        key = canonicalize(label, r.get("vram_gb"))
        if key is None:
            unmapped[label] = unmapped.get(label, 0) + 1
            continue
        cnt = r.get("gpu_count") or 1
        total = r.get("total_hourly_usd")
        per_gpu = (total / cnt) if (total and cnt) else r.get("price_per_hour_usd")
        agg = per_model.setdefault(key, ModelAgg())
        if (r.get("pricing_type") or "").lower() == "spot":
            agg.add(spot=per_gpu)
        else:
            agg.add(ondemand=per_gpu, providers=1)
    return per_model, unmapped, data


# Curated Azure GPU-VM SKU → (canonical model, GPU count). Azure's SKU number is vCPUs, NOT
# GPU count, so counts MUST be explicit (per-GPU = retailPrice / count). Unmapped GPU-family
# SKUs are logged. venue=hyperscaler → kept OUT of the marketplace blend (3-6x price levels).
AZURE_SKUS: dict[str, tuple[str, int]] = {
    # ND96*_H100_v5 = 8x H100 SXM (the "96" is vCPUs, not GPUs); all IB/flex/noIB variants.
    "Standard_ND96isr_H100_v5": ("h100_sxm", 8),
    "Standard_ND96is_H100_v5": ("h100_sxm", 8),
    "Standard_ND96isrf_H100_v5": ("h100_sxm", 8),
    "Standard_ND96is_noIB_H100_v5": ("h100_sxm", 8),
    "Standard_ND96is_flex_H100_v5": ("h100_sxm", 8),
    "Standard_NC40ads_H100_v5": ("h100_sxm", 1),
    "Standard_NC80adis_H100_v5": ("h100_sxm", 2),
    # A100 v4: ND96*_A100_v4 = 8x A100 80GB; NC*ads_A100_v4 = 1/2/4x A100 80GB.
    "Standard_ND96amsr_A100_v4": ("a100_80gb", 8),
    "Standard_ND96asr_A100_v4": ("a100_80gb", 8),
    "Standard_ND96ams_A100_v4": ("a100_80gb", 8),
    "Standard_ND96asr_v4": ("a100_40gb", 8),
    "Standard_NC24ads_A100_v4": ("a100_80gb", 1),
    "Standard_NC48ads_A100_v4": ("a100_80gb", 2),
    "Standard_NC96ads_A100_v4": ("a100_80gb", 4),
    # T4 (NC*as_T4_v3): single T4 except NC64 = 4.
    "Standard_NC4as_T4_v3": ("t4", 1),
    "Standard_NC8as_T4_v3": ("t4", 1),
    "Standard_NC16as_T4_v3": ("t4", 1),
    "Standard_NC64as_T4_v3": ("t4", 4),
}
_AZURE_REGION = "eastus"


async def _collect_azure(url: str) -> tuple[dict[str, ModelAgg], dict[str, int], object]:
    """Azure Retail Prices — hyperscaler on-demand + spot, anonymous OData. Targeted per-family
    queries (H100/A100/T4) keep it bounded. Spot rows carry 'Spot' or 'Low Priority' in the
    sku/meter name (NOT a `type` value). Per-GPU = retailPrice / curated count."""
    per_model: dict[str, ModelAgg] = {}
    unmapped: dict[str, int] = {}
    raw_items: list = []
    async with httpx.AsyncClient(timeout=40.0, headers=_UA) as c:
        for token in ("H100", "A100", "T4"):
            flt = (f"serviceName eq 'Virtual Machines' and armRegionName eq '{_AZURE_REGION}' "
                   f"and priceType eq 'Consumption' and contains(armSkuName,'{token}')")
            resp = await c.get(url, params={"$filter": flt})
            resp.raise_for_status()
            for it in resp.json().get("Items", []):
                raw_items.append(it)
                sku = it.get("armSkuName")
                mapping = AZURE_SKUS.get(sku)
                if not mapping:
                    if sku:
                        unmapped[sku] = unmapped.get(sku, 0) + 1
                    continue
                key, cnt = mapping
                price = it.get("retailPrice")
                if not price or not cnt:
                    continue
                per_gpu = price / cnt
                name = (it.get("skuName", "") + " " + it.get("meterName", ""))
                is_spot = ("Spot" in name) or ("Low Priority" in name)
                agg = per_model.setdefault(key, ModelAgg())
                agg.add(spot=per_gpu) if is_spot else agg.add(ondemand=per_gpu)
    return per_model, unmapped, {"region": _AZURE_REGION, "items": raw_items}


async def _collect_vast(url: str) -> tuple[dict[str, ModelAgg], dict[str, int], object]:
    """Vast.ai official bundles API. ToS REQUIRES the official API + free key (Vast bans
    anonymous systematic retrieval) — we never poll without VAST_API_KEY. Per-offer
    dph_total/num_gpus = $/GPU/hr on-demand; min_bid/num_gpus = bid. available = count of
    distinct rentable&unrented machines. gpu_name uses SPACES in responses."""
    if not settings.vast_api_key:
        raise RuntimeError("VAST_API_KEY not set — Vast skipped (ToS requires the official API key)")
    q = json.dumps({"rentable": {"eq": True}, "order": [["dph_total", "asc"]], "limit": 1000})
    headers = {**_UA, "Authorization": f"Bearer {settings.vast_api_key}"}
    async with httpx.AsyncClient(timeout=40.0, headers=headers) as c:
        resp = await c.get(url, params={"q": q})
        resp.raise_for_status()
        data = resp.json()
    per_model: dict[str, ModelAgg] = {}
    unmapped: dict[str, int] = {}
    avail_machines: dict[str, set] = {}
    for o in data.get("offers", []):
        name = o.get("gpu_name", "")
        vram = (o.get("gpu_ram") or 0) / 1024.0 if o.get("gpu_ram") else None
        key = canonicalize(name, vram)
        if key is None:
            unmapped[name] = unmapped.get(name, 0) + 1
            continue
        n = o.get("num_gpus") or 1
        agg = per_model.setdefault(key, ModelAgg())
        if o.get("dph_total") and n:
            agg.add(ondemand=o["dph_total"] / n)
        if o.get("min_bid") and n:
            agg.add(spot=o["min_bid"] / n)
        if o.get("rentable") and not o.get("rented"):
            avail_machines.setdefault(key, set()).add(o.get("machine_id"))
    for key, machines in avail_machines.items():
        per_model[key].add(available=len(machines))
    return per_model, unmapped, data


# Registry: source_id → collector coroutine. A source_id with no collector returns
# status "no_collector". Lambda/CoreWeave bespoke HTML scrapers are intentionally deferred —
# their managed on-demand prices are covered cleanly via the ComputePrices aggregator.
COLLECTORS = {
    "gpu_akash": _collect_akash,
    "gpu_clore": _collect_clore,
    "gpu_tensordock": _collect_tensordock_v2,
    "gpu_runpod": _collect_runpod,
    "gpu_computeprices": _collect_computeprices,
    "gpu_azure": _collect_azure,
    "gpu_vast": _collect_vast,
}


# ─── Raw archive ─────────────────────────────────────────────────────────────────────

def _archive_raw(source_id: str, raw_obj: object, ts: datetime) -> str | None:
    """Gzip the raw response to gpu_raw_dir/<source>/<YYYY-MM-DD>/<ts>.json.gz.
    Best-effort: archive failure must never break ingest."""
    try:
        day = ts.strftime("%Y-%m-%d")
        d = Path(settings.gpu_raw_dir) / source_id / day
        d.mkdir(parents=True, exist_ok=True)
        fp = d / (ts.strftime("%Y%m%dT%H%M%SZ") + ".json.gz")
        with gzip.open(fp, "wt", encoding="utf-8") as f:
            json.dump(raw_obj, f, separators=(",", ":"))
        return str(fp)
    except Exception as e:  # pragma: no cover - disk/permission issues shouldn't fail ingest
        logger.warning("gpu raw-archive failed for %s: %s", source_id, e)
        return None


# ─── Series writers ──────────────────────────────────────────────────────────────────

async def _upsert(db, series: str, ts: datetime, value: float, meta: dict) -> None:
    stmt = pg_insert(TimeseriesPoint).values(
        series=series, ts=ts, value=value, meta=meta,
    ).on_conflict_do_update(
        index_elements=["series", "ts"],
        set_={"value": value, "meta": meta},
    )
    await db.execute(stmt)


async def ingest_source(source_id: str) -> dict:
    """Fetch+parse ONE GPU rental source; write its per-(source,model) series."""
    collector = COLLECTORS.get(source_id)
    if collector is None:
        return {"source_id": source_id, "status": "no_collector"}

    async with async_session() as db:
        src = await db.get(Source, source_id)
        if not src:
            return {"source_id": source_id, "status": "missing"}
        if not src.enabled:
            return {"source_id": source_id, "status": "disabled"}

        try:
            per_model, unmapped, raw_obj = await collector(src.url)
        except Exception as e:
            logger.error("gpu_rental fetch/parse failed for %s: %s", source_id, e)
            return {"source_id": source_id, "status": "fetch_error", "error": str(e)}

        now = datetime.now(timezone.utc)
        venue = SOURCE_VENUE.get(source_id, "marketplace")
        prefix = source_id.removeprefix("gpu_")  # e.g. "akash"
        models_written = 0

        for key, agg in per_model.items():
            cls = gpu_class(key)
            base_meta = {"source": source_id, "venue": venue, "gpu_class": cls, "model": key}
            od_median = median(agg.ondemand) if agg.ondemand else None
            sp_median = median(agg.spot) if agg.spot else None

            if od_median is not None:
                await _upsert(db, f"gpu_{prefix}_{key}_ondemand", now, od_median,
                              {**base_meta, "n_offers": len(agg.ondemand)})
            if sp_median is not None:
                await _upsert(db, f"gpu_{prefix}_{key}_spot", now, sp_median,
                              {**base_meta, "n_offers": len(agg.spot)})
            if od_median and sp_median:
                await _upsert(db, f"gpu_{prefix}_{key}_spot_ratio", now, sp_median / od_median, base_meta)
            if agg.available is not None:
                await _upsert(db, f"gpu_{prefix}_{key}_avail", now, float(agg.available), base_meta)
            if agg.total and agg.total > 0:
                occ = (agg.total - (agg.available or 0)) / agg.total * 100.0
                await _upsert(db, f"gpu_{prefix}_{key}_occupancy", now, occ, base_meta)
            if agg.providers and agg.providers > 0:
                await _upsert(db, f"gpu_{prefix}_{key}_providers", now, float(agg.providers), base_meta)
            models_written += 1

        src.last_fetched_at = now
        await db.commit()

    archive_path = _archive_raw(source_id, raw_obj, now)
    if unmapped:
        logger.info("gpu_rental %s: %d unmapped labels (top: %s)", source_id, len(unmapped),
                    sorted(unmapped.items(), key=lambda x: -x[1])[:5])
    logger.info("gpu_rental %s: %d models written, venue=%s, archived=%s",
                source_id, models_written, venue, bool(archive_path))
    return {
        "source_id": source_id,
        "status": "ok",
        "venue": venue,
        "models_written": models_written,
        "unmapped_labels": unmapped,
        "archived": archive_path,
    }


# ─── Cross-source aggregates ───────────────────────────────────────────────────────────

async def _latest_per_source(db, suffix: str) -> dict[str, dict[str, float]]:
    """Latest value of every `gpu_<src>_<model>_<suffix>` series, grouped as
    {canonical_model: {source_id: value}}. Uses each series' most recent ts."""
    pattern = f"gpu_%_{suffix}"
    rows = (
        await db.execute(
            select(TimeseriesPoint.series, TimeseriesPoint.value, TimeseriesPoint.ts, TimeseriesPoint.meta)
            .where(TimeseriesPoint.series.like(pattern))
            .order_by(TimeseriesPoint.series, TimeseriesPoint.ts.desc())
        )
    ).all()
    out: dict[str, dict[str, float]] = {}
    seen: set[str] = set()
    for r in rows:
        if r.series in seen:
            continue  # first row per series = latest (ordered desc)
        seen.add(r.series)
        meta = r.meta or {}
        src = meta.get("source")
        model = meta.get("model")
        if not src or not model:
            continue
        out.setdefault(model, {})[src] = float(r.value)
    return out


async def recompute_aggregates() -> dict:
    """Blend latest per-source series into headline cross-source `gpu_<model>_*` series.

    Only marketplace+neocloud venues feed the blended median (hyperscaler & aggregator
    stay separate). Run after all sources polled. Writes one timestamp for the batch."""
    now = datetime.now(timezone.utc)
    written = 0
    async with async_session() as db:
        od = await _latest_per_source(db, "ondemand")
        ratio = await _latest_per_source(db, "spot_ratio")
        avail = await _latest_per_source(db, "avail")

        models = set(od) | set(ratio) | set(avail)
        for model in models:
            cls = gpu_class(model)
            meta = {"gpu_class": cls, "model": model}

            blend_prices = [v for s, v in od.get(model, {}).items()
                            if SOURCE_VENUE.get(s) in _BLEND_VENUES]
            if blend_prices:
                await _upsert(db, f"gpu_{model}_ondemand_median", now, median(blend_prices),
                              {**meta, "n_sources": len(blend_prices)})
                written += 1

            blend_ratios = [v for s, v in ratio.get(model, {}).items()
                            if SOURCE_VENUE.get(s) in _BLEND_VENUES]
            if blend_ratios:
                await _upsert(db, f"gpu_{model}_spot_ratio", now, median(blend_ratios),
                              {**meta, "n_sources": len(blend_ratios)})
                written += 1

            blend_avail = [v for s, v in avail.get(model, {}).items()
                           if SOURCE_VENUE.get(s) in _BLEND_VENUES]
            if blend_avail:
                await _upsert(db, f"gpu_{model}_avail_count", now, float(sum(blend_avail)),
                              {**meta, "n_sources": len(blend_avail)})
                written += 1

        await db.commit()
    logger.info("gpu_rental recompute_aggregates: %d headline series written", written)
    return {"status": "ok", "series_written": written}
