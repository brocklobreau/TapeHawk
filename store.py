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
  move_60m      REAL
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
                         ("uncovered", "INTEGER"), ("coverage_at", "TEXT")):
            if col not in fhave:
                c.execute(f"ALTER TABLE filings ADD COLUMN {col} {ddl}")
        c.executescript(INDEX_SQL)          # only now are all columns present
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
           "graded_at, grade_symbol, move_15m, move_60m, grade_note "
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
        moves.sort()
        signed = [r["move_60m"] for r in rs]
        return {
            "n": len(rs),
            "median_abs_move": round(moves[len(moves) // 2], 2),
            "mean_abs_move": round(sum(moves) / len(moves), 2),
            "moved_over_2pct": round(100.0 * sum(1 for m in moves if m >= 2) / len(moves), 1),
            "mean_signed": round(sum(signed) / len(signed), 3),
            "pct_up": round(100.0 * sum(1 for m in signed if m > 0) / len(signed), 1),
        }

    by_impact = {}
    for lvl in ("high", "medium", "low"):
        by_impact[lvl] = summarise([r for r in all_rows if r["impact_level"] == lvl])
    by_impact["unrated"] = summarise([r for r in all_rows if not r["impact_level"]])

    by_tone = {}
    for t in ("positive", "negative", "unclear"):
        by_tone[t] = summarise([r for r in all_rows if r["tone"] == t])

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
    return {"days": days, "overall": summarise(all_rows), "big_news": summarise(big),
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
        c.commit()
        return "new"
    if h.get("resumed_at") and not row["resumed_at"]:
        c.execute("UPDATE halts SET resumed_at = ?, quote_at = COALESCE(?, quote_at) "
                  "WHERE id = ?", (h["resumed_at"], h.get("quote_at"), row["id"]))
        c.commit()
        return "resumed"
    return None


def recent_halts(limit=120, symbol=None, code=None, open_only=False):
    sql = "SELECT * FROM halts WHERE 1=1"
    args = []
    if symbol:
        sql += " AND symbol = ?"
        args.append(symbol.upper())
    if code:
        sql += " AND code = ?"
        args.append(code.upper())
    if open_only:
        sql += " AND resumed_at IS NULL"
    sql += " ORDER BY COALESCE(halted_at, seen_at) DESC, id DESC LIMIT ?"
    args.append(min(int(limit), 400))
    return [dict(r) for r in _conn().execute(sql, args)]


def halts_needing_grade(cutoff_iso, limit=20):
    """Halts that have reopened long enough ago to measure, oldest first."""
    rows = _conn().execute(
        "SELECT halt_key, symbol, code, halted_at, resumed_at FROM halts "
        "WHERE graded_at IS NULL AND resumed_at IS NOT NULL AND resumed_at < ? "
        "ORDER BY resumed_at ASC LIMIT ?", (cutoff_iso, int(limit)))
    return [dict(r) for r in rows]


def mark_halt_graded(halt_key, result):
    c = _conn()
    c.execute("UPDATE halts SET graded_at = ?, pre_price = ?, reopen_price = ?, "
              "gap_pct = ?, move_15m = ?, move_60m = ? WHERE halt_key = ?",
              (datetime.now(timezone.utc).isoformat(), result.get("pre_price"),
               result.get("reopen_price"), result.get("gap_pct"),
               result.get("move_15m"), result.get("move_60m"), halt_key))
    c.commit()


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
        "SELECT code, gap_pct, move_15m, move_60m FROM halts "
        "WHERE graded_at IS NOT NULL AND gap_pct IS NOT NULL AND seen_at > ?",
        (since,))]

    def summarise(rows):
        gaps = sorted(r["gap_pct"] for r in rows)
        if not gaps:
            return None
        follow = [r["move_60m"] for r in rows if r["move_60m"] is not None]
        return {
            "n": len(gaps),
            "median_gap": round(gaps[len(gaps) // 2], 2),
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
    return {"days": days, "total": total, "open": still_open,
            "overall": summarise(graded), "by_code": by_code,
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
