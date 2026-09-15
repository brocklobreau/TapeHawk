# Tapehawk

Real-time market headlines with instant company context.

Live Benzinga headlines arriving in ~0.15 seconds, categorised and filtered,
with a research panel that answers "what is this company and is it cheap"
without leaving the page.

## Measured, not claimed

Alpaca's news websocket, sampled during market hours on 2026-09-15:

| | Alpaca (this) | FMP (previous) |
|---|---|---|
| Fastest delivery | **0.10s** | 636s (10.6 min) |
| Median delivery | **0.16s** | 1,586s (26 min) |
| Ticker tags | 100% of headlines | partial |
| Cost | free tier | paid |

Volume is **~0.8 headlines/minute** (~300-500/day). That is not a firehose,
and a meaningful share is editorial filler, which is why `classify.py`
matters as much as the speed: it drops 13F ownership churn and SEO listicles
("If You Invested $1000 In X 15 Years Ago...") so the real events are visible.

## Honest positioning

This is **not** faster than Newsquawk on breaking news and will not be.
Newsquawk's product is human analysts watching central bank feeds and
speaking into a live audio squawk. No aggregator beats that.

What this does that they don't: puts the company's valuation, margins and
cash flow one click from the headline.

## Setup

1. **Create the repo** and push these files.
2. **Render → New → Web Service**, point at the repo. `render.yaml` sets the
   rest (one worker, 1GB disk at `data/`, health check).
3. **Environment variables** (Render dashboard, never in the repo):
   - `ALPACA_KEY_ID` / `ALPACA_SECRET_KEY` — free paper-trading keys
   - `FMP_API_KEY` — the same key Bellwether uses
4. Deploy. The feed starts filling immediately; the page shows a live dot and
   the best latency seen so far.

## Files

| File | Role |
|---|---|
| `app.py` | Flask: SSE push, feed API, lookup API, gzip |
| `news_stream.py` | Alpaca websocket consumer, reconnects forever |
| `store.py` | SQLite archive (WAL, deduped, pruned) |
| `classify.py` | Category tagging + noise filtering |
| `research.py` | FMP fundamentals + transparent valuation read |
| `index.html` | The interface |

## Design notes

**One gunicorn worker, deliberately.** Each worker would open its own Alpaca
socket and write the same rows. Work per request is tiny, so one is plenty.

**SSE, not websockets, for the browser.** Traffic is one-directional and SSE
reconnects by itself — a phone that sleeps comes back without any code from
us. The 20-second keepalive matters: this feed is quiet for minutes at a
time and proxies close idle connections, which would look like the news
stopping.

**The stream's failure mode is silent.** If it dies the site serves stale
headlines and looks healthy. Hence the heartbeat in `/api/status` and the
live dot in the header — "the feed is dead" should be visible, not inferred.

**Keyword matching, not comprehension.** A "clinical" tag means the headline
mentions a trial or an FDA step. It does not mean the trial succeeded. The
tag is a pointer to go read the article.
