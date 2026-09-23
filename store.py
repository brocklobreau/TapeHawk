"""
Headline storage. SQLite, because the whole point of this service is that a
headline arrives once, at speed, and must still be there when someone opens
the page an hour later.

Deliberately not in-memory: Render restarts services on deploy and on its own
schedule, and a news site that forgets everything on restart is a news site
with no archive, no search and no way to show you what you missed overnight.
The file lives on the mounted disk.

Write path is single-threaded (one websocket consumer), read path is the web
workers. SQLite handles that fine with WAL mode, which is set below --
without it a read during a write raises "database is locked" and the page
500s at exactly the moment news is breaking, which is the worst possible
time for it.
"""
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get("TAPEHAWK_DB", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "headlines.db"))

_local = threading.local()

# Table and indexes are separate on purpose, and the order matters. The index
# on `importance` cannot be created until that column exists, and
# CREATE TABLE IF NOT EXISTS does nothing to a database that predates it -- so
# running them together meant "no such column: importance" on startup against
# any existing database, i.e. the feature would have taken the live service
# down on deploy. Table first, then migrate columns, then indexes.
TABLE_SQL = """
CREATE TABLE IF NOT EXISTS headlines (
  id           INTEGER PRIMARY KEY,
  alpaca_id    INTEGER UNIQUE,
  created_at   TEXT NOT NULL,
  received_at  TEXT NOT NULL,
  latency_ms   INTEGER,
  headline     TEXT NOT NULL,
  summary      TEXT,
  author       TEXT,
  source       TEXT,
  url          TEXT,
  symbols      TEXT,
  categories   TEXT,
  is_noise     INTEGER DEFAULT 0,
  importance   INTEGER DEFAULT 0,
  reasons      TEXT,
  content      TEXT,
  tone         TEXT,
  tone_reasons TEXT,
  impact_level TEXT,
  impact_note  TEXT,
  graded_at    TEXT,
  grade_symbol TEXT,
  grade_entry  REAL,
  move_15m     REAL,
  move_60m     REAL,
  grade_note   TEXT
);
"""

# Filings live in their own table rather than as rows in `headlines`. They are
# a different kind of object -- no wire latency, no category keywords, a
# percentage and a reporting person instead -- and squeezing them into the
# headline schema would mean half the columns null on every row and a feed
# query that has to know which sort it is looking at.
FILINGS_SQL = """
CREATE TABLE IF NOT EXISTS filings (
  id               INTEGER PRIMARY KEY,
  accession        TEXT UNIQUE,
  kind             TEXT NOT NULL DEFAULT '13d',
  items            TEXT,
  labels           TEXT,
  direction        TEXT,
  uncovered        INTEGER,
  coverage_at      TEXT,
  time_tried       INTEGER,
  form             TEXT NOT NULL,
  issuer_cik       INTEGER,
  issuer_name      TEXT,
  ticker           TEXT,
  reporting_person TEXT,
  percent          REAL,
  shares           REAL,
  cusip            TEXT,
  filed_at         TEXT,
  seen_at          TEXT NOT NULL,
  latency_ms       INTEGER,
  url              TEXT,
  source           TEXT
);

CREATE TABLE IF NOT EXISTS halts (
  id            INTEGER PRIMARY KEY,
  halt_key      TEXT UNIQUE,
  symbol        TEXT NOT NULL,
  name          TEXT,
  market        TEXT,
  code          TEXT,
  halted_at     TEXT,
  halt_date     TEXT,
  quote_at      TEXT,
  resumed_at    TEXT,
  band_price    REAL,
  seen_at       TEXT NOT NULL,
  graded_at     TEXT,
  pre_price     REAL,
  reopen_price  REAL,
  gap_pct       REAL,
  move_15m      REAL,
  move_60m      REAL,
  run_in_pct    REAL,
  direction     TEXT,
  seq           INTEGER,
  into_price    REAL,
  news_id         INTEGER,
  news_headline   TEXT,
  news_importance INTEGER,
  news_at         TEXT,
  news_url        TEXT,
  news_checked_at TEXT,
  move_5m         REAL,
  grade_version   INTEGER
);

-- Accession numbers of 8-Ks already judged routine. Reading an 8-K's header
-- costs a request, and without this ledger every poll would re-read the
-- header of every routine 8-K still in the feed window, forever. Storing the
-- verdict rather than the filing keeps this table tiny.
CREATE TABLE IF NOT EXISTS filings_seen (
  accession TEXT PRIMARY KEY,
  at        TEXT NOT NULL
);
"""

# The investing screen. One row per company from the last pass; the facts
# and the notes are JSON so a rule change does not need a migration.
GEMS_SQL = """
CREATE TABLE IF NOT EXISTS gems (
  symbol       TEXT PRIMARY KEY,
  name         TEXT, sector TEXT, industry TEXT, exchange TEXT,
  market_cap   REAL, price REAL,
  score        REAL, value_pts INTEGER, growth_pts INTEGER, quality_pts INTEGER, setup_pts INTEGER,
  timing_pts   INTEGER, catalyst_pts INTEGER, fair_price REAL, upside REAL, rule_version INTEGER,
  facts        TEXT, notes TEXT, disqualified TEXT, verdict TEXT,
  scored_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_gems_score ON gems(score DESC);
CREATE TABLE IF NOT EXISTS gem_picks (
  id           INTEGER PRIMARY KEY,
  symbol       TEXT NOT NULL,
  picked_on    TEXT NOT NULL,
  rank         INTEGER, score REAL, catalyst_pts INTEGER,
  price        REAL, spy REAL,
  ret_1m REAL, spy_1m REAL, graded_1m TEXT,
  ret_3m REAL, spy_3m REAL, graded_3m TEXT,
  ret_6m REAL, spy_6m REAL, graded_6m TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_gem_picks ON gem_picks(symbol, picked_on);
"""

INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_created ON headlines(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_noise_created ON headlines(is_noise, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_importance ON headlines(importance DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_ungraded ON headlines(graded_at, created_at);
CREATE INDEX IF NOT EXISTS idx_filings_seen ON filings(seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_filings_ticker ON filings(ticker, seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_filings_kind ON filings(kind, seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_filings_coverage ON filings(coverage_at, seen_at);
CREATE INDEX IF NOT EXISTS idx_halts_time ON halts(halted_at DESC);
CREATE INDEX IF NOT EXISTS idx_halts_symbol ON halts(symbol, halted_at DESC);
CREATE INDEX IF NOT EXISTS idx_halts_grade ON halts(graded_at, resumed_at);
"""


def _conn():
    c = getattr(_local, "conn", None)
    if c is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
        c.row_factory = sqlite3.Row
        # WAL lets readers and the writer work at the same time. The default
        # rollback journal blocks readers during a write, which on a news feed
        # means the page locks up precisely when headlines are arriving.
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.executescript(TABLE_SQL)
        c.executescript(FILINGS_SQL)
        # Additive migration. CREATE TABLE IF NOT EXISTS does nothing to a
        # table that already exists, so a database created before these
        # columns existed would never gain them and every insert would fail.
        # Checking and adding is the difference between shipping a feature and
        # taking the service down on deploy.
        have = {r["name"] for r in c.execute("PRAGMA table_info(headlines)")}
        for col, ddl in (("importance", "INTEGER DEFAULT 0"), ("reasons", "TEXT"),
                         ("content", "TEXT"), ("tone", "TEXT"),
                         ("tone_reasons", "TEXT"), ("impact_level", "TEXT"),
                         ("impact_note", "TEXT"), ("graded_at", "TEXT"),
                         ("grade_symbol", "TEXT"), ("grade_entry", "REAL"),
                         ("move_15m", "REAL"), ("move_60m", "REAL"),
                         ("grade_note", "TEXT")):
            if col not in have:
                c.execute(f"ALTER TABLE headlines ADD COLUMN {col} {ddl}")
        # Same treatment for filings. A database created by the 13D-only
        # version already has this table, so CREATE TABLE IF NOT EXISTS leaves
        # it alone -- and idx_filings_kind below would then fail with "no such
        # column: kind" and take the service down on deploy. This is the
        # identical trap that the importance column set once already.
        fhave = {r["name"] for r in c.execute("PRAGMA table_info(filings)")}
        for col, ddl in (("kind", "TEXT NOT NULL DEFAULT '13d'"), ("items", "TEXT"),
                         ("labels", "TEXT"), ("direction", "TEXT"),
                         ("uncovered", "INTEGER"), ("coverage_at", "TEXT"),
                         ("time_tried", "INTEGER")):
            if col not in fhave:
                c.execute(f"ALTER TABLE filings ADD COLUMN {col} {ddl}")
        hhave = {r["name"] for r in c.execute("PRAGMA table_info(halts)")}
        for col, ddl in (("run_in_pct", "REAL"), ("direction", "TEXT"),
                         ("seq", "INTEGER"), ("into_price", "REAL"),
                         ("news_id", "INTEGER"), ("news_headline", "TEXT"),
                         ("news_importance", "INTEGER"), ("news_at", "TEXT"),
                         ("news_url", "TEXT"), ("news_checked_at", "TEXT"),
                         ("move_5m", "REAL"), ("grade_version", "INTEGER")):
            if col not in hhave:
                c.execute(f"ALTER TABLE halts ADD COLUMN {col} {ddl}")
        c.executescript(INDEX_SQL)          # only now are all columns present
        c.executescript(GEMS_SQL)
        ghave = {r["name"] for r in c.execute("PRAGMA table_info(gems)")}
        for col, ddl in (("timing_pts", "INTEGER"), ("catalyst_pts", "INTEGER"),
                         ("fair_price", "REAL"), ("upside", "REAL"), ("rule_version", "INTEGER")):
            if col not in ghave:
                c.execute(f"ALTER TABLE gems ADD COLUMN {col} {ddl}")
        c.commit()
        _local.conn = c
    return c


def init():
    _conn()


def insert(article):
    """Returns True if stored, False if it was a duplicate.

    Alpaca re-sends an article when it is corrected or updated, so the same
    alpaca_id can arrive more than once. INSERT OR IGNORE against the unique
    index keeps the FIRST arrival, which is the one whose latency measurement
    is honest -- an update re-delivered an hour later would otherwise look
    like a very slow headline and poison the latency stats."""
    c = _conn()
    cur = c.execute(
        """INSERT OR IGNORE INTO headlines
           (alpaca_id, created_at, received_at, latency_ms, headline, summary,
            author, source, url, symbols, categories, is_noise,
            importance, reasons, content, tone, tone_reasons,
            impact_level, impact_note)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (article.get("alpaca_id"), article["created_at"], article["received_at"],
         article.get("latency_ms"), article["headline"], article.get("summary"),
         article.get("author"), article.get("source"), article.get("url"),
         json.dumps(article.get("symbols") or []),
         json.dumps(article.get("categories") or []),
         1 if article.get("is_noise") else 0,
         int(article.get("importance") or 0),
         json.dumps(article.get("reasons") or []),
         article.get("content"),
         article.get("tone"), json.dumps(article.get("tone_reasons") or []),
         article.get("impact_level"), article.get("impact_note")))
    c.commit()
    return cur.rowcount > 0


def _row(r):
    d = dict(r)
    d["symbols"] = json.loads(d.get("symbols") or "[]")
    d["categories"] = json.loads(d.get("categories") or "[]")
    d["reasons"] = json.loads(d.get("reasons") or "[]")
    d["has_content"] = bool(d.get("content"))
    d["tone_reasons"] = json.loads(d.get("tone_reasons") or "[]")
    d["big"] = (d.get("importance") or 0) >= 5
    d["is_noise"] = bool(d.get("is_noise"))
    return d


def get(article_id):
    """One article WITH its body. Kept out of recent() on purpose: bodies run
    to a couple of thousand characters, and eighty of them would turn a feed
    request that should be ~40KB into something close to a megabyte, on the
    one endpoint that has to feel instant."""
    r = _conn().execute("SELECT * FROM headlines WHERE id = ?", (article_id,)).fetchone()
    return _row(r) if r else None


def recent(limit=100, since_id=None, symbol=None, category=None,
           include_noise=False, search=None, min_importance=None):
    sql = ("SELECT id, alpaca_id, created_at, received_at, latency_ms, headline, "
           "summary, author, source, url, symbols, categories, is_noise, "
           "importance, reasons, tone, tone_reasons, impact_level, impact_note, "
           "graded_at, grade_symbol, grade_entry, move_15m, move_60m, grade_note "
           "FROM headlines WHERE 1=1")
    args = []
    if not include_noise:
        sql += " AND is_noise = 0"
    if since_id:
        sql += " AND id > ?"
        args.append(since_id)
    if symbol:
        # symbols is a JSON array; match the quoted token so "A" cannot match
        # "AAPL". Crude but correct, and avoids a join table for v1.
        sql += " AND symbols LIKE ?"
        args.append(f'%"{symbol.upper()}"%')
    if category:
        sql += " AND categories LIKE ?"
        args.append(f'%"{category}"%')
    if search:
        sql += " AND (headline LIKE ? OR summary LIKE ?)"
        args.extend([f"%{search}%", f"%{search}%"])
    if min_importance:
        sql += " AND importance >= ?"
        args.append(int(min_importance))
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(min(int(limit), 300))
    return [_row(r) for r in _conn().execute(sql, args)]


def stats():
    c = _conn()
    total = c.execute("SELECT COUNT(*) n FROM headlines").fetchone()["n"]
    noise = c.execute("SELECT COUNT(*) n FROM headlines WHERE is_noise=1").fetchone()["n"]
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    last_hr = c.execute("SELECT COUNT(*) n FROM headlines WHERE received_at > ?",
                        (since,)).fetchone()["n"]
    lat = c.execute("SELECT AVG(latency_ms) a, MIN(latency_ms) m FROM headlines "
                    "WHERE latency_ms IS NOT NULL AND latency_ms BETWEEN 0 AND 600000"
                    ).fetchone()
    newest = c.execute("SELECT MAX(id) i FROM headlines").fetchone()["i"]
    return {"total": total, "filtered": noise, "last_hour": last_hr,
            "avg_latency_ms": round(lat["a"]) if lat["a"] else None,
            "best_latency_ms": lat["m"], "newest_id": newest}


def backfill_importance(score_fn, tone_fn=None, impact_fn=None, log=print):
    """Score rows that were stored before importance existed.

    Without this the Big News rail stays empty for as long as the archive
    takes to turn over -- including a genuine circuit-breaker halt that had
    already come through the wire. Rows never scored are identified by a NULL
    `reasons`, because scoring always writes that column even when the list
    is empty; a row that legitimately scored zero has "[]" there and is left
    alone rather than being scored twice.
    """
    c = _conn()
    rows = c.execute("SELECT id, headline, symbols, categories FROM headlines "
                     "WHERE reasons IS NULL OR tone IS NULL").fetchall()
    if not rows:
        return 0
    done = 0
    for r in rows:
        try:
            syms = json.loads(r["symbols"] or "[]")
            cats = json.loads(r["categories"] or "[]")
            imp = score_fn(r["headline"], syms, cats)
            tn = tone_fn(r["headline"]) if tone_fn else {"direction": None, "reasons": []}
            ip = impact_fn(r["headline"], syms) if impact_fn else {"level": None, "note": None}
            c.execute("UPDATE headlines SET importance = ?, reasons = ?, tone = ?, "
                      "tone_reasons = ?, impact_level = ?, impact_note = ? WHERE id = ?",
                      (int(imp["score"]), json.dumps(imp["reasons"]),
                       tn.get("direction"), json.dumps(tn.get("reasons") or []),
                       ip.get("level"), ip.get("note"), r["id"]))
            done += 1
        except Exception:
            # One unscoreable row must not abort the whole backfill.
            continue
    c.commit()
    log(f"store: scored {done} headline(s) stored before importance existed")
    return done


def ungraded(cutoff_iso, limit=25):
    """Headlines old enough to have an answer and not yet graded. Oldest
    first, so a backlog drains in the order it arrived rather than the newest
    items starving the rest."""
    rows = _conn().execute(
        "SELECT id, headline, symbols, created_at FROM headlines "
        "WHERE graded_at IS NULL AND is_noise = 0 AND created_at < ? "
        "ORDER BY created_at ASC LIMIT ?", (cutoff_iso, int(limit)))
    return [_row(r) for r in rows]


def mark_graded(article_id, symbol, move_15m, move_60m, entry, reason=None):
    """Always writes graded_at, including for skips. A headline that cannot
    be graded -- crypto pair, arrived at 2am -- must still be marked, or the
    grader retries it forever and the backlog never drains."""
    c = _conn()
    c.execute("UPDATE headlines SET graded_at = ?, grade_symbol = ?, "
              "grade_entry = ?, move_15m = ?, move_60m = ?, grade_note = ? "
              "WHERE id = ?",
              (datetime.now(timezone.utc).isoformat(), symbol, entry,
               move_15m, move_60m, reason, article_id))
    c.commit()


# A move smaller than this is not a reaction, it is the spread wobbling.
# Direction calls are not scored against it: handing the site credit because a
# stock drifted +0.03% after a headline would make the hit rate a measurement
# of rounding noise. These rows are reported on their own line instead.
FLAT_BAND_PCT = 0.25


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def scoreboard(days=30):
    """How well the calls held up, straight from the graded rows.

    Reports counts alongside every number. A 100% hit rate on three samples
    is not a hit rate, and a scoreboard that hides its n is marketing rather
    than measurement -- which would defeat the entire point of publishing it."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    c = _conn()

    def rows(where, args=()):
        return c.execute(
            "SELECT impact_level, tone, categories, importance, move_15m, move_60m, "
            "headline, grade_symbol, created_at, url FROM headlines "
            "WHERE graded_at IS NOT NULL AND move_60m IS NOT NULL "
            "AND created_at > ? " + where + " ORDER BY created_at DESC",
            (since,) + tuple(args)).fetchall()

    all_rows = rows("")
    def summarise(rs):
        if not rs:
            return None
        moves = [abs(r["move_60m"]) for r in rs]
        signed = [r["move_60m"] for r in rs]

        # "Went up" cannot be the measure of a call, and using it as one was a
        # real error on this page. A negative headline followed by a fall is a
        # CORRECT call that scores zero on "went up", so any pool holding both
        # tones averages two numbers that pull in opposite directions: a site
        # that called every headline perfectly would still land near 50% if
        # its headlines split evenly between good news and bad. What is scored
        # here instead is AGREEMENT -- did the stock move the way the tone
        # said it would. Now 50% means a coin flip everywhere on the page and
        # 100% means perfect, which is what a reader assumes a hit rate means.
        #
        # Headlines with no directional call are not scored at all rather than
        # counted as failures, and neither are the ones that barely moved.
        right = wrong = flat = 0
        for r in rs:
            tone, m = r["tone"], r["move_60m"]
            if tone not in ("positive", "negative"):
                continue
            if abs(m) < FLAT_BAND_PCT:
                flat += 1
            elif (m > 0) == (tone == "positive"):
                right += 1
            else:
                wrong += 1
        scored = right + wrong
        return {
            "n": len(rs),
            "median_abs_move": round(_median(moves), 2),
            "mean_abs_move": round(sum(moves) / len(moves), 2),
            "moved_over_2pct": round(100.0 * sum(1 for m in moves if m >= 2) / len(moves), 1),
            "mean_signed": round(sum(signed) / len(signed), 3),
            # The blunt version of the same test, and the one that is hardest
            # to fool: bad news should carry a clearly NEGATIVE median. A tone
            # label that only sorts language will leave both near zero.
            "median_signed": round(_median(signed), 2),
            "pct_up": round(100.0 * sum(1 for m in signed if m > 0) / len(signed), 1),
            "pct_correct": round(100.0 * right / scored, 1) if scored else None,
            "scored": scored,
            "no_reaction": flat,
            "no_call": len(rs) - scored - flat,
        }

    by_impact = {}
    for lvl in ("high", "medium", "low"):
        by_impact[lvl] = summarise([r for r in all_rows if r["impact_level"] == lvl])
    by_impact["unrated"] = summarise([r for r in all_rows if not r["impact_level"]])

    def split_by_tone(rs):
        return {t: summarise([r for r in rs if r["tone"] == t])
                for t in ("positive", "negative", "unclear")}

    by_tone = split_by_tone(all_rows)

    by_cat = {}
    for r in all_rows:
        for cat in json.loads(r["categories"] or "[]"):
            by_cat.setdefault(cat, []).append(r)
    by_cat = {k: summarise(v) for k, v in sorted(by_cat.items(),
                                                 key=lambda kv: -len(kv[1]))[:8]}

    big = [r for r in all_rows if (r["importance"] or 0) >= 5]
    def as_row(r):
        return {"headline": r["headline"], "symbol": r["grade_symbol"],
                "created_at": r["created_at"], "url": r["url"],
                "impact_level": r["impact_level"], "tone": r["tone"],
                "move_15m": r["move_15m"], "move_60m": r["move_60m"],
                "importance": r["importance"]}

    recent = [as_row(r) for r in all_rows[:40]]
    # Big News gets its own list. Mixed into forty routine headlines, the
    # handful of calls that actually matter are impossible to pick out --
    # which is the whole thing a reader comes to this page to check.
    recent_big = [as_row(r) for r in big[:25]]

    pending = c.execute("SELECT COUNT(*) n FROM headlines "
                        "WHERE graded_at IS NULL AND is_noise = 0").fetchone()["n"]
    skipped = c.execute("SELECT COUNT(*) n FROM headlines "
                        "WHERE graded_at IS NOT NULL AND move_60m IS NULL").fetchone()["n"]
    return {"days": days, "flat_band": FLAT_BAND_PCT,
            "overall": summarise(all_rows), "big_news": summarise(big),
            # The same split, but for Big News on its own. A single "median
            # move" over the whole rail pools good news, bad news and the
            # unreadable together, so the number a reader most wants -- what
            # did the stock do when the rail called it POSITIVE -- was not on
            # the page anywhere.
            "big_by_tone": split_by_tone(big),
            "overall_by_tone": by_tone,
            "recent_big": recent_big,
            "by_impact": by_impact, "by_tone": by_tone, "by_category": by_cat,
            "recent": recent, "pending": pending, "ungradeable": skipped}


# --- 13D filings ------------------------------------------------------------

def filing_exists(accession):
    if not accession:
        return False
    return _conn().execute("SELECT 1 FROM filings WHERE accession = ?",
                           (accession,)).fetchone() is not None


def insert_filing(f):
    """True if stored, False if already seen.

    EDGAR re-lists a filing across consecutive polls, and an amendment carries
    its own accession number, so INSERT OR IGNORE on the accession is what
    keeps the tab from filling with the same stake over and over.
    """
    c = _conn()
    cur = c.execute(
        """INSERT OR IGNORE INTO filings
           (accession, kind, items, labels, direction, form, issuer_cik,
            issuer_name, ticker, reporting_person, percent, shares, cusip,
            filed_at, seen_at, latency_ms, url, source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f.get("accession"), f.get("kind", "13d"),
         json.dumps(f.get("items") or []),
         json.dumps(f.get("labels") or []), f.get("direction"),
         f.get("form", "SC 13D"), f.get("issuer_cik"),
         f.get("issuer_name"), f.get("ticker"), f.get("reporting_person"),
         f.get("percent"), f.get("shares"), f.get("cusip"), f.get("filed_at"),
         f.get("seen_at") or datetime.now(timezone.utc).isoformat(),
         f.get("latency_ms"), f.get("url"), f.get("source")))
    c.commit()
    return cur.rowcount > 0


def filings_missing_time(limit=12):
    """Rows whose filed_at is a bare date, newest first, skipping any already
    tried and found to have no readable stamp."""
    return [dict(r) for r in _conn().execute(
        "SELECT accession, issuer_cik, seen_at FROM filings "
        "WHERE filed_at IS NOT NULL AND filed_at NOT LIKE '%T%' "
        "AND (time_tried IS NULL OR time_tried = 0) "
        "ORDER BY filed_at DESC, id DESC LIMIT ?", (limit,)).fetchall()]


def set_filed_at(accession, filed_at, latency_ms, give_up=False):
    c = _conn()
    if give_up:
        c.execute("UPDATE filings SET time_tried = 1 WHERE accession = ?", (accession,))
    else:
        c.execute("UPDATE filings SET filed_at = ?, latency_ms = ?, time_tried = 1 "
                  "WHERE accession = ?", (filed_at, latency_ms, accession))
    c.commit()


def dismiss_filing(accession):
    """Record that a filing was looked at and judged not worth listing."""
    c = _conn()
    c.execute("INSERT OR IGNORE INTO filings_seen (accession, at) VALUES (?,?)",
              (accession, datetime.now(timezone.utc).isoformat()))
    c.commit()


def filing_dismissed(accession):
    if not accession:
        return False
    return _conn().execute("SELECT 1 FROM filings_seen WHERE accession = ?",
                           (accession,)).fetchone() is not None


def _filing_row(r):
    d = dict(r)
    d["items"] = json.loads(d.get("items") or "[]")
    d["labels"] = json.loads(d.get("labels") or "[]")
    return d


def recent_filings(limit=100, ticker=None, amendments=True, kind=None,
                   direction=None):
    sql = "SELECT * FROM filings WHERE 1=1"
    args = []
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    if direction:
        sql += " AND direction = ?"
        args.append(direction)
    if ticker:
        sql += " AND ticker = ?"
        args.append(ticker.upper())
    if not amendments:
        # An initial 13D is a new position. An amendment can be a holder
        # trimming, which is the opposite news, so they are separable.
        sql += " AND form NOT LIKE '%/A'"
    sql += " ORDER BY COALESCE(filed_at, seen_at) DESC, id DESC LIMIT ?"
    args.append(min(int(limit), 300))
    return [_filing_row(r) for r in _conn().execute(sql, args)]


def filings_needing_coverage(cutoff_iso, limit=50):
    """Stored filings old enough to judge and not yet checked against the
    headline archive. Oldest first so a backlog drains in order."""
    rows = _conn().execute(
        "SELECT accession, ticker, filed_at, seen_at FROM filings "
        "WHERE coverage_at IS NULL AND kind = '8k' "
        "AND COALESCE(filed_at, seen_at) < ? "
        "ORDER BY COALESCE(filed_at, seen_at) ASC LIMIT ?",
        (cutoff_iso, int(limit)))
    return [dict(r) for r in rows]


def set_coverage(accession, uncovered):
    """Always stamps coverage_at, including when the verdict is unknown --
    otherwise an unjudgeable filing is retried on every pass forever."""
    c = _conn()
    c.execute("UPDATE filings SET uncovered = ?, coverage_at = ? WHERE accession = ?",
              (uncovered, datetime.now(timezone.utc).isoformat(), accession))
    c.commit()


def headlines_mentioning(symbol, start_iso, end_iso):
    """How many headlines tagged this ticker in a window. Uses created_at --
    when the news happened -- not received_at, so a restart that backfills the
    archive later does not make an uncovered filing look covered."""
    if not symbol:
        return 0
    return _conn().execute(
        "SELECT COUNT(*) n FROM headlines WHERE symbols LIKE ? "
        "AND created_at >= ? AND created_at <= ?",
        (f'%"{symbol.upper()}"%', start_iso, end_iso)).fetchone()["n"]


def best_headline_for(symbol, start_iso, end_iso):
    """The strongest headline tagged with this ticker in a window, or None.

    Strongest = highest importance, then most recent. Uses created_at -- when
    the news happened -- so a restart that backfills the archive later cannot
    make a silent halt look news-backed after the fact."""
    if not symbol:
        return None
    row = _conn().execute(
        "SELECT id, headline, importance, created_at, url FROM headlines "
        "WHERE symbols LIKE ? AND created_at >= ? AND created_at <= ? "
        "ORDER BY COALESCE(importance, 0) DESC, created_at DESC LIMIT 1",
        (f'%"{symbol.upper()}"%', start_iso, end_iso)).fetchone()
    return dict(row) if row else None


def halts_needing_news(since_iso, limit=60):
    """Halts to (re)check for a headline: recent ones without a Big News
    match yet. Re-checked on every poll while young, because the wire often
    writes the halt up a few minutes after the exchange declares it."""
    rows = _conn().execute(
        "SELECT halt_key, symbol, halted_at FROM halts "
        "WHERE halted_at IS NOT NULL AND halted_at >= ? "
        "AND (news_importance IS NULL OR news_importance < 5) "
        "ORDER BY halted_at DESC LIMIT ?", (since_iso, int(limit)))
    return [dict(r) for r in rows]


def set_halt_news(halt_key, hit):
    c = _conn()
    now = datetime.now(timezone.utc).isoformat()
    if hit:
        c.execute("UPDATE halts SET news_id = ?, news_headline = ?, news_importance = ?, "
                  "news_at = ?, news_url = ?, news_checked_at = ? WHERE halt_key = ?",
                  (hit.get("id"), hit.get("headline"), hit.get("importance"),
                   hit.get("created_at"), hit.get("url"), now, halt_key))
    else:
        c.execute("UPDATE halts SET news_checked_at = ? WHERE halt_key = ?", (now, halt_key))
    c.commit()


def filing_stats(days=30):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    c = _conn()

    def n(sql, args=()):
        return c.execute("SELECT COUNT(*) n FROM filings WHERE " + sql, args).fetchone()["n"]

    total_13d = n("kind = '13d'")
    fresh = n("kind = '13d' AND form NOT LIKE '%/A'")
    total_8k = n("kind = '8k'")
    lat = c.execute("SELECT AVG(latency_ms) a, MIN(latency_ms) m FROM filings "
                    "WHERE latency_ms IS NOT NULL").fetchone()
    top = [dict(r) for r in c.execute(
        "SELECT reporting_person p, COUNT(*) n FROM filings "
        "WHERE kind = '13d' AND reporting_person IS NOT NULL "
        "AND reporting_person != '' GROUP BY p ORDER BY n DESC, p ASC LIMIT 8")]
    # Counted against filings actually JUDGED, not against every 8-K stored.
    # A percentage whose denominator quietly includes rows nobody has checked
    # yet is the kind of number that reads as measurement and is not.
    judged = n("kind = '8k' AND uncovered IS NOT NULL")
    uncovered = n("kind = '8k' AND uncovered = 1")
    by_item, by_dir = {}, {}
    for r in c.execute("SELECT items, direction FROM filings WHERE kind = '8k'"):
        for it in json.loads(r["items"] or "[]"):
            by_item[it] = by_item.get(it, 0) + 1
        d = r["direction"] or "unclear"
        by_dir[d] = by_dir.get(d, 0) + 1
    return {"days": days,
            "total": total_13d, "window": n("kind = '13d' AND seen_at > ?", (since,)),
            "initial": fresh, "amendments": total_13d - fresh,
            "avg_latency_ms": round(lat["a"]) if lat["a"] else None,
            "best_latency_ms": lat["m"], "top_filers": top,
            "eightk_total": total_8k,
            "eightk_window": n("kind = '8k' AND seen_at > ?", (since,)),
            "eightk_judged": judged, "eightk_uncovered": uncovered,
            "eightk_by_item": dict(sorted(by_item.items(), key=lambda kv: -kv[1])),
            "eightk_by_direction": by_dir}


# --- halts ------------------------------------------------------------------

def upsert_halt(h):
    """'new', 'resumed', or None.

    A halt appears in the feed the moment it starts, with the resumption
    fields empty, and the SAME row is republished later once a resumption is
    scheduled. So this cannot be insert-or-ignore: the second sighting is
    where the resumption time arrives, and dropping it would leave every halt
    permanently ungradeable.
    """
    c = _conn()
    now = datetime.now(timezone.utc).isoformat()
    row = c.execute("SELECT id, resumed_at FROM halts WHERE halt_key = ?",
                    (h.get("halt_key"),)).fetchone()
    if row is None:
        c.execute(
            """INSERT OR IGNORE INTO halts
               (halt_key, symbol, name, market, code, halted_at, halt_date,
                quote_at, resumed_at, band_price, seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (h.get("halt_key"), h.get("symbol"), h.get("name"), h.get("market"),
             h.get("code"), h.get("halted_at"), h.get("halt_date"),
             h.get("quote_at"), h.get("resumed_at"), h.get("band_price"), now))
        _resequence(c, h.get("symbol"), h.get("halt_date"))
        c.commit()
        return "new"
    if h.get("resumed_at") and not row["resumed_at"]:
        c.execute("UPDATE halts SET resumed_at = ?, quote_at = COALESCE(?, quote_at) "
                  "WHERE id = ?", (h["resumed_at"], h.get("quote_at"), row["id"]))
        c.commit()
        return "resumed"
    return None


def _resequence(c, symbol, halt_date):
    """Number this symbol's halts for the day: 1st, 2nd, 3rd...

    Recomputed for the whole group on every insert rather than counted once,
    because the feed is polled and a halt can turn up after a later one was
    already stored. A runner's fourth halt trades nothing like its first, and
    that number is the only way the base rates can tell them apart.
    """
    if not symbol or not halt_date:
        return
    ids = [r["id"] for r in c.execute(
        "SELECT id FROM halts WHERE symbol = ? AND halt_date = ? "
        "ORDER BY COALESCE(halted_at, seen_at) ASC, id ASC", (symbol, halt_date))]
    for n, rid in enumerate(ids, 1):
        c.execute("UPDATE halts SET seq = ? WHERE id = ? AND (seq IS NULL OR seq != ?)",
                  (n, rid, n))


def resequence_all(log=print):
    """One pass over rows stored before seq existed. Cheap: it is one query
    per symbol-day, and there are not many of those."""
    c = _conn()
    groups = c.execute("SELECT DISTINCT symbol, halt_date FROM halts "
                       "WHERE seq IS NULL AND symbol IS NOT NULL "
                       "AND halt_date IS NOT NULL").fetchall()
    for g in groups:
        _resequence(c, g["symbol"], g["halt_date"])
    c.commit()
    if groups:
        log(f"halts: numbered halts-of-the-day for {len(groups)} symbol-day(s)")


BIG_NEWS_MIN = 5          # the Big News rail's own threshold


def recent_halts(limit=120, symbol=None, code=None, open_only=False,
                 direction=None, news_only=False):
    sql = "SELECT * FROM halts WHERE 1=1"
    args = []
    if news_only:
        sql += " AND news_importance >= ?"
        args.append(BIG_NEWS_MIN)
    if symbol:
        sql += " AND symbol = ?"
        args.append(symbol.upper())
    if code:
        sql += " AND code = ?"
        args.append(code.upper())
    if direction in ("up", "down"):
        sql += " AND direction = ?"
        args.append(direction)
    if open_only:
        sql += " AND resumed_at IS NULL"
    sql += " ORDER BY COALESCE(halted_at, seen_at) DESC, id DESC LIMIT ?"
    args.append(min(int(limit), 400))
    return [dict(r) for r in _conn().execute(sql, args)]


# A "spike" for the gems page: the stock moved at least this much, up, within
# the hour after the headline. Five percent is the line between a reaction and
# a wobble on a small cap; it is stated in the payload so the page shows it.
SPIKE_PCT = 5.0
SPIKE_MIN_SAMPLE = 8


def spike_history(category=None, days=90, tone="positive"):
    """What stories like this one actually did, from this site's own graded
    archive. Same category, same tone, Big News grade, last `days` days.

    Returns n, how many spiked (best of +15m/+60m at or above SPIKE_PCT),
    the rate, the median 60-minute move, and the share that ran 10%+. When the
    category has fewer than SPIKE_MIN_SAMPLE graded rows the answer falls back
    to every big positive story and says so, because an odds figure on four
    samples is a coin toss wearing a percentage sign."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    c = _conn()

    def rows(cat):
        sql = ("SELECT move_15m, move_60m FROM headlines WHERE graded_at IS NOT NULL "
               "AND move_60m IS NOT NULL AND created_at > ? AND importance >= ? AND tone = ?")
        args = [since, BIG_NEWS_MIN, tone]
        if cat:
            sql += " AND categories LIKE ?"
            args.append(f'%"{cat}"%')
        return c.execute(sql, args).fetchall()

    def summarise(rs, scope):
        peaks = [max(r["move_15m"] if r["move_15m"] is not None else r["move_60m"],
                     r["move_60m"]) for r in rs]
        n = len(peaks)
        spiked = sum(1 for p in peaks if p >= SPIKE_PCT)
        ran = sum(1 for p in peaks if p >= 10.0)
        return {"scope": scope, "n": n, "spiked": spiked,
                "spike_rate": round(spiked / n * 100) if n else None,
                "big_rate": round(ran / n * 100) if n else None,
                "median_60m": _median([r["move_60m"] for r in rs]) if n else None,
                "days": days, "spike_pct": SPIKE_PCT, "enough": n >= SPIKE_MIN_SAMPLE}

    if category:
        rs = rows(category)
        if len(rs) >= SPIKE_MIN_SAMPLE:
            return summarise(rs, category)
    out = summarise(rows(None), "all")
    out["wanted"] = category
    return out


def halt_count_today(symbol):
    """How many times this symbol has halted on the most recent trading day
    it halted -- the number the quick check pairs with the float."""
    c = _conn()
    row = c.execute("SELECT halt_date FROM halts WHERE symbol = ? "
                    "ORDER BY COALESCE(halted_at, seen_at) DESC LIMIT 1",
                    (symbol.upper(),)).fetchone()
    if not row or not row["halt_date"]:
        return 0
    return c.execute("SELECT COUNT(*) n FROM halts WHERE symbol = ? AND halt_date = ?",
                     (symbol.upper(), row["halt_date"])).fetchone()["n"]


def halts_needing_grade(cutoff_iso, limit=20):
    """Halts that have reopened long enough ago to measure, oldest first."""
    rows = _conn().execute(
        "SELECT halt_key, symbol, code, halted_at, resumed_at FROM halts "
        "WHERE graded_at IS NULL AND resumed_at IS NOT NULL AND resumed_at < ? "
        "ORDER BY resumed_at ASC LIMIT ?", (cutoff_iso, int(limit)))
    return [dict(r) for r in rows]


# Bumped whenever the measurement changes in a way that makes old rows
# incomparable with new ones. Rows graded under an older version are measured
# again, a few per pass, so the tables never mix two definitions of a number.
#   1: 5-minute bars, gap from the settled pre-halt price
#   2: 1-minute bars, gap from the halt price, run-in and direction, +5m
GRADE_VERSION = 2


def mark_halt_graded(halt_key, result):
    c = _conn()
    c.execute("UPDATE halts SET graded_at = ?, pre_price = ?, reopen_price = ?, "
              "gap_pct = ?, move_5m = ?, move_15m = ?, move_60m = ?, run_in_pct = ?, "
              "direction = ?, into_price = ?, grade_version = ? WHERE halt_key = ?",
              (datetime.now(timezone.utc).isoformat(), result.get("pre_price"),
               result.get("reopen_price"), result.get("gap_pct"),
               result.get("move_5m"), result.get("move_15m"), result.get("move_60m"),
               result.get("run_in_pct"),
               # 'unknown' rather than NULL on a miss, so the re-measure queue
               # does not pick the same unmeasurable halt up again every pass.
               result.get("direction") or "unknown", result.get("into_price"),
               GRADE_VERSION, halt_key))
    c.commit()


def halts_needing_remeasure(limit=10):
    """Halts graded under an older measurement. Newest first: the rows a
    reader is looking at heal first."""
    rows = _conn().execute(
        "SELECT halt_key, symbol, code, halted_at, resumed_at FROM halts "
        "WHERE graded_at IS NOT NULL AND gap_pct IS NOT NULL AND resumed_at IS NOT NULL "
        "AND (grade_version IS NULL OR grade_version < ?) "
        "ORDER BY resumed_at DESC LIMIT ?", (GRADE_VERSION, int(limit)))
    return [dict(r) for r in rows]


# Kept as an alias: the halt watcher called this before versions existed.
halts_needing_direction = halts_needing_remeasure


def halt_stats(days=30):
    """The base rate: what halts do at the reopen, across all of them.

    Every figure carries its own n. A median gap computed over four halts is
    not a base rate, and the whole reason this table exists is to be a control
    -- a control that hides its sample size is worse than no control.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    c = _conn()
    total = c.execute("SELECT COUNT(*) n FROM halts WHERE seen_at > ?",
                      (since,)).fetchone()["n"]
    still_open = c.execute("SELECT COUNT(*) n FROM halts WHERE resumed_at IS NULL"
                           ).fetchone()["n"]
    graded = [dict(r) for r in c.execute(
        "SELECT code, gap_pct, move_5m, move_15m, move_60m, direction, seq, run_in_pct, "
        "news_importance FROM halts WHERE graded_at IS NOT NULL "
        "AND gap_pct IS NOT NULL AND seen_at > ?", (since,))]
    for r in graded:
        r["backed"] = (r.get("news_importance") or 0) >= BIG_NEWS_MIN

    def summarise(rows):
        gaps = sorted(r["gap_pct"] for r in rows)
        if not gaps:
            return None
        follow = [r["move_60m"] for r in rows if r["move_60m"] is not None]
        follow_sorted = sorted(follow)
        quick = sorted(r["move_5m"] for r in rows if r.get("move_5m") is not None)
        return {
            "n": len(gaps),
            "median_gap": round(gaps[len(gaps) // 2], 2),
            # The scalp: reopening print to five minutes later, signed. For
            # a trade that is in and out inside the first minutes this is
            # the number; +60m describes a trade nobody here is taking.
            "median_move_5m": round(quick[len(quick) // 2], 2) if quick else None,
            "quick_n": len(quick),
            "quick_up_pct": (round(100.0 * sum(1 for m in quick if m > 0) / len(quick), 1)
                             if quick else None),
            # Signed, on purpose: bought the reopen, held an hour, this is the
            # typical result. The unsigned figures above describe volatility;
            # this one describes a trade.
            "median_move_60m": (round(follow_sorted[len(follow_sorted) // 2], 2)
                                if follow_sorted else None),
            "mean_abs_gap": round(sum(abs(g) for g in gaps) / len(gaps), 2),
            "gapped_up_pct": round(100.0 * sum(1 for g in gaps if g > 0) / len(gaps), 1),
            "over_10pct": sum(1 for g in gaps if abs(g) > 10),
            # Did the hour after the reopen extend the gap or give it back?
            "continued_pct": (round(100.0 * sum(
                1 for r in rows if r["move_60m"] is not None
                and (r["move_60m"] > 0) == (r["gap_pct"] > 0)) / len(follow), 1)
                if follow else None),
            "follow_n": len(follow),
        }

    by_code = {}
    for r in graded:
        by_code.setdefault(r["code"] or "?", []).append(r)
    by_code = {k: summarise(v) for k, v in
               sorted(by_code.items(), key=lambda kv: -len(kv[1]))[:8]}
    counts = {r["code"]: r["n"] for r in c.execute(
        "SELECT code, COUNT(*) n FROM halts WHERE seen_at > ? GROUP BY code "
        "ORDER BY n DESC", (since,))}

    # The split a halt trader actually needs. A stock that ripped INTO the
    # halt and one that cratered into it are different trades, and the first
    # halt of a runner's day is not its fourth. Pooled, those cancel into a
    # median gap near zero that describes nothing anyone traded.
    def seq_bucket(r):
        q = r.get("seq")
        if not q:
            return None
        return "1" if q == 1 else "2" if q == 2 else "3+"
    # The split the page is built around: halts with a Big News headline
    # behind them versus halts on nothing. Everything directional below is
    # computed on the news-backed set only -- the silent ones are the
    # control, not the trade.
    backed = [r for r in graded if r["backed"]]
    silent = [r for r in graded if not r["backed"]]
    by_news = {"backed": summarise(backed), "silent": summarise(silent)}
    by_direction = {d: summarise([r for r in backed if r["direction"] == d])
                    for d in ("up", "down")}
    by_direction_seq = {}
    for d in ("up", "down"):
        by_direction_seq[d] = {b: summarise([r for r in backed
                                             if r["direction"] == d and seq_bucket(r) == b])
                               for b in ("1", "2", "3+")}
    unknown_dir = sum(1 for r in backed if r["direction"] not in ("up", "down"))
    total_backed = c.execute("SELECT COUNT(*) n FROM halts WHERE seen_at > ? "
                             "AND news_importance >= ?", (since, BIG_NEWS_MIN)).fetchone()["n"]
    open_backed = c.execute("SELECT COUNT(*) n FROM halts WHERE resumed_at IS NULL "
                            "AND news_importance >= ?", (BIG_NEWS_MIN,)).fetchone()["n"]
    unchecked = c.execute("SELECT COUNT(*) n FROM halts WHERE seen_at > ? "
                          "AND news_checked_at IS NULL", (since,)).fetchone()["n"]

    return {"days": days, "total": total, "open": still_open,
            "total_backed": total_backed, "open_backed": open_backed,
            "news_unchecked": unchecked,
            "overall": summarise(graded), "by_code": by_code,
            "by_news": by_news,
            "by_direction": by_direction, "by_direction_seq": by_direction_seq,
            "direction_unknown": unknown_dir,
            "counts": counts,
            "pending": c.execute(
                "SELECT COUNT(*) n FROM halts WHERE graded_at IS NULL "
                "AND resumed_at IS NOT NULL").fetchone()["n"]}


def prune_stale_filings(keep_days=45, log=print):
    """Delete filings older than the archive window.

    Runs at startup because a bad ingest does not fix itself: a hundred 13Ds
    from December were written to this table by a source that ignores its own
    date parameters, and they would have sat on the page as "recent filings"
    forever. Removing them on boot makes the fix self-healing rather than
    something that needs a database surgeon.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    c = _conn()
    n = c.execute("DELETE FROM filings WHERE COALESCE(filed_at, seen_at) < ?",
                  (cutoff,)).rowcount
    c.commit()
    if n:
        log(f"store: removed {n} filing(s) older than {keep_days} days")
    return n


def prune(keep_days=45):
    """Keep the archive bounded. Render's smallest disk is 1GB; headlines are
    tiny but unbounded growth is still how a service dies six months from now
    with nobody watching."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    c = _conn()
    n = c.execute("DELETE FROM headlines WHERE created_at < ?", (cutoff,)).rowcount
    c.commit()
    return n


# ---- the investing screen -----------------------------------------------------

def upsert_gem(g):
    c = _conn()
    p = g.get("parts") or {}
    def pts(k):
        v = p.get(k)
        return None if v is None else int(round(v))
    c.execute(
        """INSERT INTO gems (symbol, name, sector, industry, exchange, market_cap, price, score,
                             value_pts, growth_pts, quality_pts, setup_pts, timing_pts, catalyst_pts,
                             fair_price, upside, rule_version, facts, notes,
                             disqualified, verdict, scored_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, sector=excluded.sector,
             industry=excluded.industry, exchange=excluded.exchange, market_cap=excluded.market_cap,
             price=excluded.price, score=excluded.score, value_pts=excluded.value_pts,
             growth_pts=excluded.growth_pts, quality_pts=excluded.quality_pts,
             setup_pts=excluded.setup_pts, timing_pts=excluded.timing_pts, catalyst_pts=excluded.catalyst_pts,
             fair_price=excluded.fair_price, upside=excluded.upside, rule_version=excluded.rule_version,
             facts=excluded.facts, notes=excluded.notes,
             disqualified=excluded.disqualified, verdict=excluded.verdict, scored_at=excluded.scored_at""",
        (g["symbol"], g.get("name"), g.get("sector"), g.get("industry"), g.get("exchange"),
         g.get("market_cap"), g.get("price"), float(g.get("score") or 0),
         pts("cheap") if "cheap" in p else pts("value"), pts("growth"), pts("quality"), pts("setup"),
         pts("timing"), pts("catalyst"), g.get("fair_price"), g.get("upside"), g.get("rule_version"),
         json.dumps(g.get("facts") or {}), json.dumps(g.get("notes") or []),
         g.get("disqualified"), g.get("verdict"), datetime.now(timezone.utc).isoformat()))
    c.commit()


def _gem_row(r):
    d = dict(r)
    d["facts"] = json.loads(d.get("facts") or "{}")
    d["notes"] = json.loads(d.get("notes") or "[]")
    d["parts"] = {"cheap": d.pop("value_pts", None), "quality": d.pop("quality_pts", None),
                  "growth": d.pop("growth_pts", None), "timing": d.pop("timing_pts", None),
                  "catalyst": d.pop("catalyst_pts", None)}
    d.pop("setup_pts", None)
    return d


def top_gems(limit=60, sector=None, size=None, small_cap=2e9, mid_cap=10e9, min_score=0, rule_version=None):
    """Survivors under the current rules only: after a rule change, rows scored
    under the old rules stay hidden until the re-scoring pass reaches them."""
    sql = "SELECT * FROM gems WHERE disqualified IS NULL AND score >= ?"
    args = [min_score]
    if rule_version is not None:
        sql += " AND rule_version = ?"; args.append(rule_version)
    if sector:
        sql += " AND sector = ?"
        args.append(sector)
    if size == "small":
        sql += " AND market_cap < ?"; args.append(small_cap)
    elif size == "mid":
        sql += " AND market_cap >= ? AND market_cap < ?"; args.extend([small_cap, mid_cap])
    elif size == "large":
        sql += " AND market_cap >= ?"; args.append(mid_cap)
    sql += " ORDER BY score DESC, market_cap ASC LIMIT ?"
    args.append(min(int(limit), 300))
    return [_gem_row(r) for r in _conn().execute(sql, args)]


def gem(symbol):
    r = _conn().execute("SELECT * FROM gems WHERE symbol = ?", (symbol.upper(),)).fetchone()
    return _gem_row(r) if r else None


def gem_counts(min_score=0, rule_version=None):
    c = _conn()
    rv = " AND rule_version = ?" if rule_version is not None else ""
    ra = (rule_version,) if rule_version is not None else ()
    def n(where, *a):
        return c.execute(f"SELECT COUNT(*) n FROM gems WHERE {where}{rv}", a + ra).fetchone()["n"]
    return {"scored": n("1"), "passed": n("disqualified IS NULL"),
            "shown": n("disqualified IS NULL AND score >= ?", min_score),
            "good": n("disqualified IS NULL AND score >= 62"),
            "strong": n("disqualified IS NULL AND score >= 75")}


def gem_sectors(min_score=0, rule_version=None):
    rv = " AND rule_version = ?" if rule_version is not None else ""
    ra = (rule_version,) if rule_version is not None else ()
    return [r["sector"] for r in _conn().execute(
        "SELECT sector, COUNT(*) n FROM gems WHERE disqualified IS NULL AND score >= ? AND sector IS NOT NULL"
        + rv + " GROUP BY sector ORDER BY n DESC", (min_score,) + ra)]


def gems_rule_version():
    """The rule version the newest row was scored under, or None."""
    r = _conn().execute("SELECT rule_version FROM gems ORDER BY scored_at DESC LIMIT 1").fetchone()
    return r["rule_version"] if r else None


# ---- the track record: what the list actually did afterwards --------------------
# A pick is a symbol entering the shown list. It is recorded once with the price
# that day and the S&P (SPY) that day, then graded at one, three and six months
# against SPY over the same window. A symbol that stays on the list is not
# re-recorded until 30 days after its last pick, so one stock cannot fill the
# record with thirty copies of itself.

PICK_COOLDOWN_DAYS = 30
HORIZONS = {"1m": 30, "3m": 91, "6m": 182}


def record_gem_picks(rows, spy_price, today=None):
    """rows: the shown list in rank order (dicts with symbol/score/parts/price).
    Returns the number of new picks written."""
    c = _conn()
    today = today or datetime.now(timezone.utc).date().isoformat()
    cutoff = (datetime.fromisoformat(today) - timedelta(days=PICK_COOLDOWN_DAYS)).date().isoformat()
    n = 0
    for i, g in enumerate(rows, 1):
        if not g.get("price"):
            continue
        recent = c.execute("SELECT 1 FROM gem_picks WHERE symbol = ? AND picked_on > ?", (g["symbol"], cutoff)).fetchone()
        if recent:
            continue
        cur = c.execute("""INSERT OR IGNORE INTO gem_picks (symbol, picked_on, rank, score, catalyst_pts, price, spy)
                           VALUES (?,?,?,?,?,?,?)""",
                        (g["symbol"], today, i, g.get("score"), (g.get("parts") or {}).get("catalyst"), g["price"], spy_price))
        n += cur.rowcount
    c.commit()
    return n


def gem_picks_due(horizon, today=None):
    """Picks old enough for this horizon ('1m'/'3m'/'6m') and not yet graded on it."""
    today = today or datetime.now(timezone.utc).date().isoformat()
    by = (datetime.fromisoformat(today) - timedelta(days=HORIZONS[horizon])).date().isoformat()
    return [dict(r) for r in _conn().execute(
        f"SELECT * FROM gem_picks WHERE graded_{horizon} IS NULL AND picked_on <= ? ORDER BY picked_on", (by,))]


def grade_gem_pick(pick_id, horizon, ret, spy_ret):
    c = _conn()
    c.execute(f"UPDATE gem_picks SET ret_{horizon} = ?, spy_{horizon} = ?, graded_{horizon} = ? WHERE id = ?",
              (ret, spy_ret, datetime.now(timezone.utc).isoformat(), pick_id))
    c.commit()


def gem_track_summary(catalyst_split=60):
    """Per horizon: how many graded, average pick return, average SPY return,
    how often the pick beat SPY -- overall and split by catalyst score, which
    is the question the record exists to answer."""
    c = _conn()
    out = {"picks": c.execute("SELECT COUNT(*) n FROM gem_picks").fetchone()["n"],
           "first_pick": (c.execute("SELECT MIN(picked_on) m FROM gem_picks").fetchone() or {})["m"],
           "horizons": {}}
    for h in HORIZONS:
        def agg(where="1", *a):
            r = c.execute(f"""SELECT COUNT(*) n, AVG(ret_{h}) ret, AVG(spy_{h}) spy,
                                     AVG(CASE WHEN ret_{h} > spy_{h} THEN 1.0 ELSE 0.0 END) hit
                              FROM gem_picks WHERE graded_{h} IS NOT NULL AND {where}""", a).fetchone()
            return {"n": r["n"], "ret": round(r["ret"], 2) if r["ret"] is not None else None,
                    "spy": round(r["spy"], 2) if r["spy"] is not None else None,
                    "hit": round(r["hit"] * 100, 1) if r["hit"] is not None else None}
        out["horizons"][h] = {"all": agg(), "high_catalyst": agg("catalyst_pts >= ?", catalyst_split),
                              "low_catalyst": agg("(catalyst_pts < ? OR catalyst_pts IS NULL)", catalyst_split)}
    return out


def gem_picks_recent(limit=60):
    return [dict(r) for r in _conn().execute(
        "SELECT * FROM gem_picks ORDER BY picked_on DESC, rank ASC LIMIT ?", (int(limit),))]


def activist_13d(symbol, days=180):
    """Schedule 13D filings on file for this ticker inside the window -- the
    site's own SEC watcher already collects these."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    rows = _conn().execute(
        """SELECT reporting_person, percent, filed_at, form FROM filings
           WHERE ticker = ? AND kind = '13d' AND (form LIKE 'SC 13D%' OR form LIKE 'SCHEDULE 13D%')
             AND COALESCE(filed_at, seen_at) >= ?
           ORDER BY COALESCE(filed_at, seen_at) DESC""", (symbol.upper(), since)).fetchall()
    if not rows:
        return {"activist_13d": 0}
    r = rows[0]
    return {"activist_13d": len(rows), "activist_who": r["reporting_person"], "activist_pct": r["percent"],
            "activist_filed": r["filed_at"]}


def gems_last_scored():
    r = _conn().execute("SELECT MAX(scored_at) m FROM gems").fetchone()
    return r["m"] if r else None
