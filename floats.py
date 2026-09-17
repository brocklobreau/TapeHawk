"""
Share float, on demand and cached.

Float is the single biggest determinant of how far a halt chain can run -- a
five-million-share float can halt six times in a morning, a two-hundred-million
share float cannot -- and it was the one input to the halt checklist that the
site could not show. This fetches it from FMP, keeps it for hours (float moves
on offerings and lock-up expiries, not minute to minute), and never blocks a
page: the halts feed reads from the cache only, and a background pass warms the
cache for the names actually halting today.

Two sources, in order:
  1. FMP's shares-float endpoint: float shares, shares outstanding, free float %.
  2. The quote's sharesOutstanding, reported honestly as OUTSTANDING rather than
     float. Outstanding is an upper bound on float; showing it as if it were the
     float would make every name look bigger than it is.

CREDENTIALS NEVER TOUCH THIS FILE. FMP_API_KEY is read from the environment.
"""
import os
import threading
import time
from datetime import datetime, timezone

import requests

BASE = "https://financialmodelingprep.com/stable"
TIMEOUT = 12
CACHE_SECONDS = 6 * 3600          # a float figure is good for the session
MISS_SECONDS = 45 * 60            # do not hammer FMP for a symbol it has no data on
MAX_WARM_PER_PASS = 8             # halts feed polls every 45s; 8 lookups a pass is gentle

_cache = {}                       # symbol -> (fetched_at_epoch, record | None)
_lock = threading.Lock()
_status = {"lookups": 0, "hits": 0, "misses": 0, "last_error": None, "warmed": 0}


def _key():
    return os.environ.get("FMP_API_KEY", "").strip()


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None      # NaN guard


def _get(path, params):
    p = dict(params)
    p["apikey"] = _key()
    r = requests.get(f"{BASE}/{path}", params=p, timeout=TIMEOUT)
    if r.status_code == 402:
        return None                   # plan-restricted: treat as absent
    if r.status_code != 200:
        raise RuntimeError(f"FMP {r.status_code} for {path}")
    j = r.json()
    if isinstance(j, list):
        return j[0] if j else None
    return j or None


# The tiers a halt trader actually thinks in. Thresholds are conventional,
# not official: "low float" has no regulatory definition, and these are the
# lines most small-cap traders draw. Stated in the payload so the page can
# show the rule rather than a bare word.
TIERS = (
    (5e6,    "micro",  "Micro float -- under 5M. Halts chain easily; moves are violent both ways"),
    (20e6,   "low",    "Low float -- under 20M. The classic halt-runner profile"),
    (100e6,  "mid",    "Mid float -- 20M to 100M. Needs real volume to chain halts"),
    (float("inf"), "large", "Large float -- over 100M. Rarely halts more than once"),
)


def tier(float_shares):
    if float_shares is None:
        return None, None
    for cap, name, note in TIERS:
        if float_shares < cap:
            return name, note
    return None, None


def fetch(symbol):
    """One live lookup. Returns a record or None; never raises to the caller."""
    symbol = symbol.upper().strip()
    if not symbol or not _key():
        return None
    _status["lookups"] += 1
    rec = None
    try:
        f = _get("shares-float", {"symbol": symbol})
        if f:
            fs = _num(f.get("floatShares"))
            out = _num(f.get("outstandingShares"))
            if fs or out:
                rec = {"symbol": symbol, "float_shares": fs, "outstanding": out,
                       "free_float_pct": _num(f.get("freeFloat")),
                       "as_of": f.get("date"), "source": "shares-float",
                       "is_float": fs is not None}
        if rec is None:
            q = _get("quote", {"symbol": symbol})
            out = _num((q or {}).get("sharesOutstanding"))
            if out:
                rec = {"symbol": symbol, "float_shares": None, "outstanding": out,
                       "free_float_pct": None, "as_of": None, "source": "quote",
                       "is_float": False}
        if rec:
            basis = rec["float_shares"] if rec["float_shares"] is not None else rec["outstanding"]
            rec["tier"], rec["tier_note"] = tier(basis)
            rec["fetched_at"] = datetime.now(timezone.utc).isoformat()
            _status["hits"] += 1
        else:
            _status["misses"] += 1
        _status["last_error"] = None
    except Exception as e:
        _status["last_error"] = str(e)
        _status["misses"] += 1
        rec = None
    with _lock:
        _cache[symbol] = (time.time(), rec)
    return rec


def get(symbol, allow_fetch=True):
    """Cached record, fetching if stale and allowed. None when unknown."""
    symbol = (symbol or "").upper().strip()
    if not symbol:
        return None
    with _lock:
        hit = _cache.get(symbol)
    if hit:
        age = time.time() - hit[0]
        if hit[1] is not None and age < CACHE_SECONDS:
            return hit[1]
        if hit[1] is None and age < MISS_SECONDS:
            return None
    if not allow_fetch:
        return None
    return fetch(symbol)


def cached_only(symbols):
    """Records for whichever of these symbols are already known. No network:
    this runs on the request path of the halts page."""
    out = {}
    with _lock:
        for s in {(x or "").upper().strip() for x in symbols if x}:
            hit = _cache.get(s)
            if hit and hit[1] is not None and time.time() - hit[0] < CACHE_SECONDS:
                out[s] = hit[1]
    return out


def warm(symbols, log=print):
    """Fetch a few unknown symbols, newest first. Called from the halt
    watcher after each poll so today's halts have a float by the time
    anyone looks, without the page ever waiting on FMP."""
    if not _key():
        return 0
    done = 0
    seen = set()
    for s in symbols:
        s = (s or "").upper().strip()
        if not s or s in seen:
            continue
        seen.add(s)
        with _lock:
            hit = _cache.get(s)
        if hit:
            age = time.time() - hit[0]
            if (hit[1] is not None and age < CACHE_SECONDS) or (hit[1] is None and age < MISS_SECONDS):
                continue
        fetch(s)
        done += 1
        if done >= MAX_WARM_PER_PASS:
            break
    if done:
        _status["warmed"] += done
        log(f"floats: looked up {done} symbol(s)")
    return done


def status():
    return dict(_status, cached=len(_cache))
