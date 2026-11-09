# Tarot LLM Oracle v3 (web edition)

The web version of `tarot_llm_oracle_v3.py`. The model plans the spread, Python
draws the cards from the full 78-card deck, and Gemma 26B interprets them.
Deck, spreads, prompts and parsing are unchanged from the desktop v3.

## Changes from the desktop version

- Uses the same Gemma 26B providers as Aether Harbor, but tries Google AI Studio
  first (then Cloudflare Workers AI, then OpenRouter), so the two apps start on
  different free quotas. Thinking is off/minimal. There is no
  model selection, LM Studio endpoint or output folder.
- The analysis is unchanged: a planning call first decides the spread (how many
  cards and the sub-question each card answers), then the reading call uses the
  same prompt and output format as the desktop v3 (symbolic notes, reading,
  domain / technical lens, synthesis, what to keep, related questions,
  synthesis update).
- Only the presentation is new: the planner's roles appear on card tiles,
  sections get headings, and related questions can be clicked to use as the
  next facet. The model's symbolic notes appear at the top of the reading;
  there is no separate thinking pane.
- There is no auto-delay setting. The server limits each visitor IP to
  `TAROT_READINGS_PER_HOUR` readings, and an auto-loop stops after
  `TAROT_AUTO_LOOP_MAX` readings.
- Nothing is saved to files. Readings can be selected and copied directly.
  Earlier readings (used for synthesis) are remembered only in each visitor's
  browser, so nobody sees anyone else's readings. "Forget" clears them.

## Deploy on Render

1. New → Web Service → this repository. Render detects the `Dockerfile`
   (or use the `render.yaml` blueprint). Choose the Free plan.
2. Set the environment variables to the same values as Aether Harbor:
   `GOOGLE_API_KEY`, `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `OPENROUTER_API_KEY`.
   You can also attach the same Environment Group that Aether Harbor uses.
3. Health check path: `/healthz`.

Optional settings: `TAROT_READINGS_PER_HOUR` (default 30, 0 = unlimited),
`TAROT_AUTO_LOOP_MAX` (default 10, 0 = unlimited), `TAROT_MAX_JOBS` (default 4
concurrent readings), `TAROT_MAX_CARDS` (default 12 cards per spread).

Each reading uses two model calls (one to plan the spread, one for the reading),
from the same free quota as Aether Harbor. Render Free sleeps when idle and can
take about a minute to wake up.

## Run locally

```bash
pip install -r requirements.txt
GOOGLE_API_KEY=... python run_web_server.py --port 8000
```
