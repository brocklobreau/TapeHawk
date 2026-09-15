"""
What this stock actually does on earnings day.

Every finance site prints the next earnings date. Almost none of them print
the only number a person actually wants next to it: how far this particular
stock has moved when it reported, historically, measured from the tape.

The whole module is descriptive. It says how BIG the move tends to be, never
which way -- because when this project measured direction on news it came
out at 48.6% up, which is a coin flip, and dressing a coin flip up as a
signal is the thing that makes a site look like every other site.

Two pieces of care are worth reading before trusting the numbers:

1. REACTION DAY. FMP gives a report DATE but not reliably whether the company
   reported before the open or after the close. Those put the reaction on
   different trading days, and picking the bigger of the two per event would
   inflate every statistic here by construction -- you would be taking the
   max of two draws and calling it the average. So the convention is decided
   ONCE for the company, on the aggregate of its whole history, and then
   applied to every event. One decision on pooled data is not cherry-picking;
   one decision per event is.

2. SAMPLE SIZE. Below MIN_EVENTS the aggregate convention test is itself
   noise, so the feature reports that it does not have enough history rather
   than printing a confident-looking average built on three quarters.
"""
import os
import time
from datetime import date, datetime, timedelta

import requests

BASE = "https://financialmodelingprep.com/stable"
TIMEOUT = 15
CACHE_SECONDS = 6 * 3600          # earnings history changes about four times a year
MAX_EVENTS = 16                   # ~4 years; older quarters describe a different company
MIN_EVENTS = 6                    # below this, say so instead of averaging noise
HISTORY_YEARS = 6                 # daily bars fetched once, covers MAX_EVENTS with slack

_cache = {}


class EarningsError(RuntimeError):
    pass


def _key():
    k = os.environ.get("FMP_API_KEY")
    if not k:
        raise EarningsError("FMP_API_KEY is not set")
    return k


def _get(path, params=None):
    p = dict(params or {})
    p["apikey"] = _key()
    r = requests.get(f"{BASE}/{path}", params=p, timeout=TIMEOUT)
    if r.status_code in (401, 403):
        raise EarningsError(f"FMP rejected the request ({r.status_code}) for {path}")
    if r.status_code == 402:
        return None                 # plan-restricted: treat as absent, not as an error
    if r.status_code != 200:
        raise EarningsError(f"FMP returned {r.status_code} for {path}")
    return r.json()


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # FMP occasionally returns the string "None" or an empty field rather than
    # omitting it; float() would have raised, but NaN sails straight through
    # and poisons every average downstream.
    return None if f != f else f


def _day(v):
    """FMP dates arrive as '2025-01-30' or '2025-01-30 16:30:00'."""
    s = str(v or "").strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _pick(row, *names):
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n]
    return None


# --- the tape ---------------------------------------------------------------

def _load_bars(symbol):
    """Daily bars, oldest first, with a date -> index map.

    One bulk fetch rather than a request per earnings date: sixteen events
    would otherwise be sixteen round trips, and the research panel is
    supposed to answer in about a second.
    """
    today = date.today()
    raw = _get("historical-price-eod/full", {
        "symbol": symbol,
        "from": (today - timedelta(days=365 * HISTORY_YEARS + 10)).isoformat(),
        "to": today.isoformat(),
    })
    if isinstance(raw, dict):                 # some FMP shapes wrap the list
        raw = raw.get("historical") or []
    rows = []
    for r in (raw or []):
        d = _day(r.get("date"))
        c, o = _num(r.get("close")), _num(r.get("open"))
        if d and c and o and c > 0 and o > 0:
            rows.append({"date": d, "open": o, "close": c,
                         "high": _num(r.get("high")), "low": _num(r.get("low"))})
    rows.sort(key=lambda b: b["date"])
    return rows, {b["date"]: i for i, b in enumerate(rows)}


def _session_on_or_after(report_date, bars, index):
    """Index of the first trading session on or after a report date.

    Earnings get dated on weekends and holidays often enough that skipping
    those events would quietly drop real quarters from the sample.
    """
    for offset in range(0, 6):
        i = index.get(report_date + timedelta(days=offset))
        if i is not None:
            return i
    return None


def _move(bars, i):
    """Gap, full day and intraday move for session i, against i-1's close."""
    if i is None or i < 1 or i >= len(bars):
        return None
    prev, cur = bars[i - 1], bars[i]
    base = prev["close"]
    return {
        "date": cur["date"].isoformat(),
        "gap_pct": round((cur["open"] - base) / base * 100, 2),
        "day_pct": round((cur["close"] - base) / base * 100, 2),
        "intraday_pct": round((cur["close"] - cur["open"]) / cur["open"] * 100, 2),
    }


# --- reaction-day convention ------------------------------------------------

_BEFORE_OPEN = {"bmo", "before market open", "before-market-open", "premarket", "pre-market"}
_AFTER_CLOSE = {"amc", "after market close", "after-market-close", "aftermarket", "post-market"}


def _stated_convention(events):
    """Use FMP's own timing field when it ships one -- measuring is a fallback,
    not a preference. Requires agreement across the history; a symbol that
    switched from after-close to before-open mid-sample gets measured."""
    seen = set()
    for e in events:
        t = str(e.get("time") or "").strip().lower()
        if t in _BEFORE_OPEN:
            seen.add("same")
        elif t in _AFTER_CLOSE:
            seen.add("next")
    return seen.pop() if len(seen) == 1 else None


def choose_reaction_day(same_day_moves, next_day_moves):
    """Which session carries the reaction, decided on the pooled history.

    Returns 'same' or 'next'. Deliberately compares MEANS across every event
    rather than choosing per event: per-event selection would pick the larger
    of two correlated draws sixteen times over and report the result as an
    average, which is a number that cannot be small no matter how the stock
    behaves.
    """
    def mean_abs(ms):
        vals = [abs(m["day_pct"]) for m in ms if m]
        return sum(vals) / len(vals) if vals else 0.0
    return "next" if mean_abs(next_day_moves) > mean_abs(same_day_moves) else "same"


# --- aggregation ------------------------------------------------------------

def _median(vals):
    s = sorted(vals)
    n = len(s)
    if not n:
        return None
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def summarise(events):
    """Aggregate stats over graded events. Pure -- no network, so it is testable."""
    moves = [abs(e["day_pct"]) for e in events]
    gaps = [abs(e["gap_pct"]) for e in events]
    ups = sum(1 for e in events if e["day_pct"] > 0)

    # Did the day extend the gap or give it back? Only meaningful where the
    # gap was big enough to have a direction worth continuing.
    directional = [e for e in events if abs(e["gap_pct"]) >= 1.0]
    extended = sum(1 for e in directional
                   if (e["intraday_pct"] > 0) == (e["gap_pct"] > 0))

    # The honest test of "a beat is good news": it often is not.
    beats = [e for e in events if e.get("surprise_pct") is not None and e["surprise_pct"] > 0]
    beats_that_fell = sum(1 for e in beats if e["day_pct"] < 0)

    return {
        "events": len(events),
        "avg_abs_move_pct": round(sum(moves) / len(moves), 2) if moves else None,
        "median_abs_move_pct": round(_median(moves), 2) if moves else None,
        "avg_abs_gap_pct": round(sum(gaps) / len(gaps), 2) if gaps else None,
        "biggest_up_pct": round(max((e["day_pct"] for e in events), default=0), 2) if events else None,
        "biggest_down_pct": round(min((e["day_pct"] for e in events), default=0), 2) if events else None,
        "over_5pct": sum(1 for m in moves if m > 5),
        "up_days": ups,
        "down_days": len(events) - ups,
        "gap_sample": len(directional),
        "gap_extended": extended,
        "beats": len(beats),
        "beats_that_fell": beats_that_fell,
    }


# FMP has renamed this dataset more than once across API generations, and this
# code cannot be exercised against the live API from the build environment. The
# fallback chain costs one wasted request in the worst case and is the
# difference between a working feature and a blank card if the first name has
# moved again. Field names differ between the two shapes; _pick absorbs that.
_REPORT_ENDPOINTS = ("earnings", "earnings-surprises")


def _fetch_reports(symbol):
    last_error = None
    for path in _REPORT_ENDPOINTS:
        try:
            raw = _get(path, {"symbol": symbol, "limit": 60})
        except EarningsError as e:
            last_error = e
            continue
        if isinstance(raw, dict):
            raw = raw.get("earnings") or raw.get("historical") or []
        if raw:
            return raw
    if last_error and "rejected" in str(last_error):
        raise last_error                # a bad key should say so, not look empty
    return []


def lookup(symbol):
    symbol = symbol.upper().strip()
    hit = _cache.get(symbol)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return dict(hit[1], cached=True)

    raw = _fetch_reports(symbol)
    if not raw:
        raise EarningsError(f"No earnings history published for {symbol}")

    rows = []
    for r in raw:
        d = _day(r.get("date"))
        if not d:
            continue
        rows.append({
            "date": d,
            "time": _pick(r, "time", "reportTime", "when"),
            "eps": _num(_pick(r, "epsActual", "eps", "actualEarningResult")),
            "eps_est": _num(_pick(r, "epsEstimated", "epsEstimate", "estimatedEarning")),
            "rev": _num(_pick(r, "revenueActual", "revenue")),
            "rev_est": _num(_pick(r, "revenueEstimated", "revenueEstimate")),
        })
    rows.sort(key=lambda r: r["date"])

    today = date.today()
    # A row with no reported EPS and a future date is the upcoming report.
    future = [r for r in rows if r["date"] >= today and r["eps"] is None]
    upcoming = future[0] if future else None
    past = [r for r in rows if r["date"] < today and r["eps"] is not None][-MAX_EVENTS:]

    result = {
        "ticker": symbol,
        "next": None,
        "history": [],
        "summary": None,
        "reaction_day": None,
        "note": None,
        "cached": False,
    }
    if upcoming:
        result["next"] = {
            "date": upcoming["date"].isoformat(),
            "days_away": (upcoming["date"] - today).days,
            "time": _label_time(upcoming["time"]),
            "eps_est": upcoming["eps_est"],
        }

    if len(past) < MIN_EVENTS:
        result["note"] = (f"Only {len(past)} reported quarter"
                          f"{'' if len(past) == 1 else 's'} on file — too few to "
                          "describe how this stock behaves on earnings day.")
        _store(symbol, result)
        return result

    bars, index = _load_bars(symbol)
    if len(bars) < 30:
        result["note"] = "No usable daily price history for this symbol."
        _store(symbol, result)
        return result

    same, nxt = [], []
    for r in past:
        i = _session_on_or_after(r["date"], bars, index)
        same.append(_move(bars, i))
        nxt.append(_move(bars, i + 1) if i is not None else None)

    stated = _stated_convention(past)
    which = stated or choose_reaction_day(same, nxt)
    chosen = same if which == "same" else nxt

    events = []
    for r, m in zip(past, chosen):
        if not m:
            continue
        surprise = None
        if r["eps"] is not None and r["eps_est"] not in (None, 0):
            surprise = round((r["eps"] - r["eps_est"]) / abs(r["eps_est"]) * 100, 1)
        events.append({
            "report_date": r["date"].isoformat(),
            "reaction_date": m["date"],
            "gap_pct": m["gap_pct"],
            "day_pct": m["day_pct"],
            "intraday_pct": m["intraday_pct"],
            "eps": r["eps"],
            "eps_est": r["eps_est"],
            "surprise_pct": surprise,
        })

    if len(events) < MIN_EVENTS:
        result["note"] = ("Price history doesn't reach far enough back to grade "
                          "enough of this company's reports.")
        _store(symbol, result)
        return result

    events.sort(key=lambda e: e["reaction_date"], reverse=True)
    result["history"] = events
    result["summary"] = summarise(events)
    result["reaction_day"] = {
        "which": which,
        "source": "reported" if stated else "measured",
        "label": ("the session the report is dated" if which == "same"
                  else "the session after the report date"),
    }
    _store(symbol, result)
    return result


def _label_time(t):
    t = str(t or "").strip().lower()
    if t in _BEFORE_OPEN:
        return "before the open"
    if t in _AFTER_CLOSE:
        return "after the close"
    return None


def _store(symbol, result):
    _cache[symbol] = (time.time(), result)
    if len(_cache) > 300:
        for k, _v in sorted(_cache.items(), key=lambda kv: kv[1][0])[:100]:
            _cache.pop(k, None)
