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

INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_created ON headlines(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_noise_created ON headlines(is_noise, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_importance ON headlines(importance DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_ungraded ON headlines(graded_at, created_at);
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
    recent = [{"headline": r["headline"], "symbol": r["grade_symbol"],
               "created_at": r["created_at"], "url": r["url"],
               "impact_level": r["impact_level"], "tone": r["tone"],
               "move_15m": r["move_15m"], "move_60m": r["move_60m"],
               "importance": r["importance"]}
              for r in all_rows[:40]]

    pending = c.execute("SELECT COUNT(*) n FROM headlines "
                        "WHERE graded_at IS NULL AND is_noise = 0").fetchone()["n"]
    skipped = c.execute("SELECT COUNT(*) n FROM headlines "
                        "WHERE graded_at IS NOT NULL AND move_60m IS NULL").fetchone()["n"]
    return {"days": days, "overall": summarise(all_rows), "big_news": summarise(big),
            "by_impact": by_impact, "by_tone": by_tone, "by_category": by_cat,
            "recent": recent, "pending": pending, "ungradeable": skipped}


def prune(keep_days=45):
    """Keep the archive bounded. Render's smallest disk is 1GB; headlines are
    tiny but unbounded growth is still how a service dies six months from now
    with nobody watching."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    c = _conn()
    n = c.execute("DELETE FROM headlines WHERE created_at < ?", (cutoff,)).rowcount
    c.commit()
    return n
