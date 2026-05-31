# Vercel Deployment — Handoff

Decision artifact. Read, decide, then ping Claude with the chosen path.

## What's being asked

Make the Frontier Monitor dashboard available at a public URL via Vercel, modeled on how `opicker-dashboard` was deployed.

## Why it's not a one-liner

Current Frontier Monitor stack:
- **Local-only**: FastAPI backend on `127.0.0.1:8765`, Postgres in Docker
- **Server-rendered**: Jinja templates produce HTML on each request
- **Background jobs**: APScheduler runs scrapers + scoring inside the Python process

Vercel doesn't run Docker, doesn't run long-lived Python processes, can't reach `localhost`. So "deploy to Vercel" implies one of three migrations.

## Three options (revisit your initial pick)

### A. Static snapshot dashboard ⭐ *(your initial pick — fastest ~1h)*

- Local cron: after `/refresh`, dump `/api/scoreboard` JSON → `dashboard/public/data/scoreboard.json`
- Same script: `git add → commit → push` to GitHub
- Vercel auto-rebuilds on push (free tier supports this)
- Static `index.html` (copy of current `home.html`) fetches the JSON, renders the same six panels + sparklines
- **Trade**: dashboard is ~24h-stale (matches the daily refresh cadence). No live API. Read-only.
- **No new infra**: no Supabase, no cloud DB, no serverless functions.

### B. Full opicker pattern — Next.js + Supabase *(half day)*

- Provision Supabase project (free tier OK for this volume)
- Migrate the schema (sources / raw_items / signals / timeseries / alerts / digests) to Supabase Postgres via Alembic
- Local backend dual-writes (or switches to) Supabase
- Build `dashboard/` as Next.js 16 + React 19 + Tailwind (mirrors `opicker-dashboard`)
- Vercel deploy
- **Trade**: live updates the moment `/refresh` runs locally. More moving parts, second DB to maintain.
- **Reuses your existing Supabase familiarity from opicker.**

### C. Tunnel — local backend + Vercel frontend *(~2h)*

- Cloudflare Tunnel or Tailscale Funnel exposes `localhost:8765` at a stable URL
- Next.js dashboard on Vercel calls that URL directly
- **Trade**: data freshness matches `/refresh` instantly, but dashboard breaks when your machine is off. Same availability profile as today, just the *viewing* moves to a public URL.

## Decisions to make before I build

1. **Pick A / B / C** (you initially said A; consider whether your opicker workflow makes B more natural)
2. **Dashboard scope**: scoreboard only vs scoreboard + signals feed + sources page
3. **Domain**: stick with `*.vercel.app` default, or set up a custom domain
4. **Visibility**: public dashboard is fine since the data is already public (Anthropic blog posts + SEC filings); confirm you're OK with the curated ROI/capex JSON being effectively published via the snapshot

## What changes regardless of choice

- Need a script that writes a snapshot of `/api/scoreboard` (option A directly publishes this; B+C wrap it in API routes; the data shape is the same)
- The `daily-refresh.ps1` Windows scheduled task gets a new step at the end (commit+push for A; nothing extra for B/C since Supabase is dual-written or backend is tunneled)

## Recommended next step

If you pick **A**, I build:
- `dashboard/index.html` (copied from `home.html`, modified to fetch local JSON)
- `dashboard/app.css` (copied from `static/css/app.css`)
- `dashboard/data/scoreboard.json` (initial empty placeholder)
- `.scheduled/snapshot-and-push.ps1` (snapshot + git push wrapper)
- Hook the new script into `daily-refresh.ps1`
- Vercel project link via `vercel` CLI (you'll need to install it: `npm i -g vercel` or use the GitHub integration on vercel.com)
- `vercel.json` (specifies the `dashboard/` subdirectory as the deploy root)

If you pick **B**, the work expands to roughly 4-6 steps (Supabase setup, schema migration, dual-write logic, Next.js scaffold, component port, env vars).

If you pick **C**, the work is similar to B without the DB migration — just frontend + tunnel config.

## Reference: opicker pattern observed

`opicker-dashboard/` is Next.js 16 + React 19 + Tailwind 4 + Supabase + Plotly + Recharts. The Python scripts at the project root (`option_picker_ibkr.py`, etc.) push data to Supabase, the Next.js app reads it. No GitHub Actions for ingest — local machine pushes; Vercel just serves.

That's the same shape as option B above.

---

When you've decided, reply with: `option A` / `option B` / `option C`, plus any deviation from defaults (scope, domain, etc.).
