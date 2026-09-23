"""
Hidden Gems: the investing screen.

Not a trading page. This asks a different question than the rest of the
site: which companies are good businesses the market is not paying up for?
Three tests, each a plain rule of thumb a person could apply by hand, added
up into one score whose parts are always shown:

    VALUE    -- cheap for what it earns: P/E, EV/EBITDA, price-to-sales
                against growth, free-cash-flow yield.
    GROWTH   -- revenue and earnings actually growing, double digits.
    QUALITY  -- really profitable, real margins, real return on equity,
                a balance sheet that will not need an offering.

Hard gates first, because a score can be gamed by one huge number: the
company must be profitable on a trailing basis, free cash flow must be
positive, revenue must not be shrinking, and debt must be under twice equity.
Story stocks do not get in on growth alone.

"Hidden" is the small-and-mid-cap bias in the setup points, not a gate: a
$100B name that clears the bar is listed too, it just does not get the
small-cap points.

Universe: US-listed common stocks over $300M, from FMP's screener, scored one
symbol at a time at a gentle pace and written to the database, so the page
reads from disk and a redeploy does not empty it. A full pass takes an hour
or so and runs once a day after the close (and once at startup if the table
is empty or stale). Every number on the page names its source ratio, so a
reader can disagree with the rule rather than the verdict.

This is arithmetic on reported figures. It is not a recommendation and it
cannot see fraud, a pending lawsuit, or a customer walking out the door.
"""
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

import store

BASE = "https://financialmodelingprep.com/stable"
TIMEOUT = 15

MIN_MARKET_CAP = 300e6
PACE_SECONDS = 0.35            # ~3 requests/second: a full pass in about an hour
PASS_HOUR_ET = 18              # once a day, after the close
STALE_HOURS = 30               # older than this at startup: run a pass now
MAX_UNIVERSE = 4000
SMALL_CAP = 2e9
MID_CAP = 10e9
TOP_N = 60                     # what the page shows

_state = {"enabled": False, "running": False, "started_at": None, "finished_at": None,
          "universe": 0, "scored": 0, "passed": 0, "errors": 0, "last_error": None, "passes": 0}
_lock = threading.Lock()
_stop_flag = threading.Event()


def _key():
    return os.environ.get("FMP_API_KEY", "").strip()


def _get(path, params=None):
    p = dict(params or {})
    p["apikey"] = _key()
    r = requests.get(f"{BASE}/{path}", params=p, timeout=TIMEOUT)
    if r.status_code == 402:
        return None
    if r.status_code != 200:
        raise RuntimeError(f"FMP {r.status_code} for {path}")
    return r.json()


def _first(v):
    if isinstance(v, list):
        return v[0] if v else {}
    return v or {}


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _pct(v):
    """FMP mixes fractions (0.27) and percentages; anything outside the
    plausible fraction range is passed through as-is."""
    f = _num(v)
    if f is None:
        return None
    return round(f * 100, 2) if -1.5 <= f <= 1.5 else round(f, 2)


# ---- the rules ----------------------------------------------------------------

def gates(f):
    """Why this company is NOT a candidate, or None. Pure."""
    if f.get("net_margin") is None or f["net_margin"] <= 0:
        return "not profitable (trailing net margin at or under zero)"
    if f.get("fcf_yield") is not None and f["fcf_yield"] <= 0:
        return "negative free cash flow"
    if f.get("rev_growth") is not None and f["rev_growth"] < 0:
        return f"revenue shrinking ({f['rev_growth']:+.1f}%)"
    if f.get("debt_to_equity") is not None and f["debt_to_equity"] > 2:
        return f"debt is {f['debt_to_equity']:.1f}x equity"
    if f.get("pe") is not None and f["pe"] <= 0:
        return "no earnings to price"
    return None


def score(f):
    """(total, parts, notes). Each part is capped; the notes say which rule
    fired. Pure, so the page's explanation is the same arithmetic."""
    notes = []
    value = growth = quality = setup = 0
    pe, ev, ps = f.get("pe"), f.get("ev_ebitda"), f.get("ps")
    fcfy = f.get("fcf_yield")
    rg, eg = f.get("rev_growth"), f.get("eps_growth")
    nm, roe, de, cr = f.get("net_margin"), f.get("roe"), f.get("debt_to_equity"), f.get("current_ratio")

    # VALUE (max 30)
    if pe is not None and pe > 0:
        if pe < 12:
            value += 12; notes.append(f"P/E {pe:.1f} — low against the ~18-20 market average")
        elif pe < 18:
            value += 8; notes.append(f"P/E {pe:.1f} — below the market average")
        elif pe < 25 and rg is not None and rg > 15:
            value += 5; notes.append(f"P/E {pe:.1f} is fair for {rg:.0f}% growth")
    if ev is not None and 0 < ev < 10:
        value += 8; notes.append(f"EV/EBITDA {ev:.1f} — cheap on cash earnings")
    elif ev is not None and 10 <= ev < 15:
        value += 4; notes.append(f"EV/EBITDA {ev:.1f} — reasonable")
    if fcfy is not None:
        if fcfy >= 6:
            value += 10; notes.append(f"Free-cash-flow yield {fcfy:.1f}% — pays for itself fast")
        elif fcfy >= 3:
            value += 5; notes.append(f"Free-cash-flow yield {fcfy:.1f}%")
    value = min(value, 30)

    # GROWTH (max 30)
    if rg is not None:
        if rg >= 20:
            growth += 15; notes.append(f"Revenue growing {rg:.0f}% a year")
        elif rg >= 10:
            growth += 10; notes.append(f"Revenue growing {rg:.0f}%")
        elif rg >= 5:
            growth += 5; notes.append(f"Revenue growing {rg:.0f}% — steady, not fast")
    if eg is not None:
        if eg >= 20:
            growth += 15; notes.append(f"Earnings per share up {eg:.0f}%")
        elif eg >= 10:
            growth += 8; notes.append(f"Earnings per share up {eg:.0f}%")
    growth = min(growth, 30)

    # QUALITY (max 30)
    if nm is not None:
        if nm >= 15:
            quality += 10; notes.append(f"Net margin {nm:.0f}% — a genuinely profitable business")
        elif nm >= 8:
            quality += 6; notes.append(f"Net margin {nm:.0f}%")
        elif nm > 0:
            quality += 3
    if roe is not None:
        if roe >= 15:
            quality += 8; notes.append(f"Return on equity {roe:.0f}%")
        elif roe >= 10:
            quality += 4
    if de is not None:
        if de < 0.5:
            quality += 7; notes.append("Little debt")
        elif de < 1:
            quality += 4; notes.append("Debt under equity")
    if cr is not None and cr >= 1.5:
        quality += 5
    quality = min(quality, 30)

    # SETUP / HIDDEN (max 10)
    mc = f.get("market_cap") or 0
    if 0 < mc < SMALL_CAP:
        setup += 4; notes.append("Small cap — under $2B, off most radars")
    elif mc < MID_CAP:
        setup += 2
    lo, hi, px = f.get("year_low"), f.get("year_high"), f.get("price")
    if px and lo and hi and hi > lo:
        pos = (px - lo) / (hi - lo) * 100
        if pos <= 30:
            setup += 3; notes.append(f"Trading in the bottom {pos:.0f}% of its 52-week range")
    up = f.get("upside")
    if up is not None and up >= 20:
        setup += 3; notes.append(f"Analyst target {up:.0f}% above the price")
    setup = min(setup, 10)

    total = value + growth + quality + setup
    return total, {"value": value, "growth": growth, "quality": quality, "setup": setup}, notes


def verdict(total):
    if total >= 70:
        return "Strong: cheap, growing and profitable at once"
    if total >= 55:
        return "Good: two of the three tests clearly passed"
    if total >= 40:
        return "Worth a look: one clear strength"
    return "Passes the gates, little else"


# ---- data ----------------------------------------------------------------------

def universe(log=print):
    """US-listed common stocks over MIN_MARKET_CAP from the screener."""
    params = {"marketCapMoreThan": int(MIN_MARKET_CAP), "isEtf": "false", "isFund": "false",
              "isActivelyTrading": "true", "country": "US", "limit": MAX_UNIVERSE}
    rows = None
    for path in ("company-screener", "stock-screener"):
        try:
            rows = _get(path, params)
            if isinstance(rows, list):
                break
        except Exception as e:
            log(f"gems: screener {path} failed ({str(e)[:80]})")
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        sym = (r.get("symbol") or "").upper()
        if not sym or not sym.isalnum() or len(sym) > 5:
            continue                       # warrants, units, preferreds carry punctuation
        ex = (r.get("exchangeShortName") or r.get("exchange") or "").upper()
        if ex and ex not in ("NASDAQ", "NYSE", "AMEX", "NYSE ARCA"):
            continue
        out.append({"symbol": sym, "name": r.get("companyName") or sym,
                    "sector": r.get("sector"), "industry": r.get("industry"),
                    "exchange": ex or None, "market_cap": _num(r.get("marketCap")),
                    "price": _num(r.get("price"))})
    out.sort(key=lambda x: -(x["market_cap"] or 0))
    return out


def facts_for(symbol):
    """Four calls: ratios, growth, analyst target, quote. Missing pieces are
    None and the rules cope with None."""
    ratios = _first(_get("ratios", {"symbol": symbol, "limit": 1})) or {}
    time.sleep(PACE_SECONDS)
    growth = _first(_get("financial-growth", {"symbol": symbol, "limit": 1})) or {}
    time.sleep(PACE_SECONDS)
    try:
        target = _first(_get("price-target-consensus", {"symbol": symbol})) or {}
    except Exception:
        target = {}
    time.sleep(PACE_SECONDS)
    q = _first(_get("quote", {"symbol": symbol})) or {}
    time.sleep(PACE_SECONDS)
    price = _num(q.get("price"))
    fcf_ps = _num(ratios.get("freeCashFlowPerShare"))
    tgt = _num(target.get("targetConsensus") or target.get("targetMedian"))
    return {
        "price": price,
        "pe": _num(q.get("pe")) or _num(ratios.get("priceToEarningsRatio")),
        "ev_ebitda": _num(ratios.get("enterpriseValueMultiple")),
        "ps": _num(ratios.get("priceToSalesRatio")),
        "pb": _num(ratios.get("priceToBookRatio")),
        "fcf_yield": round(fcf_ps / price * 100, 2) if (fcf_ps is not None and price) else None,
        "rev_growth": _pct(growth.get("growthRevenue")),
        "eps_growth": _pct(growth.get("growthEPS") if growth.get("growthEPS") is not None else growth.get("growthNetIncome")),
        "net_margin": _pct(ratios.get("netProfitMargin")),
        "op_margin": _pct(ratios.get("operatingProfitMargin")),
        "roe": _pct(ratios.get("returnOnEquity")),
        "debt_to_equity": _num(ratios.get("debtToEquityRatio")),
        "current_ratio": _num(ratios.get("currentRatio")),
        "dividend_yield": _pct(ratios.get("dividendYield")),
        "year_low": _num(q.get("yearLow")), "year_high": _num(q.get("yearHigh")),
        "market_cap": _num(q.get("marketCap")),
        "upside": round((tgt - price) / price * 100, 1) if (tgt and price) else None,
        "analyst_target": tgt,
    }


# ---- the pass -------------------------------------------------------------------

def run_pass(log=print, limit=None):
    """Score the universe and write it down. Returns (scored, passed)."""
    if not _key():
        return 0, 0
    with _lock:
        if _state["running"]:
            return 0, 0
        _state.update(running=True, started_at=datetime.now(timezone.utc).isoformat(),
                      scored=0, passed=0, errors=0, last_error=None)
    scored = passed = errors = 0
    try:
        names = universe(log)
        if limit:
            names = names[:limit]
        with _lock:
            _state["universe"] = len(names)
        log(f"gems: scoring {len(names)} companies over ${MIN_MARKET_CAP/1e6:.0f}M")
        for i, n in enumerate(names):
            if _stop_flag.is_set():
                break
            try:
                f = facts_for(n["symbol"])
            except Exception as e:
                errors += 1
                with _lock:
                    _state["errors"] = errors
                    _state["last_error"] = f"{n['symbol']}: {str(e)[:100]}"
                if errors > 50 and errors > scored:
                    log(f"gems: too many errors ({errors}), stopping the pass: {str(e)[:100]}")
                    break
                continue
            f["market_cap"] = f.get("market_cap") or n.get("market_cap")
            f["price"] = f.get("price") or n.get("price")
            why_not = gates(f)
            if why_not:
                total, parts, notes = 0, {"value": 0, "growth": 0, "quality": 0, "setup": 0}, []
            else:
                total, parts, notes = score(f)
                passed += 1
            store.upsert_gem({"symbol": n["symbol"], "name": n["name"], "sector": n.get("sector"),
                              "industry": n.get("industry"), "exchange": n.get("exchange"),
                              "market_cap": f["market_cap"], "price": f["price"],
                              "score": total, "parts": parts, "facts": f, "notes": notes,
                              "disqualified": why_not, "verdict": None if why_not else verdict(total)})
            scored += 1
            if scored % 100 == 0:
                with _lock:
                    _state.update(scored=scored, passed=passed)
                log(f"gems: {scored}/{len(names)} scored, {passed} pass the gates")
        log(f"gems: pass done -- {scored} scored, {passed} pass the gates, {errors} errors")
    finally:
        with _lock:
            _state.update(running=False, finished_at=datetime.now(timezone.utc).isoformat(),
                          scored=scored, passed=passed, errors=errors, passes=_state["passes"] + 1)
    return scored, passed


def snapshot(limit=TOP_N, sector=None, size=None):
    rows = store.top_gems(limit=limit, sector=sector, size=size)
    with _lock:
        st = dict(_state)
    st.update({"min_market_cap": MIN_MARKET_CAP, "small_cap": SMALL_CAP, "mid_cap": MID_CAP,
               "gems": rows, "counts": store.gem_counts(), "sectors": store.gem_sectors(),
               "last_scored_at": store.gems_last_scored()})
    return st


def _seconds_until_next_pass():
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now = datetime.now(timezone.utc)
    nxt = now.replace(hour=PASS_HOUR_ET, minute=5, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return max(60, (nxt - now).total_seconds())


def _loop(log):
    time.sleep(120)
    last = store.gems_last_scored()
    stale = True
    if last:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(last)
            stale = age > timedelta(hours=STALE_HOURS)
        except ValueError:
            pass
    if stale:
        try:
            run_pass(log)
        except Exception as e:
            log(f"gems: pass failed (non-fatal): {e}")
    while not _stop_flag.is_set():
        time.sleep(_seconds_until_next_pass())
        try:
            run_pass(log)
        except Exception as e:
            log(f"gems: pass failed (non-fatal): {e}")


def start(log=print):
    if not _key():
        log("gems: FMP_API_KEY not set -- hidden gems disabled")
        return
    _state["enabled"] = True
    threading.Thread(target=_loop, args=(log,), daemon=True, name="gems").start()


def status():
    with _lock:
        return {k: _state[k] for k in ("enabled", "running", "passes", "finished_at", "universe",
                                       "scored", "passed", "errors", "last_error")}
