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

## Big News rules (2026-10-07)

Measured on the 120 newest Big News items, about half the rail was noise to
a trader: law-firm "investor reminder" releases, housekeeping (a notes
offering closing, tender-offer results, a reverse split, a SPAC's IPO
pricing, a monthly volume report), mega-cap releases that cannot move the
stock, and the same release shown two or three times. Four rules fix it;
none needs an AI. The plan is `../tapehawk-bignews-plan.md`.

1. **Law-firm solicitations are noise.** Two tiers in `classify`.
   `LAW_FIRM_PATTERNS` are shapes only a pitch has ("investor reminder",
   "lead plaintiff", "deal notice", "on behalf of investors", "investors
   who lost", LLP beside investors) and the firm names seen on the wires:
   one match makes `is_noise()` true and `noise_reason()` "law-firm
   solicitation". A one-word firm name (Pomerantz, Glancy, Monteverde...)
   is also somebody's surname, so it needs a second word from the trade in
   the same headline. `LAW_FIRM_GENERIC` are shapes a company's own release
   can share ("class action", "shareholder notice", "reminds shareholders",
   "under investigation", "securities fraud claims"): one of those is noise
   only with a `LAW_FIRM_SIGNALS` word beside it (contact, deadline,
   losses, join, a law firm, LLP/LLC...) and none of `LAW_FIRM_EXCEPTIONS`
   (settlement, dismissal, "without merit", a rights plan, a meeting, the
   SEC, a subpoena, an internal investigation). Noise rows are stored but
   hidden from the feed and never published, exactly like the 13F churn --
   the harshest mark in the system, so "Acme Announces Settlement of
   Securities Class Action", "Acme Adopts Shareholder Rights Plan", "Acme
   Receives Subpoena From SEC" and "Acme Provides Shareholder Update" stay
   LEGAL and reach Halthawk (all 128 solicitation rows in the 300 newest
   live feed rows are still noise under the two tiers).
2. **Routine housekeeping is never Big News.** `classify.ROUTINE_PATTERNS`
   is a tuple of (regex, plain words); a match caps `importance()` at 2,
   `big` False, appends "routine: <words>", and the dollar figure adds
   nothing. `ROUTINE_EXCEPTIONS` keep the real events out of it: a tender
   offer TO ACQUIRE shares (M&A), a special dividend (any wording: "special
   cash dividend", "special one-time dividend"), a dividend cut or
   suspension, a CEO exit, a contract "award" or dollar figure "award"
   ("Wins $300 Million Award From U.S. Army"), a received grant, topline /
   Phase results, a breakthrough designation, a definitive or merger
   agreement, a vote approving a merger, guidance raised or cut, index
   inclusion, "named a supplier". **A real event outranks the housekeeping
   beside it:** when a `_CRITICAL_PATTERNS` match is on the headline ("Acme
   Receives FDA Approval for Zedox; to Host Conference Call Today") the cap
   is not applied and the dollar points count -- unless the critical match
   is itself housekeeping (a stock split, a tender offer's results) or the
   headline is a price-action note ("shares are trading higher after the
   FDA approval"), which is routine whatever event it mentions. Routine
   rows stay in the feed.
3. **Size the catalyst against the company.** `importance(title, symbols,
   categories, market_cap=None, exchange=None)`: the defaults give exactly
   the old score. With a size: a dollar figure worth half the company or
   more +3, a tenth +1, under 2% of a $10B+ company takes the dollar
   points back; a $20B+ company -2, a $100B+ company -3 (the spec said -2
   for every large company, but then Pfizer's FDA approval at 7 - 2 = 5
   still made the rail, which is the case the rule exists to remove), a
   company under $300M +1; the score never goes below 0; `big = score >= 5`.
   An exchange outside NASDAQ / NYSE / AMEX / NYSE American / NYSE Arca /
   Cboe (short names and FMP's long names: "New York Stock Exchange",
   "NASDAQ Global Select") makes `big` False whatever the score ("not
   US-listed (TSX)"; OTC/PNK: "over the counter: not tradable here"; the
   exchange `NONE` -- see below -- "not found on a US exchange"); an
   unknown exchange changes nothing. `size_words(cap)` -> "$420M company" /
   "$3.2B company" / "$210B company" is on every row and the rail shows it.
   **Where the size comes from:** `companies.py`, table `companies`
   (symbol, name, market_cap, price, exchange, country, fetched_at,
   error). `size_of(symbol)` reads sqlite only and NEVER fetches: a
   missing row is queued, a row older than `FRESH_DAYS` (3) is returned
   and re-queued, an error row past its retry window is re-queued. A
   daemon worker (`start(log)`; no `FMP_API_KEY` -> never starts, and then
   `app` skips the warm-up too, so the queue does not fill with nothing to
   drain it) drains the queue at most `RATE_PER_MIN` (60) FMP CALLS a
   minute (a ticker can take two: `/stable/quote`, then `/stable/profile`
   when the quote has no exchange or nothing came back) through
   `fetch_fn`; tests replace `fetch_fn`. Three kinds of answer are stored
   as `error`: a 402 or an empty answer ("not on this FMP plan", "no data")
   is not asked again for `ERROR_RETRY_HOURS` (24); quote AND profile both
   empty is `NO_LISTING` ("no US listing") with exchange `NONE`, also kept
   a day, and it COUNTS as an answer (`store.size_from(row)` passes the
   exchange through, so Novartis on NOVN and Fairfax on FFH can never be
   big -- the risk: a ticker FMP has not indexed yet, a fresh IPO, is held
   off the rail until the daily retry finds it); a timeout, a 429 or a 5xx
   is "fetch failed: <kind>" (`TRANSIENT`), asked again in
   `TRANSIENT_RETRY_HOURS` (1), and the worker waits `RETRY_PAUSE_S` (60)
   before the next ticker. No error text ever carries the key: a requests
   exception is recorded by class name only (its message holds the whole
   URL, key included) and any other message is scrubbed of `apikey=`.
   When a size (or the no-listing answer) lands,
   `store.rescore_symbol(symbol, 48, score_fn)` re-scores that ticker's
   rows from the last 48 hours with it (tone and impact untouched), so a
   row is right within a minute of its first sighting. `warm(store)` at
   boot queues every ticker from the last `WARM_DAYS` (14). The FMP budget:
   Starter allows 300 calls a minute; floats, gems, the grader and this
   worker share the key, and 60 here leaves room for the rest.
   `/api/status["companies"]` = cached, queued, fetched_today, errors,
   last_error, rate_per_min, calls.
4. **Each release once.** `wires.handle()`: when `note_arrival` matches a
   release another SOURCE delivered inside the 15-minute window with the
   same stock and half the words shared (`COPY_OVERLAP`), the row and the
   SSE payload carry `dup_of` = the first row's id (`store.set_dup`; the
   log says "(also on rtpr, 60s later)"). `copy_of` keeps its meaning: the
   same source's own second delivery. `store.recent(min_importance=...)`
   -- the rail's query -- adds `AND dup_of IS NULL`; the plain feed shows
   every row. The page (`paintBig`) also skips a live push with `dup_of`
   and a live push whose key (first ticker + the first eight words of the
   headline, digits kept so "Phase 2" and "Phase 3" differ) is already on
   the rail; the first paint trusts the server, and a headline under four
   words is never keyed.

**What Halthawk still receives: everything.** Every row still goes out on
the bus and over `/api/feed`; its reader (`tapehawk_news._norm`) keeps the
keys it knows and ignores `dup_of`, `market_cap`, `exchange`, `size_words`.
It does its own first-copy dedupe and its own kind filter.

**The wire thread never waits on FMP.** `Source.article()` asks
`companies.size_of(first ticker)` (one sqlite read) and queues the other
tickers; `test_classify` asserts `fetch_fn` is never called on the handle
path.

**After a deploy** `app._rescore_once` runs `store.rescore_recent(30, ...)`
in a thread: the noise check and the scorer (with cached sizes) over the
last 30 days in batches of 200 with a pause, and logs how many rows changed
importance, how many turned noise, and how many rows it could not re-score
(`skipped`, with the last error) -- a row the scorer or the database choked
on is counted, never silently dropped.

**To add a routine or law-firm pattern:** one line in `ROUTINE_PATTERNS`
(regex, words), `LAW_FIRM_PATTERNS` (a shape only a pitch has) or
`LAW_FIRM_GENERIC` (a shape a company can share: it needs a signal), and
one line in `tests/test_classify.py` with a live headline that must be
routine or noise -- plus, when the pattern could catch a real event, one
headline that must NOT be. The test pins each sure shape and each firm
name on its own, so a deleted pattern shows up.

Columns added (additive): `headlines.dup_of INTEGER`, `market_cap REAL`,
`exchange TEXT`; table `companies`. `_row` exposes `dup_of`, `market_cap`,
`exchange`, `size_words`; `big` is `importance >= 5` AND no exchange
verdict on the stored exchange (a TSX, OTC or NONE row scoring 7 is not
big and `recent(min_importance=5)` -- the rail -- leaves it out; a row
with no exchange stored passes as before). Found 2026-10-07 while fixing
the review: the scorer's verdict never reached the stored row.

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
