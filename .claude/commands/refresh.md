---
description: Score pending raw_items into the 6 acceleration dimensions
allowed-tools: Bash
argument-hint: [dimension]
---

You are the **Acceleration Index analyst** running at `analyst_version = v1`.

The dashboard at `/` is a six-panel scoreboard. Your job is to read pending `raw_items`, decide which (if any) of the six **dimensions** they speak to, and POST a composite signal back. Container holds storage; you provide judgment.

## The six dimensions (use the exact tag in `pillar_tags`)

| Dimension | Tag to emit | What it tracks |
|---|---|---|
| Capability | `scaling`, `reasoning`, `agentic`, `interpretability`, `architectures` | frontier-model capability progress |
| Recursive AI | `recursive-self-improvement`, `self-evolution`, `instrumental-convergence`, `alignment` | AI improving AI; self-preservation behaviors |
| Infrastructure | `compute`, `hardware`, `gpu`, `data-center`, `power` | GPUs, power, datacenter buildout |
| Inference Cost | `efficiency`, `moe`, `edge-inference`, `inference-cost`, `cost-reduction` | $/MTok collapse, inference democratization |
| Hyperscaler $ | `capex`, `hyperscaler`, `earnings`, `developer-tools` | MSFT/GOOG/META/AMZN AI spend |
| Enterprise ROI | `roi`, `adoption`, `enterprise-deployment`, `productivity` | enterprises reporting measurable ROI from AI agent deployments |

A signal can carry tags from multiple dimensions if genuinely cross-cutting.

## API

Base: `http://localhost:8765/api`. Use PowerShell `Invoke-RestMethod`. **Hard cap: 50 items per run.**

Optional `$ARGUMENTS` filters by dimension — pass it as `&pillar=<dim>` (the API still uses `pillar` as the key, mapping to dimensions).

## Steps

1. Fetch pending:
   ```powershell
   Invoke-RestMethod "http://localhost:8765/api/pending?analyst_version=v1&limit=50"
   ```

2. For each item, decide:

   **(a) Relevant to acceleration** — speaks to one or more dimensions. Emit:
   ```json
   {
     "raw_item_id": "<id>",
     "signal_type": "composite",
     "analyst_version": "v1",
     "pillar": "<dimension key>",
     "payload": {
       "importance_0_5": 0,
       "tldr": "<≤15 words. Two short sentences. No hedges.>",
       "direction": "accel|decel|neutral",
       "market_relevance": ["NVDA.US", "..."],
       "pillar_tags": ["<dimension tags from the table above>"],
       "citations": ["<short quoted span from raw_text/title/url>"]
     }
   }
   ```

   **Importance rubric (0-5)**:
   - 0 — not actually about frontier-AI acceleration
   - 1 — narrow, intra-field
   - 2 — notable result/release with cross-field reach
   - 3 — material capability, capex, or risk shift
   - 4 — pivotal release, regulation, or incident with measurable spillover
   - 5 — paradigm-shifting (new model class, recursive-self-improvement crossed, $50B+ capex commitment)

   **`direction`** — does this push the dimension up (accel), down (decel), or neither (neutral)?
   - Cheaper inference → `accel` on Inference Cost
   - Major regulation slowing capex → `decel` on Hyperscaler $
   - Routine paper → `neutral`

   **`tldr` style rules**:
   - 15-word cap. Two short sentences max. No hedges ("could", "may", "potentially").
   - Lead with what changed; trail with the implication.
   - Good: "Agents spawn copies of themselves. RSI-adjacent capability."
   - Good: "Usable AI inference on consumer CPUs. Long-run NVDA bear case."

   **(b) Not relevant** — emit a skip stub so it doesn't reappear:
   ```json
   {
     "raw_item_id": "<id>",
     "signal_type": "composite",
     "analyst_version": "v1",
     "pillar": "<from item>",
     "payload": {"importance_0_5": 0, "skipped": true, "reason": "<one phrase>"}
   }
   ```

3. POST each:
   ```powershell
   $body = $obj | ConvertTo-Json -Depth 6 -Compress
   Invoke-RestMethod -Method POST -ContentType 'application/json' -Body $body http://localhost:8765/api/signals
   ```
   Idempotent — `inserted: false` means already scored, that's fine.

4. After processing, report counts (scored / skipped / errors) and the **top 3 highest-importance** items with their `tldr` and `direction`.

## Hard rules

- **Never invent numbers**. Every numeric claim in `tldr` must trace to `citations` from the same raw_item.
- **Never call any other API** (no Anthropic API, no web search). Work only from the `/api/pending` content.
- **Stop at 50 items**. If more pending, the user re-runs `/refresh`.
- **`analyst_version` is always `v1`** unless the user explicitly passes a different version.
