# Tapehawk — notes for Claude Code

Tapehawk is a market-news site (Flask + gunicorn on Render, one worker,
sqlite on a 1 GB disk) at tapehawk.onrender.com. Tabs: Live (the wire),
Halts, SEC Filings, Hidden Gems, Snipe. Halthawk (a separate repo and
service) reads this site's APIs to trade. Pushing to `main` deploys.

## Rules that never change

- **Credentials never touch the repo or a chat.** `ALPACA_KEY_ID`,
  `ALPACA_SECRET_KEY`, `FMP_API_KEY`, `RTPR_KEY`, `SEC_USER_AGENT` are set
  in Render's Environment tab only. A key in git or a message is burned.
- The owner's email is used only as the SEC user agent, never sent
  elsewhere.
- Nothing on this site places an order. It informs; Halthawk trades.
- The owner is not a programmer: plain words, few knobs, honest numbers
  (every panel shows its n and what it cannot tell you).

## How it is wired

- `app.py` — routes and `start_once()`. gunicorn imports the app in the
  master before forking, so background threads start in
  `gunicorn.conf.py` `post_fork` plus a `before_request` fallback keyed on
  `os.getpid()`. ONE worker: two would open two news sockets.
- `news_stream.py` — the Alpaca (Benzinga) news websocket. Every headline
  is normalised, classified (`classify.py`: categories, importance, tone,
  impact), stored (`store.insert`) and published to SSE listeners
  (`/api/stream`) — browsers, Halthawk, and `snipe.py`.
- `wires.py` — press releases straight from the wire services:
  GlobeNewswire and PR Newswire RSS polled every 3 s (conditional GETs),
  plus the rtpr.io alert socket (`RTPR_KEY`; needs the "All articles" rule
  in their dashboard) which relays all four wires but sends only a link,
  so the article is fetched and parsed (`parse_rtpr_article`). Releases
  with a US ticker go through the same pipeline as socket headlines,
  source named. `note_arrival` keeps the who-was-first scoreboard (same
  ticker within 15 min = same story). The first rtpr article is logged as
  `wire rtpr: first article sample` — check it if headlines look wrong.
- `snipe.py` — the Snipe tab's engine: every FDA/buyout/contract/trial/
  partnership (or positive Big News) headline becomes a setup; its stock is
  polled every 2 s (Alpaca snapshots, IEX) for the move since the story,
  the speed, and the distance to the LULD halt band (estimated). States:
  fresh → moving → near → halted (wire headline / exchange feed / silence)
  → reopened; faded. `/api/snipe` is what Halthawk trades from.
- `halts.py` — Nasdaq's halt RSS (reason codes, resumption times),
  attached to the story behind each halt. `filings.py` — SEC EDGAR 13D/8-K
  watcher (the form type was renamed `SCHEDULE 13D` on 2024-12-18; both
  spellings are handled; EDGAR rate-limits with 429 and efts 403s).
  `gems.py` — Hidden Gems screen (Bellwether rules ported; FMP data).
  `outcomes.py` — grades headlines against FMP 5-minute bars.
- `store.py` — sqlite at `TAPEHAWK_DB`. Additive migrations only.
- Pages are single HTML files, no build step. Labels are uppercased by
  CSS, so browser tests compare case-insensitively.

## Tests

`sh tests/run.sh` — plain scripts, temp sqlite, fakes for every network
call; each prints `... ok`. The sandbox these were written in could not
reach Alpaca, FMP, sec.gov or the wires, so network behaviour was verified
from Render logs after deploy; keep it that way — fake the network in
tests, read the logs in production.

## Verifying a deploy

Owner uploads via GitHub's web UI; Render auto-deploys (service
`srv-daklrdvqj5pc73blegjg`). Look for `tapehawk: started`, `stream:
connected to Alpaca news`, `wire globenewswire: primed`, `wire rtpr:
connecting`. Duplicate uploads sometimes land as `index_1.html`: delete
those.
