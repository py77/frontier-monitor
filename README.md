# Frontier Monitor

A Bloomberg-Terminal-for-AI-acceleration. One number, six dimensional panels, alerts. Answers: *Are we approaching economically transformative AI faster than the day we benchmarked?*

**Architecture rule (do not violate)**: analysis runs on Claude Max via slash commands, not the Anthropic API. The Docker backend is a deterministic data layer; Claude Code is the analyst, hitting the local FastAPI surface (`/api/pending`, `/api/signals`, `/api/scoreboard`) over HTTP.

## Six dimensions

| Dimension | Source |
|---|---|
| **Capability** | Anthropic-narrative signals — `anthropic.com/news`, `/engineering`, `/research` (sitemap-driven) + `claude.com/blog` |
| **Recursive AI** | Same Anthropic feeds, filtered to alignment / interpretability / Frontier Red Team |
| **Infrastructure** | Merchant AI silicon revenue YoY (NVDA DC + AMD DC + AVGO AI) blended with GW power-commitment signals |
| **Inference Cost** | OpenRouter frontier-model pricing (daily) |
| **Hyperscaler $** | Curated quarterly capex (MSFT/GOOGL/META/AMZN 8-K cash flows) blended with capex-tagged signals |
| **Enterprise ROI** | Periodic survey datapoints (% reporting measurable AI ROI) blended with adoption-tagged signals |

Composite **Acceleration Index** = weighted average of the six.

## Rebased scoring

Each dimension scores **deviation from a baseline snapshot**, not absolute level. Today's raw inputs anchor at score=50; future cadence drives the dial up or down. Verdicts: `ACCELERATING ≥ 60`, `STEADY 40–59`, `SLOWING < 40`. Re-snapshot any time via `POST /api/baselines/snapshot`.

## Stack

- **Runtime**: Docker Compose (postgres:16-alpine + Python 3.12 backend)
- **Backend**: FastAPI + async SQLAlchemy + asyncpg + Alembic + APScheduler
- **Frontend**: Server-rendered Jinja with vanilla SVG sparklines (no React, no build step)
- **Scraping**: `httpx` only — Anthropic content via `sitemap.xml` discovery + per-article OG-meta. Zero third-party RSS mirrors, zero authenticated SDKs.

## Quick start

```powershell
copy .env.example .env
# .env defaults are fine for local dev — no API keys required
docker compose up -d
# UI: http://localhost:8765

# Pre-load Anthropic backlog (one-shot; afterwards the scheduler runs every 2h)
Invoke-RestMethod -Method POST http://localhost:8765/api/ingest/anthropic_news
Invoke-RestMethod -Method POST http://localhost:8765/api/ingest/anthropic_research
Invoke-RestMethod -Method POST http://localhost:8765/api/ingest/anthropic_engineering
Invoke-RestMethod -Method POST http://localhost:8765/api/ingest/claude_blog

# Score new items (open Claude Code in this directory, then type)
/refresh
```

## Unattended /refresh

Daily at 14:00 local via Windows Task Scheduler:

```powershell
pwsh -File .\.scheduled\register-task.ps1
```

Runs `claude -p "/refresh"` headlessly with the machine on. See [CLAUDE.md](CLAUDE.md) for full details.

## Project conventions

See [CLAUDE.md](CLAUDE.md) — covers the architectural keystones, dimension tagging contract, idempotency rules, anti-fabrication policy, and the rebased scoring model.

## Out of scope

- Twitter/X (paid API), Bloomberg/Reuters (licensed feeds)
- Anything that requires importing `anthropic` directly into the backend
