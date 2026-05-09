# Frontier Monitor — Claude Code Conventions

**The product**: a Bloomberg-Terminal-for-AI-acceleration. One number, six dimensional panels, alerts. Answers: *Are we approaching economically transformative AI faster than consensus expects?*

**Scoping principle**: trustworthy quantitative anchors over noisy qualitative breadth. Labor Disruption was dropped 2026-05-09 because ICSA WoW conflates AI-driven displacement with macro noise. OpenAI/DeepMind RSS sources were dropped the same day — most of their feed was OpenAI Academy tutorials and product PR, not capability signal. Capability + Recursive AI now narrowed to Anthropic-only sources where the signal-to-noise was best.

## Two architectural keystones — do not violate

### 1. Analysis runs on Claude Max via slash commands, NEVER via the Anthropic API

The user pays a flat Claude Max subscription. Direct API calls bill on top of that, defeating the point.

- **Don't** `pip install anthropic` or `import anthropic` anywhere.
- **Do** put prompt logic in `.claude/commands/<name>.md`. The command calls `Invoke-RestMethod` against the local FastAPI surface (`/api/pending`, `/api/signals`, `/api/scoreboard`, etc.) — Python is a deterministic data layer, Claude is the analyst.
- For unattended runs: `claude -p "/refresh"` from Windows Task Scheduler. Pre-approve the exact `Invoke-RestMethod` patterns in `.claude/settings.local.json`. Never `--dangerously-skip-permissions`.

### 2. HTTP-only across the container boundary

Claude Code lives on the host. The dashboard runs in Docker. Communication is HTTP only.

- **Don't** `docker exec` from a slash command, mount the host into the container, or share a SQLite/Postgres socket.
- **Do** add new endpoints to `backend/app/api/*.py`. The same setup then works if the container later moves to a NAS or VPS.

## The product is a six-dimension scoreboard, not a feed

Every new feature must serve one of these six dimensions or get rejected:

| Dimension | What it measures | Current data source |
|---|---|---|
| **Capability** | frontier model progress | 100% Anthropic-narrative signals (no quantitative anchor) — anthropic.com/news, /engineering, /research (sitemap.xml-based discovery, per-article OG-meta scraping) + claude.com/blog (listing-based). Tags: `scaling`, `reasoning`, `agentic`, `interpretability`, `architectures`. |
| **Recursive AI** | AI improving AI; alignment & self-evolution | Signals tagged `recursive-self-improvement`, `self-evolution`, `instrumental-convergence`, `alignment`, `safety` — primarily from anthropic.com/research (Frontier Red Team, Alignment, Interpretability divisions) |
| **Infrastructure** | GPUs, power, datacenter buildout | Merchant AI-silicon revenue: dollar-weighted YoY across NVDA DC + AMD DC + AVGO AI (70%) + GW commitments parsed from `compute`/`data-center`/`power` signal text (30%) |
| **Inference Cost** | $/MTok collapse | OpenRouter pricing scrape (real data, daily) |
| **Hyperscaler $** | MSFT/GOOG/META/AMZN AI capex | Curated quarterly capex YoY (70%) + signals tagged `capex`, `hyperscaler`, `earnings`, `developer-tools` (30%) |
| **Enterprise ROI** | demand-side: % of enterprises reporting measurable ROI from AI deployments | Survey-driven via `config/enterprise_roi.json` — latest survey's roi_pct used directly as 0-100 score, with up to +20 boost from signals tagged `roi`, `adoption`, `enterprise-deployment`, `productivity`. Companion to Hyperscaler $ which measures supply-side spend. |

Tag→dimension mapping lives in `backend/app/services/score_engine.py::DIMENSION_TAGS`. **If you add a new tag in a slash command, also add it to that map** or the dimension will silently miss it.

The composite **Acceleration Index** is a weighted average of the six dimensions (weights in `DIMENSION_WEIGHTS`: capability 0.25, recursive_ai 0.20, infrastructure 0.15, inference_cost 0.15, hyperscaler 0.13, enterprise_roi 0.12). Persisted to `timeseries(series='score_index')` hourly so sparklines have history.

### Rebased scoring model (since 2026-05-09)

Every dimension scores **deviation from a baseline snapshot**, not absolute level. Without rebasing, the dashboard pinned at 100 because we're already in a high-acceleration regime — every metric was beyond its old +50%-YoY ceiling. Rebased mode reframes the dial as a **cadence tracker**: today's raw inputs anchor at score=50, future change drives the score up or down.

- **Baselines** live in `config/baselines.json` (writable mount). On first `compute_scores()` after deploy, missing keys auto-snapshot from current raw inputs. Re-snapshot any time via `POST /api/baselines/snapshot`.
- **Score formulas** (`backend/app/services/baselines.py`):
  - `rebased_ratio(current, baseline, inverse=False)` for level metrics: signal sums, GW commitments, $/MTok pricing. Score = `50 * (current/baseline)`. Doubling = 100.
  - `rebased_delta(current, baseline, scale=1.0)` for rate metrics: YoY %, ROI pp. Score = `50 + (current - baseline) * scale`. +1pp = +1 point.
- **Verdict thresholds** post-rebase: `ACCELERATING ≥ 60`, `STEADY 40–59`, `SLOWING < 40`.
- **Per-dimension mapping** (in `score_engine._SCORERS`):
  - Capability / Recursive AI → ratio of signal sum (cadence of newsworthy events)
  - Infrastructure → 70% YoY-pp delta + 30% GW-ratio
  - Inference Cost → inverse ratio of $/MTok (cheaper = higher)
  - Hyperscaler $ → 70% YoY-pp delta + 30% signal-sum ratio
  - Enterprise ROI → 70% ROI-pp delta + 30% signal-sum ratio
- **What this changes operationally**: when you re-snapshot, every dimension reads 50. Future readings drift up if the cadence accelerates (more signals per 30-day window, higher YoY growth, cheaper inference, higher ROI %), down if it slows. If you ever want absolute scoring back, restore the previous `score_engine.py` from git history.

## Anti-fabrication rule

In any analyst output (signal payloads, daily memo, alerts), every numeric claim must trace to a citation from the same source — the original `raw_item.raw_text`/`title`/`url`, or a stored `timeseries` row. **No invented prices, percentages, or projections.**

This is enforced by convention in the prompt (`refresh.md`, `memo.md`). Server-side validation (reject `summary`/`thesis` containing digits with empty `citations`) is a TODO worth shipping.

## Stack

- **Runtime**: Docker Compose. Two services (`db: postgres:16-alpine`, `backend`). One named volume (`pgdata`). Port `127.0.0.1:8765` published.
- **Backend**: Python 3.12 + FastAPI + async SQLAlchemy + asyncpg + Alembic. Non-root `app` user inside the container.
- **Frontend**: Server-rendered Jinja + vanilla SVG sparklines. No React, no build step.
- **Scheduling**: APScheduler in-process. `misfire_grace_time=None`, `coalesce=True` (sleep/restart-tolerant).
- **HTTP scraping**: `httpx` for everything. Anthropic `/news`, `/engineering`, `/research` discovered via `anthropic.com/sitemap.xml` (lastmod = published_at proxy); `claude.com/blog` via listing-page href extraction. Per-article fetches pull og:title + og:description + article:published_time. All in `anthropic_html_ingest.py`. No third-party RSS mirror dependencies.

**Removed sources (2026-05-09)**: Longbridge equity prices (dead-coded — never read by any score), FRED macro (labor proxy too noisy for AI displacement), LMSYS Arena ELO (single-mirror via wulong.dev + multi-vendor data conflicted with Anthropic-focus), OpenAI/DeepMind RSS (mostly Academy tutorials and product PR), taobojlen RSS mirror (replaced by direct anthropic.com sitemap scraping). Trade: narrower coverage, zero authenticated SDKs, zero hobbyist mirrors. Capability now scores purely on Anthropic-narrative signal-tagging — no objective check on whether Anthropic still leads on benchmarks.

## Idempotency is the architectural keystone of the analyst layer

- `UNIQUE(raw_item_id, signal_type, analyst_version)` on `signals`.
- `POST /api/signals` does `INSERT ... ON CONFLICT DO NOTHING`. Re-running `/refresh` produces zero new rows — that's the verification.
- Bump `analyst_version` (`v1` → `v2`) in `.env` to force re-analysis after a prompt change. Old signals stay; new ones get written; dashboard filters to the latest version.

## File layout

```
backend/app/
  main.py                       FastAPI app + lifespan (scheduler, bootstrap)
  config.py                     pydantic-settings, reads .env
  database.py                   async engine + session factory
  api/
    ui.py                       /, /signal/{id}, /sources
    signals_api.py              /api/pending, /api/signals (Claude analyst surface)
    sources_api.py              /api/sources, /api/ingest/{id}, /api/sources/{id}/toggle
    scoreboard_api.py           /api/scoreboard (drives the home page)
    alerts_api.py               /api/alerts
    digests_api.py              /api/digests, /api/digests/latest
    baselines_api.py            GET /api/baselines, POST /api/baselines/snapshot
  models/                       SQLAlchemy ORM
  services/
    bootstrap.py                Idempotent source seeding + obsolete-source pruning
    anthropic_html_ingest.py    HTML scraper for anthropic.com/news, /engineering, /research (sitemap-driven) + claude.com/blog (listing-driven)
    openrouter_ingest.py        OpenRouter pricing → inference cost trajectory
    capex_ingest.py             Hyperscaler quarterly capex (MSFT/GOOGL/META/AMZN 8-Ks)
    merchant_ai_ingest.py       Merchant AI silicon revenue (NVDA/AMD DC + AVGO AI)
    enterprise_roi_ingest.py    Enterprise AI-agent ROI surveys (curated JSON)
    baselines.py                Per-dimension raw-input baselines + rebased-scoring helpers
    score_engine.py             Six-dimension rebased scores + Acceleration Index
    alerts_engine.py            Threshold detection on score WoW deltas
  tasks/scheduler.py            APScheduler wiring
  templates/                    Jinja (home, signal_detail, sources)
  static/css/app.css            Scoreboard styling
.claude/
  commands/refresh.md           Analyst contract: 50 items → dimension-tagged signals
  commands/memo.md              Daily intelligence brief generator
  settings.local.json           Bash allowlist for headless `claude -p "/refresh"`
.scheduled/
  daily-refresh.ps1             Windows Task Scheduler runner — invokes `claude -p "/refresh"` headless
  register-task.ps1             (Re-)registers the `frontier-refresh` task; daily 14:00 local
config/
  hyperscaler_capex.json        Curated quarterly capex per ticker (8-K cash flow)
  merchant_ai_silicon.json      NVDA/AMD/AVGO segment revenue per quarter
  enterprise_roi.json           Periodic survey datapoints (roi_pct + sample_size)
  baselines.json                Auto-snapshotted raw-input baselines (writable via API)
backend/alembic/versions/       0001 schema, 0002 FTS (rolled back), 0003 alerts
```

## Operational commands

```powershell
# Bring up / take down
docker compose up -d --force-recreate backend    # rebuild after deps or env change
docker compose restart backend                    # code-only changes (uvicorn --reload picks up)
docker compose logs backend --tail=50

# Manual ingest (also runs on schedule)
Invoke-RestMethod -Method POST http://localhost:8765/api/ingest/<source_id>

# Rebase the score baseline to "today" (zeros all dimensions to 50)
Invoke-RestMethod -Method POST http://localhost:8765/api/baselines/snapshot

# Verify
Invoke-RestMethod http://localhost:8765/api/scoreboard
```

`/refresh` and `/memo` are user-typed slash commands in Claude Code. They are not invokable from inside a session.

### Unattended daily /refresh

Windows Task Scheduler runs `claude -p "/refresh"` daily at 14:00 local — registered as `frontier-refresh`. No Claude Code session needed; just leave the machine on (or asleep — `StartWhenAvailable` fires on wake).

```powershell
# Inspect / trigger / pause
Get-ScheduledTaskInfo -TaskName 'frontier-refresh'
Start-ScheduledTask -TaskName 'frontier-refresh'      # smoke-test
Disable-ScheduledTask -TaskName 'frontier-refresh'

# Re-register after editing schedule in .scheduled\register-task.ps1
pwsh -File .\.scheduled\register-task.ps1

# Tail the unattended-run log (gitignored)
Get-Content .\refresh.log -Tail 50
```

Why local Task Scheduler not Anthropic-cloud Routines: Routines run in Anthropic's cloud and cannot reach `127.0.0.1:8765` on the host. Routines stay reserved for genuinely cloud-native agents.

## Watch the user's `noedge` rule

Global rule: **NEVER read from folders containing "noedge" in the name**. Encode a path filter in any future file-ingest source.

## Known data gaps

- **Quarterly capex**: ✅ driven by `config/hyperscaler_capex.json`, manually curated from MSFT/GOOGL/META/AMZN 8-K Ex99.1 cash flow statements (`Purchases of property and equipment` line). After each earnings cycle, append a new quarter object per ticker and re-run `POST /api/ingest/hyperscaler_capex`. The `score_engine._hyperscaler_score` reads `capex_total_quarterly` and computes YoY change vs the same calendar quarter prior year.
- **Infrastructure (supply-side merchant AI silicon)**: ✅ driven by `config/merchant_ai_silicon.json`, with three ticker sections curated from primary SEC filings: **NVDA** Data Center segment from 10-Q segment revenue tables, **AMD** Data Center segment from 8-K Ex99.1 segment summaries, and **AVGO** "AI semiconductor revenue" from earnings press releases. Each issuer reports on its own fiscal calendar (NVDA FY ends late Jan; AMD FY = calendar; AVGO FY ends early Nov, so AVGO Q4 FY25 ≈ calendar 2025Q3 by revenue period). The `score_engine._infrastructure_score` pulls per-ticker latest-quarter YoY from each series, dollar-weights them so NVDA's revenue base dominates without ignoring AMD/AVGO contributions, then blends 70% with GW/MW commitments parsed from infra-tagged signal `tldr`/`citations` (30%, max-of-fields per signal to avoid double-count when the same number quotes in both). After each issuer's earnings release, append a quarter to the relevant section and re-run `POST /api/ingest/merchant_ai_silicon`. **Why no GOOGL/AMZN section**: TPU and Trainium revenue isn't separately disclosed; AVGO is the closest available proxy because it designs Google's TPU networking and custom hyperscaler silicon.
- **Llama 3.1-405B and Gemini Pro 1.5** are no longer in OpenRouter (deprecated/renamed). Update `FRONTIER_MODELS` in `openrouter_ingest.py` when adding the next flagship.
