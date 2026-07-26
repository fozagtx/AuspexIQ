# AuspexIQ

Reads the signs before you launch. AuspexIQ is an MCP server that tells any
calling agent whether a YouTube niche is worth entering — using **live YouTube
Data API v3 data** to find outlier videos, measure saturation, and return a
verdict: **ENTER / CROWDED / AVOID**.

Every number in every response originates from a live API call made at request
time (or a short-TTL cache of a previous live response). There is no mock data,
no seed data, and no fixtures anywhere in the runtime path.

## Tools

| Tool | What it does | Typical cost |
|---|---|---|
| `scan_niche` | Assess a niche keyword: who ranks, concentration, outlier videos vs. each channel's own baseline, saturation score, and an ENTER/CROWDED/AVOID verdict | ~122 quota units uncached |
| `channel_outliers` | Reveal which of a channel's recent videos overperformed the channel's own baseline (median views of recent long-form uploads) | ~4–6 quota units uncached |

`scan_niche` inputs: `query` (required, 2–80 chars), `region_code` (default
`US`), `recency_days` (default 365, 30–1825), `max_results` (default 50,
10–50).

`channel_outliers` inputs: `channel` (required — a `UC...` channel ID, an
`@handle`, or a full youtube.com channel URL), `lookback_videos` (default 30,
10–100), `min_multiple` (default 2.5, 1.5–10).

Failures return a structured error, never a fabricated result:

```json
{"ok": false, "error": {"code": "MISSING_API_KEY | QUOTA_EXHAUSTED | YT_API_ERROR | CHANNEL_NOT_FOUND | INVALID_INPUT | UPSTREAM_TIMEOUT", "message": "...", "retryable": true}}
```

## Setup

Requires Python 3.11+ and a YouTube Data API v3 key.

1. Get an API key: [Google Cloud Console](https://console.cloud.google.com/) →
   create/select a project → enable **YouTube Data API v3** → Credentials →
   Create API key.
2. Install dependencies:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Environment variables

Copy `.env.example` and fill in your values (export them in your shell, or set
them in your host's dashboard — the server reads the process environment):

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `YT_API_KEY` | yes | — | YouTube Data API v3 key. If unset, the server starts (health checks pass) but every tool call returns the `MISSING_API_KEY` structured error. |
| `PORT` | no | `8000` | HTTP listen port. |
| `DAILY_UNIT_BUDGET` | no | `9000` | Hard daily cap on YouTube quota units the server will spend (Google's free tier is 10,000/day; the quota day resets at midnight Pacific). When spent, calls return `QUOTA_EXHAUSTED` with the reset time. |

## Local run

Streamable HTTP (the production transport — MCP endpoint at `/mcp`):

```bash
export YT_API_KEY=your-key-here
python -m src.server
# → http://localhost:8000/mcp   (MCP, streamable HTTP)
# → http://localhost:8000/healthz  (returns {"ok": true})
```

Stdio (for local MCP clients):

```bash
python -m src.server --stdio
```

Quick health check:

```bash
curl http://localhost:8000/healthz
```

## Inspector test

With the server running locally (or deployed), verify both tools with MCP
Inspector:

```bash
npx @modelcontextprotocol/inspector
```

1. Transport: **Streamable HTTP**.
2. URL: `http://localhost:8000/mcp` (or `https://<your-domain>/mcp`).
3. Connect → **List Tools** → both `scan_niche` and `channel_outliers` should
   appear.
4. Run one real call of each (e.g. `scan_niche` with any niche keyword, and
   `channel_outliers` with any real `@handle`). Open an outlier's `url` in a
   browser — the video must be watchable and its public view count should match
   the reported `views` within cache staleness.

## Deploy

Deploy to a host with automatic HTTPS. The repo ships a `Dockerfile`, so any
Docker host works. Avoid setups that sleep on idle — cold starts look broken
during marketplace review.

### Hugging Face Spaces (current deployment)

The production instance runs as a Docker Space at
`https://pima5-auspexiq.hf.space` (MCP endpoint: `/mcp`, health: `/healthz`).

To reproduce: create a Docker Space, push this repo's files with a YAML
frontmatter block prepended to `README.md` (`sdk: docker`, `app_port: 8000`),
and set `YT_API_KEY` as a Space **secret**:

```bash
hf auth login                       # token with Write access
hf repos create <user>/AuspexIQ --type space --sdk docker
hf upload <user>/AuspexIQ <staged-dir> . --type space
python -c "from huggingface_hub import HfApi; HfApi().add_space_secret('<user>/AuspexIQ', 'YT_API_KEY', '<key>')"
```

**Keep-awake:** free Spaces sleep after 48 hours without traffic. Point a free
uptime pinger (cron-job.org, UptimeRobot) at `/healthz` every 30-60 minutes —
it keeps the Space awake without spending any YouTube quota.

### Railway

```bash
railway login
railway init
railway up
```

Then in the Railway dashboard: set `YT_API_KEY` (and optionally
`DAILY_UNIT_BUDGET`) under Variables, and generate a public domain under
Settings → Networking. Railway injects `PORT` automatically.

### Fly.io

```bash
fly launch --no-deploy        # detects the Dockerfile; accept the defaults
fly secrets set YT_API_KEY=your-key-here
fly deploy
```

### Verify the deployment

- `curl https://<your-domain>/healthz` → `{"ok": true}`
- Repeat the Inspector test above against `https://<your-domain>/mcp` — both
  tools listed, one real call each succeeds, and an uncached `scan_niche`
  completes in under 25 seconds.

## Register on OKX.AI (A2MCP ASP, free tier)

Run this sequence from Claude Code once the HTTPS endpoint is live.

1. Install Onchain OS skills, then open a **new session**:

```
npx skills add okx/onchainos-skills --yes -g
```

2. Log in to the Agentic Wallet (have your email ready):

```
Log in to Agentic Wallet on Onchain OS with my email
```

3. Register the ASP (service type: **free** — the endpoint returns the result
   directly; no x402/payment integration):

```
Help me register an A2MCP ASP on OKX.AI using OKX Agent Identity from Onchain OS
```

   Use this marketplace copy verbatim during registration:

   - **Name:** `AuspexIQ`
   - **Tagline:** `Reads the signs before you launch. Know if a YouTube niche is worth entering.`
   - **Description:** `AuspexIQ is live YouTube analysis for agents. scan_niche returns saturation, breakout outlier videos, and an ENTER/CROWDED/AVOID verdict for any niche keyword. channel_outliers reveals which of a channel's videos overperformed its own baseline. Real API data on every call — no cached opinions, no hallucinated stats. Free per call.`
   - **Category:** Software services · **Price:** 0.00 USDT/use

4. List the ASP on the marketplace:

```
Help me list my ASP on OKX.AI using Onchain OS
```

OKX reviews submissions within 24 hours and sends the result to the email
registered with the Agentic Wallet. Until approved, the service can still be
called via its Agent ID.

## Operational notes

- **Quota:** an uncached `scan_niche` costs ~122 units (search 100 + video/
  channel lookups + up to 10 deep channel baselines), so the default 9,000-unit
  budget supports ~73 uncached scans/day, plus unlimited cache hits.
  `channel_outliers` costs ~4–6 units. Every response's `meta` reports
  `quota_units_spent` and `quota_units_remaining_today`.
- **Cache:** in-memory TTL cache keyed on normalized inputs — 6h for
  `scan_niche`, 3h for `channel_outliers`, capped at 500 entries (oldest
  evicted). Repeat calls return `meta.cache = "hit"` and spend 0 units.
- **Timeouts:** every upstream call is capped at 10s and surfaces as
  `UPSTREAM_TIMEOUT` if exceeded.
- **Logging:** one structured line per request (tool, params hash, cache
  hit/miss, units, latency, outcome). The API key is never logged.
