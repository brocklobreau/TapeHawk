# Tapehawk — notes for Claude Code

Tapehawk is a market-news site (Flask + gunicorn on Render, one worker,
sqlite on a 1 GB disk) at tapehawk.onrender.com. Tabs: Live (the wire),
Halts, SEC Filings, Hidden Gems, Snipe. Halthawk (a separate repo and
service) reads this site's APIs to trade. Pushing to `main` deploys.

Since Halthawk v2 step 9 (2026-10-06) the press-release wires ARE the
stream: the Benzinga socket is off (not started; the module stays), every
headline goes out through `bus.py`, and every row and SSE payload carries
the wire's own publish time, the first paragraph, the rtpr article id and
rtpr's impact block. See "v2 step 9" below.

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
- `bus.py` — the one stream: `subscribe()` / `unsubscribe()` / `publish()`
  (queues for `/api/stream` listeners -- browsers, Halthawk -- and
  `snipe.py`), `status()` with the listener count. A full queue drops for
  that listener, never waits.
- `news_stream.py` — the Alpaca (Benzinga) news websocket, OFF since v2
  step 9: `app.start_once` no longer calls `start()`; `status()` says so
  (`enabled` False, `note`); `subscribe`/`_publish` are the bus's names.
  Kept whole and importable in case the Benzinga copy is ever wanted for
  the record. Every headline is normalised, classified (`classify.py`:
  categories, importance, tone, impact), stored (`store.insert`) and
  published on the bus.
- `wires.py` — press releases straight from the wire services:
  GlobeNewswire and PR Newswire RSS polled every 3 s (conditional GETs),
  plus the rtpr.io alert socket (`RTPR_KEY`; needs the "All articles" rule
  in their dashboard) which relays all four wires but sends only a link,
  so the article is fetched and parsed (`parse_rtpr_article` -- Halthawk's
  `rtpr_archive.py` carries a copy and its test checks the two agree LINE
  FOR LINE, so change it in both repos or not at all). Releases with a US
  ticker go through the one pipeline, source named. `note_arrival` keeps
  the who-was-first scoreboard (same ticker within 15 min = same story;
  same-source pairs are skipped by design). `stream_status()` is what the
  Live tab's dot and `/api/feed["stream"]` read: connected while any wire
  source is up (`Feed.poll_once` marks a poll that answered `ok`; the rtpr
  socket marks itself when open). The first rtpr article is logged as
  `wire rtpr: first article sample` — check it if headlines look wrong.
- `snipe.py` — the Snipe tab's engine: every FDA/buyout/contract/trial/
  partnership (or positive Big News) headline becomes a setup; its stock is
  polled every 2 s (Alpaca snapshots, IEX) for the move since the story,
  the speed, and the distance to the LULD halt band (estimated). States:
  fresh → moving → near → halted (a halt headline on the stream / exchange
  feed / silence) → reopened; faded. It reads the bus (`start(store, bus,
  log)`), so with the Benzinga socket off its setups are wire releases;
  `EXCLUDE` and `AFTER_MOVE` refuse after-the-move pieces ("shares are
  trading higher after", "why ... is up", "Acme soars after"...) while a
  business number moving in a real release ("Revenue Jumps On ...",
  `METRIC_MOVE`) still counts. Halthawk no longer trades
  from `/api/snipe` (its v2 deleted the snipe paths); the tab stays as a
  display of the shared stream, and says so.
- `halts.py` — Nasdaq's halt RSS (reason codes, resumption times),
  attached to the story behind each halt. `filings.py` — SEC EDGAR 13D/8-K
  watcher (the form type was renamed `SCHEDULE 13D` on 2024-12-18; both
  spellings are handled; EDGAR rate-limits with 429 and efts 403s).
  `gems.py` — Hidden Gems screen (Bellwether rules ported; FMP data).
  `outcomes.py` — grades headlines against FMP 5-minute bars.
- `store.py` — sqlite at `TAPEHAWK_DB`. Additive migrations only.
  `headlines` gained `wire_pub`, `rtpr_id`, `impact` (JSON), `copy_of`
  (v2 step 9); `_row` adds `paragraph` (= the summary on rtpr rows only;
  None on RSS and Benzinga rows) and loads `impact`; `add_symbol` merges a ticker into a
  stored row.
- Pages are single HTML files, no build step. Labels are uppercased by
  CSS, so browser tests compare case-insensitively.

## v2 step 9 (Halthawk plan section 12; 2026-10-06)

- **Fields.** Every article dict, SSE payload and `/api/feed` row carries
  `author` (rtpr: which wire the release came off; RSS: None), `wire_pub`
  (the wire's publish time: RSS `pubDate`, rtpr `article_published_at`),
  `rtpr_id` (the frame's `article_id`/`id`, else the link's last part --
  the same rule as Halthawk's `rtpr_archive.rtpr_id_of`), `paragraph` (the
  release's first paragraph, rtpr only: an RSS description is NOT a
  paragraph, and Halthawk's reader keys its input shape on whether the
  paragraph is there), `impact` (rtpr's block: alert_kind,
  impact_score, impact_tier, event_type, impact_direction; None otherwise)
  and `copy_of`.
- **Two-ticker releases.** rtpr sends one frame per ticker with one link.
  `RtprSocket.on_frame` keys its seen-set on link + ticker (`pairs`): the
  first frame fetches and stores; a later frame with the same link merges
  its ticker into the stored row (`store.add_symbol`, counted `merged`),
  fetches nothing and sends nothing again (the row's id does not change;
  `/api/feed?symbol=` finds it under either ticker). When no row holds the
  link (the first frame named no ticker and the text named none) the
  release is fetched again under this ticker rather than lost. Halthawk re-counts
  tickers from the release text itself, so it does not wait on this.
- **Same-source copy mark.** `Source.copy_of`: a release this source
  delivered within the last 60 s (`COPY_WINDOW_S`) naming the same stock
  with the same headline (word overlap >= 0.5, `COPY_OVERLAP`) is the same
  release; on rtpr only (`COPY_BY_MINUTE`), so is the same ticker on the
  same wire in the same publish minute with the same first paragraph
  (`PARA_KEY_CHARS`; the header-line fallback can give one release two
  headlines, and a missing wire mark on one side is not held against the
  pair). A copy always names the same stock: two companies' template
  headlines in one minute, or one company's two releases in one minute,
  stay two stories. The copy is stored and sent WITH `copy_of` = the first row's
  id (the Live tab shows a "copy" tag), counted in `copies`, and is not an
  arrival for the scoreboard. Past the minute nothing here catches it;
  Halthawk's first-knowledge rule does.
- **The stream.** `bus.py` (above); `app.start_once` logs
  `benzinga socket: off -- the wires are the stream` instead of starting
  `news_stream`; `/api/feed["stream"]` and `/api/snipe["stream"]` are
  `wires.stream_status()`; `/api/status` keeps `benzinga` (news_stream's
  status) and adds `bus`. The Live tab's text reads "live · 2 of 3 wires".
- **Tests.** `test_wires.py`: the fields on the SSE payload and the stored
  row; a two-ticker release in one row; two URLs same headline within 60 s
  -> the second marked copy, the header-line copy by minute, a different
  release in another minute is not a copy, RSS copies by headline only;
  with the Benzinga socket off a GNW release reaches a bus listener and
  `/api/feed["stream"]` reports the wires (the test sets `app._started_pid`
  so the request hook starts no threads). `test_snipe_tab.py`: seven
  after-the-move headlines open no setup, the releases still do.

## Tests

`sh tests/run.sh` — plain scripts, temp sqlite, fakes for every network
call; each prints `... ok`. The sandbox these were written in could not
reach Alpaca, FMP, sec.gov or the wires, so network behaviour was verified
from Render logs after deploy; keep it that way — fake the network in
tests, read the logs in production.

## Verifying a deploy

Owner uploads via GitHub's web UI; Render auto-deploys (service
`srv-daklrdvqj5pc73blegjg`). Look for `tapehawk: started`, `benzinga
socket: off -- the wires are the stream`, `wire globenewswire: primed`,
`wire rtpr: connecting` (no `stream: connected to Alpaca news` any more:
that socket is off). `bus.py` is a new file: a missed upload breaks the
import of app.py, wires.py and news_stream.py, so upload it first.
Duplicate uploads sometimes land as `index_1.html`: delete those.
