"""
Hidden gems: the catalyst is out, the price has not moved yet.

The Big News rail tells you a headline matters. The Halts tab tells you when
the crowd has already arrived -- by then the stock is halted and you are
chasing. This is the window in between: a headline the classifier scored as
big and positive, on a real equity, in the last few hours, where the price is
still sitting near where it was when the headline landed.

"Hidden" is measured, not asserted. For every candidate the price at the
headline is fixed (the grader's entry bar if it has one, otherwise the first
bar at or after the headline, otherwise the prior close for anything that
arrived outside a session), and the move since then is the whole test:

    quiet     |move| under QUIET_BAND_PCT   -- nobody has reacted
    stirring  between the two bands          -- starting to go
    ran       beyond RAN_PCT at any horizon  -- the crowd is here; not a gem

On top of that sits the halt checklist the rest of the site already runs:
float tier, offering check, and the research panel's valuation read. A quiet
headline on a low-float name with no offering on file and a cheap multiple
ranks first. A quiet headline on a name that filed a shelf last week ranks
last, because "quiet" there usually means "the buyers know".

Everything here is computed in a background thread and served from memory.
The page never waits on FMP or EDGAR.

CREDENTIALS NEVER TOUCH THIS FILE. FMP_API_KEY is read by the modules this
one calls.
"""
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

import classify
import floats
import offerings
import outcomes
import research
import store

BASE = "https://financialmodelingprep.com/stable"
TIMEOUT = 12

LOOKBACK_HOURS = 8          # older than this is not "not yet", it is "did not"
QUIET_BAND_PCT = 3.0        # inside this band nothing has happened
RAN_PCT = 8.0               # beyond this at any horizon the move is done
REFRESH_SECONDS = 180
MAX_CANDIDATES = 20
FULL_LOOKUP_SECONDS = 30 * 60   # research/float/offering are re-pulled this often
NEAR_LOW_PCT = 20.0         # within this of the 52-week low counts as beaten down
UPSIDE_PCT = 25.0           # analyst target this far above price counts as a value note
HISTORY_DAYS = 90           # how far back "stories like this" looks
STALE_MINUTES = 240         # a quiet story older than this has had its chance
LIKELY_PCT = 50             # spike rate at or above this: "usually spikes"
UNLIKELY_PCT = 30           # below this: "usually nothing"

_state = {"gems": [], "ran": [], "checked_at": None, "candidates": 0, "baseline": None,
          "passes": 0, "last_error": None, "enabled": False}
_lock = threading.Lock()
_full = {}                  # symbol -> (epoch, research/float/offering snapshot)


def _log_default(msg):
    print(msg, flush=True)


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _quote(symbol):
    r = requests.get(f"{BASE}/quote", params={"symbol": symbol,
                     "apikey": os.environ.get("FMP_API_KEY", "")}, timeout=TIMEOUT)
    if r.status_code != 200:
        return None
    j = r.json()
    q = j[0] if isinstance(j, list) and j else (j if isinstance(j, dict) else None)
    return q if q and q.get("price") is not None else None


def _parse_iso(s):
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def in_session(now_utc=None):
    """Regular hours only. Pre-market prints exist but are thin; a headline at
    7am is judged against the open, which is what the page says."""
    now = now_utc or datetime.now(timezone.utc)
    if outcomes.MARKET_TZ is None:                  # pragma: no cover
        return True
    et = now.astimezone(outcomes.MARKET_TZ)
    if et.weekday() >= 5:
        return False
    mins = et.hour * 60 + et.minute
    return 9 * 60 + 30 <= mins < 16 * 60


def entry_price(symbol, headline_iso, quote, graded_entry=None):
    """Price the headline landed on. Returns (price, how)."""
    if graded_entry:
        return float(graded_entry), "grader"
    t = outcomes.to_market_time(headline_iso)
    if t is not None:
        try:
            bars = outcomes.fetch_bars(symbol, t.date())
        except Exception:
            bars = []
        for dt, o, _c in bars:
            if dt >= t and o:
                return float(o), "bar at headline"
    prev = _num((quote or {}).get("previousClose"))
    if prev:
        return prev, "prior close"
    return None, None


def classify_move(move_now, move_15m=None, move_60m=None):
    peak = max(abs(x) for x in (move_now, move_15m or 0, move_60m or 0) if x is not None)
    if peak >= RAN_PCT:
        return "ran"
    if abs(move_now) < QUIET_BAND_PCT:
        return "quiet"
    return "stirring"


def value_notes(res, price):
    """The undervaluation half of the page, from the research module's own
    numbers. Notes are the specific facts; the read is its four-rule summary."""
    notes, points = [], 0
    if not res:
        return {"read": None, "score": None, "notes": [], "points": 0}
    v = res.get("valuation") or {}
    d = res.get("day") or {}
    rd = res.get("read") or {}
    pe = _num(v.get("pe")); pb = _num(v.get("price_to_book")); ps = _num(v.get("price_to_sales"))
    up = _num(v.get("analyst_upside_pct")); lo = _num(d.get("year_low")); hi = _num(d.get("year_high"))
    if pe and 0 < pe < 12:
        notes.append(f"P/E {pe:.1f}"); points += 1
    if pb and 0 < pb < 1:
        notes.append(f"Trades below book (P/B {pb:.2f})"); points += 1
    if ps and 0 < ps < 1:
        notes.append(f"Under 1x sales (P/S {ps:.2f})"); points += 1
    if up and up >= UPSIDE_PCT:
        notes.append(f"Analyst target {up:.0f}% above price"); points += 1
    if price and lo and hi and hi > lo:
        off_low = (price - lo) / lo * 100
        if off_low <= NEAR_LOW_PCT:
            notes.append(f"{off_low:.0f}% off its 52-week low"); points += 1
    read = rd.get("read")
    if read and read.startswith("Looks cheap"):
        points += 2
    elif read and read.startswith("Cheap multiple, but"):
        points -= 1
        notes.append("Value-trap read: cheap because the business is shrinking")
    elif read and read.startswith("Looks expensive"):
        points -= 1
    return {"read": read, "score": rd.get("score"), "notes": notes[:4], "points": points}


def odds_label(h):
    """One line a person can act on, from the archive numbers."""
    if not h or not h.get("n"):
        return "unknown", "No graded history yet"
    scope = ("stories like this" if h.get("scope") != "all"
             else "all big positive stories")
    if not h.get("enough"):
        return "unknown", f"Not enough history yet ({h['n']} graded)"
    rate = h["spike_rate"]
    med = h.get("median_60m")
    tail = (f"{h['spiked']} of {h['n']} {scope} moved {h['spike_pct']:g}%+ within the hour"
            + (f", median {med:+.1f}%" if med is not None else ""))
    if rate >= LIKELY_PCT:
        return "likely", f"Usually spikes — {tail}"
    if rate >= UNLIKELY_PCT:
        return "coinflip", f"Coin flip — {tail}"
    return "unlikely", f"Usually nothing — {tail}"


def guard(gem):
    """The safeguard: the reasons NOT to take it, each one a measured fact.
    Empty means it passed every check, which is not the same as a buy."""
    fails = []
    off = gem.get("offering") or {}
    if off.get("severity") == "red":
        fails.append("offering on file — supply is coming")
    elif off.get("severity") == "amber":
        fails.append("shelf or resale on file — supply can come")
    fl = gem.get("float") or {}
    if fl.get("tier") == "large":
        fails.append("large float — rarely chains")
    v = gem.get("value") or {}
    if (v.get("read") or "").startswith("Cheap multiple, but"):
        fails.append("value trap read — shrinking business")
    if gem.get("odds_level") == "unlikely":
        fails.append("stories like this usually do nothing")
    if (gem.get("age_min") or 0) > STALE_MINUTES and gem.get("status") == "quiet":
        fails.append(f"quiet for {gem['age_min'] // 60}h — it had its chance")
    if (gem.get("importance") or 0) < store.BIG_NEWS_MIN + 1:
        fails.append("only just clears the Big News bar")
    return fails


def rank(gem):
    """Higher is more interesting. Stated so the page can show the reasons."""
    why, s = [], 0
    imp = gem.get("importance") or 0
    s += imp; why.append(f"importance {imp}")
    if gem["status"] == "quiet":
        s += 2; why.append("no reaction yet")
    fl = gem.get("float") or {}
    if fl.get("tier") in ("micro", "low"):
        s += 2; why.append(f"{fl['tier']} float")
    vp = (gem.get("value") or {}).get("points") or 0
    if vp:
        s += vp; why.append(f"value +{vp}" if vp > 0 else f"value {vp}")
    off = gem.get("offering") or {}
    if off.get("severity") == "red":
        s -= 4; why.append("offering on file")
    elif off.get("severity") == "amber":
        s -= 1; why.append("shelf or resale on file")
    lvl = gem.get("odds_level")
    if lvl == "likely":
        s += 3; why.append("usually spikes")
    elif lvl == "coinflip":
        s += 1; why.append("coin flip")
    elif lvl == "unlikely":
        s -= 2; why.append("usually nothing")
    gem["score"] = s
    gem["why"] = why
    return s


def _full_lookup(symbol, log):
    """Research + float + offering. Cached here for FULL_LOOKUP_SECONDS on top
    of each module's own cache, so a gem that sits on the page for hours costs
    a handful of calls, not a handful per refresh."""
    hit = _full.get(symbol)
    if hit and time.time() - hit[0] < FULL_LOOKUP_SECONDS:
        return hit[1]
    snap = {"research": None, "float": None, "offering": None}
    try:
        snap["research"] = research.lookup(symbol)
    except Exception as e:
        log(f"gems: research for {symbol} failed ({str(e)[:80]})")
    try:
        snap["float"] = floats.get(symbol)
    except Exception:
        pass
    try:
        o = offerings.check(symbol, log=log)
        v = o.get("verdict") or []
        worst = None
        for sev in ("red", "amber"):
            for f in v:
                if f.get("severity") == sev:
                    worst = {"severity": sev, "text": f.get("text")}
                    break
            if worst:
                break
        snap["offering"] = worst or ({"severity": "clear", "text": "Nothing on file in the window"}
                                     if not o.get("error") else
                                     {"severity": "unknown", "text": o.get("error")})
    except Exception as e:
        snap["offering"] = {"severity": "unknown", "text": str(e)[:80]}
    _full[symbol] = (time.time(), snap)
    if len(_full) > 200:
        for k, _ in sorted(_full.items(), key=lambda kv: kv[1][0])[:50]:
            _full.pop(k, None)
    return snap


def candidates(now=None):
    """Latest big, positive headline per equity symbol inside the window."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=LOOKBACK_HOURS)
    rows = store.recent(limit=300, min_importance=store.BIG_NEWS_MIN)
    seen, out = set(), []
    for r in rows:
        if r.get("tone") != "positive":
            continue
        at = _parse_iso(r.get("created_at"))
        if at is None or at < since:
            continue
        sym = outcomes.gradeable_symbol(r.get("symbols"))
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append((sym, r))
        if len(out) >= MAX_CANDIDATES:
            break
    return out


def primary_category(r):
    for c in r.get("categories") or []:
        if c in classify.CATEGORY_LABEL:
            return c
    return None


def refresh(log=_log_default):
    now = datetime.now(timezone.utc)
    cands = candidates(now)
    gems, ran = [], []
    session = in_session(now)
    history = {}          # category -> archive numbers, one query each per pass
    def hist(cat):
        if cat not in history:
            try:
                history[cat] = store.spike_history(cat, days=HISTORY_DAYS)
            except Exception as e:
                log(f"gems: history for {cat} failed ({str(e)[:80]})")
                history[cat] = None
        return history[cat]
    for sym, r in cands:
        q = _quote(sym)
        if not q:
            continue
        price = _num(q.get("price"))
        entry, how = entry_price(sym, r["created_at"], q, r.get("grade_entry"))
        if not price or not entry:
            continue
        move = round((price - entry) / entry * 100, 2)
        status = classify_move(move, r.get("move_15m"), r.get("move_60m"))
        at = _parse_iso(r["created_at"])
        age_min = int((now - at).total_seconds() // 60) if at else None
        gem = {
            "symbol": sym, "name": q.get("name"), "price": price, "entry": entry,
            "entry_how": how, "move_pct": move, "status": status,
            "waiting_for_open": (status == "quiet" and not session),
            "day_change_pct": _num(q.get("changePercentage") or q.get("changesPercentage")),
            "volume": _num(q.get("volume")), "avg_volume": _num(q.get("avgVolume")),
            "market_cap": _num(q.get("marketCap")),
            "year_low": _num(q.get("yearLow")), "year_high": _num(q.get("yearHigh")),
            "headline": r["headline"], "url": r.get("url"), "source": r.get("source"),
            "at": r["created_at"], "age_min": age_min,
            "importance": r.get("importance"), "reasons": r.get("reasons") or [],
            "article_id": r["id"],
            "move_15m": r.get("move_15m"), "move_60m": r.get("move_60m"),
            "category": primary_category(r),
        }
        gem["category_label"] = classify.CATEGORY_LABEL.get(gem["category"], gem["category"])
        h = hist(gem["category"])
        gem["history"] = h
        gem["odds_level"], gem["odds"] = odds_label(h)
        if status == "ran":
            ran.append(gem)
            continue
        snap = _full_lookup(sym, log)
        res = snap.get("research")
        gem["value"] = value_notes(res, price)
        fl = snap.get("float") or {}
        gem["float"] = ({"shares": fl.get("float_shares") if fl.get("is_float") else fl.get("outstanding"),
                         "is_float": bool(fl.get("is_float")), "tier": fl.get("tier"),
                         "tier_note": fl.get("tier_note")} if fl else None)
        gem["offering"] = snap.get("offering")
        gem["sector"] = (res or {}).get("sector")
        rank(gem)
        gem["guard"] = guard(gem)
        gem["passes"] = not gem["guard"]
        gems.append(gem)
    gems.sort(key=lambda g: (-g["score"], -(g["importance"] or 0), g["age_min"] or 0))
    ran.sort(key=lambda g: g["age_min"] or 0)
    baseline = hist(None)
    with _lock:
        _state.update({"gems": gems, "ran": ran, "candidates": len(cands), "baseline": baseline,
                       "checked_at": now.isoformat(), "passes": _state["passes"] + 1,
                       "last_error": None, "in_session": session})
    log(f"gems: {len(cands)} candidate(s), {len(gems)} still quiet or stirring, {len(ran)} already ran")
    return len(gems)


def snapshot():
    with _lock:
        out = dict(_state)
    out.update({"lookback_hours": LOOKBACK_HOURS, "quiet_band_pct": QUIET_BAND_PCT,
                "spike_pct": store.SPIKE_PCT, "history_days": HISTORY_DAYS,
                "likely_pct": LIKELY_PCT, "unlikely_pct": UNLIKELY_PCT, "stale_minutes": STALE_MINUTES,
                "ran_pct": RAN_PCT, "refresh_seconds": REFRESH_SECONDS,
                "min_importance": store.BIG_NEWS_MIN})
    return out


def _loop(log):
    time.sleep(60)
    while True:
        try:
            refresh(log)
        except Exception as e:
            with _lock:
                _state["last_error"] = str(e)[:160]
            log(f"gems: pass failed (non-fatal): {e}")
        time.sleep(REFRESH_SECONDS)


def start(log=_log_default):
    if not os.environ.get("FMP_API_KEY"):
        log("gems: FMP_API_KEY not set -- hidden gems disabled")
        return
    _state["enabled"] = True
    threading.Thread(target=_loop, args=(log,), daemon=True, name="gems").start()


def status():
    with _lock:
        return {k: _state[k] for k in ("enabled", "passes", "checked_at", "candidates", "last_error")}
