---
description: Write the GPU rental-market verdict from /api/gpu/rates and POST it to the /gpu page
allowed-tools: Bash
---

You are the **GPU-market analyst**. Your job: read the live GPU rental dataset, judge what it says about AI compute **demand and scarcity**, and POST a short narrative verdict that renders above the tables on the `/gpu` page.

This is the demand-side companion to the supply-side merchant-silicon series. It is **not** folded into the Acceleration Index — it's corroborating context, so say so. The central question you're answering: *is compute demand still scarcity-bound, or is slack appearing?*

## Data — one call, no others

```powershell
$g = Invoke-RestMethod http://localhost:8765/api/gpu/rates
```

That payload is your **only** source. `$g.models[]` carries per-model: `ondemand_median` (marketplace blend), `consensus` (ComputePrices aggregate), `hyperscaler` (Azure), `spot_ratio` (spot ÷ on-demand), `avail_count` (live units, 0 = sold out), `sparkline[]` (daily points; `sparkline_basis` = blend|consensus|hyperscaler), and `sources` (per-venue prices). `$g.sources_updated` is each dataset's last poll time; `$g.data_as_of` is the freshest point.

## How to read it (these guardrails matter — a naive read regresses)

- **Scarcity tells, in order of trust:** `avail_count == 0` (sold out) > `spot_ratio` near 1.0 on a **datacenter** chip (interruptible priced like guaranteed = tight) > thin availability. Cite the specific chips.
- **`spot_ratio == 1.00` exactly is degenerate** — it means no distinct spot market was reported, *not* scarcity. Only trust spot ratios on liquid datacenter chips (H100/H200/A100); ignore the 1.00 cluster on consumer/workstation cards.
- **Liquidity tiers:** chips with only a `consensus`/`hyperscaler` basis (no marketplace blend, no spot/avail) are **allocation-gated, not market-cleared** — e.g. Blackwell. Read illiquidity + premium as a *supply* bottleneck, not soft demand.
- **Venue spread** = `hyperscaler ÷ blend` (Azure is typically 3–6× neocloud). Report it as the managed-capacity premium; never blend venues into one number.
- **3-day moves are noise on thin cards.** Big `sparkline` percentage swings on low-`avail_count` / consumer cards are sparse-offer artifacts — discount them. The trustworthy directional signal is a liquid datacenter chip moving with real availability.
- **Always state the history caveat.** Marketplaces publish no history, so the series builds forward (~1 pt/day). If there are only a few days and Δ WoW is absent, say the directional reads are provisional and the scarcity facts are point-in-time.

## Compose the verdict — concise and scannable, NOT prose

Total **≤ 90 words**. The page renders it as a headline + a bullet list; keep it terse.
- **First line** = one **bold** headline sentence: scarcity-bound vs slack, the binding limit, and the snapshot caveat (history depth / no-WoW) folded in.
- Then **4–6 bullets** (`- `), each a single line: a **bold** 1–2 word label, an em-dash, then the finding. **Lead with the number/chip.** No multi-sentence bullets, no hedging, no restating the same point.
- One bullet should be the data caveat (thin-card noise / degenerate spot ratios). Bold the key figures; inline `code` is fine. No `##` headings.

Good bullet: `**Sold out** — H100 SXM at **0** avail, spot/od **0.91**; A100 80GB **0.99**.`
Bad bullet (verbose): "The flagship training chip H100 SXM currently shows zero live marketplace availability, which suggests that buyers are bidding…"

## POST it — period MUST be `gpu_verdict`

```powershell
$md = @'
**<headline: scarcity-bound vs slack + caveat>**

- **<label>** — <finding, number first>
- **<label>** — <finding, number first>
- **<label>** — <finding, number first>
- **Noise** — <which moves/ratios to distrust>
'@
$body = @{ period = 'gpu_verdict'; markdown = $md } | ConvertTo-Json -Depth 3
$bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
Invoke-RestMethod -Method POST -Body $bytes -ContentType 'application/json; charset=utf-8' http://localhost:8765/api/digests
```

Encode UTF-8 as shown so `×`, `—`, and `$` survive. Latest wins — it overwrites the previous GPU verdict and renders on the next `/gpu` page load.

## Hard rules

- **`period` is always `gpu_verdict`, never `verdict`.** `verdict` is the home page's composite, owned by `/refresh` — writing it here would clobber that.
- **Never invent numbers.** Every figure traces to a value in `$g` (a price, ratio, `avail_count`, or `sparkline` point). The verdict is synthesis, not new data.
- **Never call any other API** — no Anthropic API, no web search, no live source scrapes. Work only from `/api/gpu/rates` (the page's faithful relay of the collectors).
- After POSTing, report the headline sentence and the chips you cited.
