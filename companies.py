"""
Company size, cached: how big is the company behind a headline?

The Big News scorer sizes a catalyst against the company (classify.importance
with market_cap and exchange): a $40M contract is the whole of a $60M company
and a line item for a $200B one, and a Swiss or Canadian listing cannot be
traded here at all. The size comes from FMP's quote endpoint, once per
ticker, and lives in sqlite (store: table `companies`) for FRESH_DAYS.

Two rules that never bend:
  * `size_of()` NEVER fetches. It reads one row and, when the row is missing
    or stale, queues the ticker for the worker. The wire thread calls it on
    every release and must never wait on FMP.
  * The worker drains the queue at most RATE_PER_MIN a minute. FMP Starter
    allows 300 a minute and floats, gems and the grader share the key; sixty
    here leaves room for the rest.

When a size lands, every row for that ticker from the last 48 hours is
re-scored WITH it (store.rescore_symbol), so a release promoted or demoted
by the company's size is right within a minute of its first sighting; the
page's two-minute re-poll of the rail picks it up.

CREDENTIALS NEVER TOUCH THIS FILE. FMP_API_KEY is read from the environment;
no key means the worker never starts and `size_of` answers from the cache.
"""
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import requests

import classify
import store

BASE = "https://financialmodelingprep.com/stable"
TIMEOUT = 10
FRESH_DAYS = 3                 # a size older than this is served AND re-queued
ERROR_RETRY_HOURS = 24         # a ticker FMP had nothing on (402, no listing) is not asked again for a day
TRANSIENT_RETRY_HOURS = 1      # a ticker FMP could not be ASKED about (timeout, 429, 5xx) is tried again in an hour
RETRY_PAUSE_S = 60             # ... and the worker waits this long after one before the next ticker
WARM_DAYS = 14                 # at boot, queue every ticker seen in the last two weeks
RATE_PER_MIN = 60              # FMP calls a minute, at most (a ticker can take two: quote, then profile)
RESCORE_HOURS = 48             # how far back a fresh size re-scores that ticker's rows
NO_LISTING = "no US listing"   # the error text when quote AND profile are empty: FMP has no such US ticker
TRANSIENT = "fetch failed"     # the error text's start when FMP could not be asked

_lock = threading.Lock()
_queue = deque()
_queued = set()
_status = {"fetched_today": 0, "errors": 0, "last_error": None, "last_fetched": None,
           "running": False, "day": None, "calls": 0}
_said = set()                  # tickers whose fetch error was already logged once
_thread = None
_sleep = time.sleep            # tests swap this out
_KEY_RE = re.compile(r"apikey=[^&\s)]+", re.I)


class Transient(Exception):
    """FMP answered but not with data: a 429 or a 5xx. Tried again soon."""


def _scrub(text):
    """An exception's words with the key taken out. requests puts the whole
    URL, query string included, into a connection error's message."""
    return _KEY_RE.sub("apikey=***", str(text))


def retry_hours(error):
    """How long a stored error keeps a ticker out of the queue."""
    return TRANSIENT_RETRY_HOURS if str(error or "").startswith(TRANSIENT) else ERROR_RETRY_HOURS


def _key():
    return os.environ.get("FMP_API_KEY", "").strip()


def _now():
    return datetime.now(timezone.utc)


def _age_days(iso):
    try:
        d = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None
    d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return (_now() - d).total_seconds() / 86400.0


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


# ---- the cache ------------------------------------------------------------------------

def want(symbol):
    """Queue a ticker for the worker. Never fetches, never waits. A ticker
    already in the queue is not queued twice; one whose last fetch was an
    error is left alone for retry_hours(error): a day when FMP had nothing
    (402, no listing), an hour when FMP could not be asked."""
    sym = str(symbol or "").upper().strip()
    if not sym:
        return False
    with _lock:
        if sym in _queued:
            return False
    row = store.company(sym)
    if row and row.get("error"):
        age = _age_days(row.get("fetched_at"))
        if age is not None and age < retry_hours(row["error"]) / 24.0:
            return False
    with _lock:
        if sym in _queued:
            return False
        _queued.add(sym)
        _queue.append(sym)
    return True


def size_of(symbol):
    """The cached row for a ticker (dict with market_cap, exchange, name,
    price, fetched_at, error) or None. NEVER fetches: one indexed sqlite
    read. A missing row is queued; a row older than FRESH_DAYS is returned
    as it is and queued for a refresh; an error row past its retry window
    (retry_hours) is queued again."""
    sym = str(symbol or "").upper().strip()
    if not sym:
        return None
    row = store.company(sym)
    if row is None:
        want(sym)
        return None
    age = _age_days(row.get("fetched_at"))
    limit = retry_hours(row["error"]) / 24.0 if row.get("error") else FRESH_DAYS
    if age is None or age > limit:
        want(sym)
    return row


def note(symbols):
    """Queue every ticker on a release whose size is unknown or stale (the
    first one was looked up by size_of already; this covers the rest)."""
    for s in symbols or []:
        size_of(s)


# ---- the fetch ------------------------------------------------------------------------

def _get(path, symbol):
    """One FMP call, counted. 402 comes back as 402 (not on the plan); a 429
    or a 5xx raises Transient (asked again in an hour); 404 is an empty
    answer; anything else raises."""
    with _lock:
        _status["calls"] += 1
    r = requests.get(f"{BASE}/{path}", params={"symbol": symbol, "apikey": _key()}, timeout=TIMEOUT)
    if r.status_code == 402:
        return 402
    if r.status_code == 404:
        return None
    if r.status_code == 429 or r.status_code >= 500:
        raise Transient(f"FMP {r.status_code} for {path}")
    if r.status_code != 200:
        raise RuntimeError(f"FMP {r.status_code} for {path}")
    j = r.json()
    if isinstance(j, list):
        return j[0] if j else None
    return j or None


def _fetch_fmp(symbol):
    """One ticker from FMP. Returns {name, market_cap, price, exchange,
    country} or {"error": words}. Quote first; profile when the quote has no
    exchange or nothing came back at all. Quote AND profile both empty is its
    own answer -- {"error": NO_LISTING, "exchange": "NONE"} -- so a ticker
    FMP does not know as a US listing (Novartis's NOVN is NOVN.SW there)
    can never be big, instead of staying at the un-sized score for good.
    Raises on a network error or a 429/5xx (the worker stores it as a
    transient error and asks again in an hour)."""
    q = _get("quote", symbol)
    if q == 402:
        return {"error": "not on this FMP plan (402)"}
    q = q or {}
    rec = {"name": q.get("name"), "market_cap": _num(q.get("marketCap")), "price": _num(q.get("price")),
           "exchange": (q.get("exchange") or None), "country": None}
    if not rec["exchange"]:
        p = _get("profile", symbol)
        if p == 402:
            return {"error": "not on this FMP plan (402)"}
        if p:
            # the short name ("NYSE"); the long one ("New York Stock Exchange") only as a fallback
            rec["exchange"] = p.get("exchangeShortName") or p.get("exchange") or None
            rec["country"] = p.get("country") or None
            rec["name"] = rec["name"] or p.get("companyName") or p.get("name")
            if rec["market_cap"] is None:
                rec["market_cap"] = _num(p.get("marketCap") or p.get("mktCap"))
        elif not q:
            return {"error": NO_LISTING, "exchange": classify.NO_LISTING}
    if rec["market_cap"] is None and not rec["exchange"]:
        return {"error": "no size or exchange in the answer"}
    return rec


fetch_fn = _fetch_fmp          # tests replace this


def score_with_size(headline, symbols, categories, market_cap=None, exchange=None):
    """What the re-scorers call: classify.importance with the size on."""
    return classify.importance(headline, symbols, categories, market_cap=market_cap, exchange=exchange)


def on_sized(symbol, log=print):
    """A size landed: re-score that ticker's rows from the last 48 hours
    with it. Returns how many rows changed importance."""
    try:
        return store.rescore_symbol(symbol, RESCORE_HOURS, score_with_size)
    except Exception as e:
        log(f"companies: re-score of {symbol} failed -- {str(e)[:100]}")
        return 0


def process_one(log=print):
    """Take one ticker off the queue, fetch it, store it, re-score its rows.
    Returns the ticker, or None when the queue was empty. The worker calls
    this; tests call it straight. An error is stored with fetched_at = now:
    a final one (402, no listing, empty answer) keeps the ticker out of the
    queue for ERROR_RETRY_HOURS, a transient one ("fetch failed: ...") for
    TRANSIENT_RETRY_HOURS. No exception text here ever carries the key."""
    with _lock:
        if not _queue:
            return None
        sym = _queue.popleft()
        _queued.discard(sym)
    today = _now().date().isoformat()
    with _lock:
        if _status["day"] != today:
            _status["day"] = today
            _status["fetched_today"] = 0
    rec = None
    try:
        rec = fetch_fn(sym) or {"error": "no answer"}
    except requests.RequestException as e:
        # the message would carry the whole URL, key included: the class name is enough
        rec = {"error": f"{TRANSIENT}: {type(e).__name__}"}
    except Transient as e:
        rec = {"error": f"{TRANSIENT}: {_scrub(e)[:60]}"}
    except Exception as e:
        rec = {"error": f"{TRANSIENT}: {type(e).__name__}: {_scrub(e)[:80]}"}
    if not isinstance(rec, dict):
        rec = {"error": "bad answer"}
    transient = str(rec.get("error") or "").startswith(TRANSIENT)
    if transient and sym not in _said:
        _said.add(sym)
        log(f"companies: {sym}: {rec['error']} (asked again in {TRANSIENT_RETRY_HOURS} h)")
    store.upsert_company(sym, rec.get("name"), rec.get("market_cap"), rec.get("price"),
                         rec.get("exchange"), rec.get("country"), rec.get("error"))
    with _lock:
        _status["fetched_today"] += 1
        _status["last_fetched"] = _now().isoformat()
        _status["last_transient"] = transient
        if rec.get("error"):
            _status["errors"] += 1
            _status["last_error"] = f"{sym}: {rec['error']}"
    # a size, or the answer "no US listing", re-scores the ticker's rows
    if not rec.get("error") or rec.get("error") == NO_LISTING:
        on_sized(sym, log)
    return sym


def _loop(log):
    gap = 60.0 / float(RATE_PER_MIN)
    while True:
        with _lock:
            before = _status["calls"]
        try:
            sym = process_one(log)
        except Exception as e:
            log(f"companies: worker error -- {_scrub(e)[:120]}")
            sym = None
        with _lock:
            calls = max(1, _status["calls"] - before)
            transient = _status.get("last_transient") if sym else False
        if transient:
            _sleep(RETRY_PAUSE_S)                  # FMP is unhappy: give it a minute
        else:
            _sleep(gap * calls if sym else 2.0)    # the budget counts calls, not tickers


def start(log=print):
    """Start the worker thread, once. No key -> never starts (and says so);
    size_of keeps answering from whatever is cached."""
    global _thread
    if not _key():
        log("companies: FMP_API_KEY not set -- company sizes come from the cache only")
        return False
    with _lock:
        if _thread is not None:
            return True
        _thread = threading.Thread(target=_loop, args=(log,), daemon=True, name="companies")
        _status["running"] = True
    _thread.start()
    log(f"companies: worker started ({RATE_PER_MIN} FMP lookups a minute at most)")
    return True


def warm(store_mod=None, log=print):
    """Queue every ticker on a headline from the last WARM_DAYS, newest
    first (one query). Called from app.start_once after the worker starts."""
    s = store_mod or store
    n = 0
    for sym in s.symbols_since(WARM_DAYS):
        before = len(_queue)
        size_of(sym)
        if len(_queue) > before:
            n += 1
    if n:
        log(f"companies: {n} ticker(s) from the last {WARM_DAYS} days queued for a size")
    return n


def status():
    with _lock:
        s = dict(_status)
        s["queued"] = len(_queue)
    try:
        s["cached"] = store.companies_count()
    except Exception:
        s["cached"] = None
    s["rate_per_min"] = RATE_PER_MIN
    s["fresh_days"] = FRESH_DAYS
    s.pop("day", None)
    s.pop("last_transient", None)
    return s
