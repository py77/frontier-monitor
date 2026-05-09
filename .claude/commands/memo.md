---
description: Generate the daily AI Acceleration intelligence memo
allowed-tools: Bash
---

Produce a daily intelligence memo and POST it to `/api/digests`. Format optimized for the question:

> "Are we approaching economically transformative AI faster than consensus expects?"

## Steps

1. Fetch the current scoreboard state:
   ```powershell
   $sb = Invoke-RestMethod http://localhost:8765/api/scoreboard
   $alerts = Invoke-RestMethod "http://localhost:8765/api/alerts?days=1"
   ```

2. Render markdown with this skeleton:

   ```markdown
   # AI Acceleration Memo — YYYY-MM-DD

   **Index: <X>/100** (<verdict>)  ·  WoW: <±X.X>

   ## What changed
   - **<importance>/5 [<dim>]** <tldr> — <implication>
   - …

   ## Dimension reads
   - **Capability <X>**: <one-line read>
   - **Recursive AI <X>**: <one-line read>
   - **Infrastructure <X>**: <one-line read>
   - **Inference Cost <X>**: <one-line read>
   - **Labor <X>**: <one-line read>
   - **Hyperscaler $ <X>**: <one-line read>

   ## Real or hype?
   <2-3 sentences distinguishing genuine acceleration signals from announcement-driven noise.>

   ## Investment implication
   <2-3 sentences. Tickers in `market_relevance` get explicit calls (long/short/watch).>

   ## Alerts
   - <severity> <headline>
   ```

3. POST it:
   ```powershell
   $period = (Get-Date -Format "yyyy-MM-dd")
   $body = @{ period = $period; markdown = $md } | ConvertTo-Json -Compress
   Invoke-RestMethod -Method POST -ContentType 'application/json' -Body $body http://localhost:8765/api/digests
   ```

## Hard rules

- All numbers in the memo must come from the scoreboard or signal `citations`. Do not introduce figures.
- Don't call any external API. Work only from `/api/scoreboard` and `/api/alerts`.
- The "Real or hype?" section is the load-bearing one — be specific about which signals look like genuine capability progress vs PR cycle.
- If a dimension is missing data ("LMSYS data pending"), say so honestly rather than padding.
