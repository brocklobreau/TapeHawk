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
  content      TEXT
);
"""

INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_created ON headlines(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_noise_created ON headlines(is_noise, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_importance ON headlines(importance DESC, id DESC);
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
                         ("content", "TEXT")):
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
            importance, reasons, content)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (article.get("alpaca_id"), article["created_at"], article["received_at"],
         article.get("latency_ms"), article["headline"], article.get("summary"),
         article.get("author"), article.get("source"), article.get("url"),
         json.dumps(article.get("symbols") or []),
         json.dumps(article.get("categories") or []),
         1 if article.get("is_noise") else 0,
         int(article.get("importance") or 0),
         json.dumps(article.get("reasons") or []),
         article.get("content")))
    c.commit()
    return cur.rowcount > 0


def _row(r):
    d = dict(r)
    d["symbols"] = json.loads(d.get("symbols") or "[]")
    d["categories"] = json.loads(d.get("categories") or "[]")
    d["reasons"] = json.loads(d.get("reasons") or "[]")
    d["has_content"] = bool(d.get("content"))
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
           "importance, reasons FROM headlines WHERE 1=1")
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


def prune(keep_days=45):
    """Keep the archive bounded. Render's smallest disk is 1GB; headlines are
    tiny but unbounded growth is still how a service dies six months from now
    with nobody watching."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    c = _conn()
    n = c.execute("DELETE FROM headlines WHERE created_at < ?", (cutoff,)).rowcount
    c.commit()
    return n
