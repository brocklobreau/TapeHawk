"""
Hidden Gems: the investing screen.

Not a trading page. This asks a different question than the rest of the
site: which companies are good businesses the market is not paying up for,
and has NOT already noticed? The rules are the ones from Bellwether's
undervalued screen (the part of that codebase that held up), applied to the
whole US market instead of a watchlist.

WHAT THIS CAN AND CANNOT DO -- read this before trusting the output.

Nothing here predicts that a stock is about to rise. No screener can; if a
public formula could reliably time a re-rating, the re-rating would already
have happened. What this DOES do is enforce, mechanically and every day, the
conditions that have to be true for a "before it pops" candidate to even be
possible:

  1. It is genuinely cheap on its own numbers: P/E, EV/EBITDA, free-cash-flow
     yield, price-to-book.
  2. The business underneath is actually good -- profitable, cash-generative,
     not drowning in debt, growing. This is the half that separates a bargain
     from a value trap, and quality THROTTLES cheapness rather than adding to
     it: a stock is usually that cheap for a reason, so poor quality mostly
     cancels the discount out however cheap the name looks.
  3. It has NOT already run. A stock sitting in the top third of its 52-week
     range, or up 35% in ten sessions, has already had the move. Those are
     vetoed outright rather than scored down, because a high score on an
     already-extended name is exactly the failure mode that makes a screen
     like this useless.
  4. Its own arithmetic says there is a discount to collect: a fair P/E from
     its growth and margins, blended with analyst targets, has to imply at
     least 15% upside, or it is a fine company priced about right -- not an
     undervalued one.

Cheap is the setup; a CATALYST is what makes the market look. A cheap
stock with nothing happening can sit cheap for years, so a quarter of the
score is the catalyst layer, built only from signals with evidence behind
them: earnings beats (post-earnings drift is the most robust anomaly in the
literature), analyst upgrades, a six-month trend that has started to turn
(value that is beginning to work beats value still falling), clustered
insider buying, an activist's Schedule 13D from the site's own SEC watcher,
and a shrinking share count. Headline word-counting is NOT used: Bellwether
had it, and it was noise.

The honest half is the track record: every symbol that enters the shown list
is recorded with that day's price and the S&P, then graded at one, three and
six months against the S&P over the same window, overall and split by
catalyst score. In six months that says, with numbers, whether cheap-plus-
catalyst beats cheap-alone on this list. Until then it is a hypothesis.

Universe: US-listed common stocks over $300M from FMP's screener. Two cheap
calls per name first (ratios, quote) settle most of the vetoes; only names
that survive get the dearer calls (growth, analyst target, price history,
insider filings, earnings history, analyst grades). Results are written to the database so the page reads from
disk and a redeploy does not empty it. A pass runs once a day after the close
(and at startup if the table is stale).

This is arithmetic on reported figures. It is not a recommendation and it
cannot see fraud, a pending lawsuit, or a customer walking out the door.
"""
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

import store

BASE = "https://financialmodelingprep.com/stable"
TIMEOUT = 15

MIN_MARKET_CAP = 300e6
PACE_SECONDS = 0.25            # ~4 requests/second at most
PASS_HOUR_ET = 18              # once a day, after the close
STALE_HOURS = 30               # older than this at startup: run a pass now
MAX_UNIVERSE = 4000
SMALL_CAP = 2e9
MID_CAP = 10e9
TOP_N = 20                     # what the page shows: the 20 best, nothing more
RULE_VERSION = 3               # bump when the rules change so old rows are re-scored on boot

# --- Hard vetoes (Bellwether's, with the two stricter Tapehawk gates kept) ---
# Vetoes rather than score penalties: a name failing any of these is not a
# "lower-ranked gem", it is a different thing entirely and should not appear
# on this screen at any score. The reason is stored and shown, not swallowed.
MAX_RANGE_POSITION = 65.0      # % of 52-week range; above this the move already happened
MAX_RECENT_RUN_PCT = 35.0      # 10-session gain above which this is a chase, not an entry
MAX_DEBT_TO_EQUITY = 2.0       # Bellwether allowed 5; the newer, stricter gate stays
MAX_PE = 45.0                  # above this it is not a value candidate by any definition
MIN_NET_MARGIN = 2.0           # below this "profitable" is a rounding error
MIN_RERATING_UPSIDE = 15.0     # below this there is no discount worth calling undervalued
MIN_SCORE_TO_SHOW = 58.0       # a weak gem is not a gem; show nothing rather than filler
INSIDER_DAYS = 180             # open-market purchases inside this window count
GRADES_DAYS = 90               # analyst upgrades/downgrades inside this window count
ACTIVIST_DAYS = 180            # a 13D inside this window counts
SPY = "SPY"                    # the benchmark the track record is graded against

# Weights over the five parts. Renormalised over the parts that have data.
WEIGHTS = {"cheap": 0.30, "quality": 0.20, "growth": 0.10, "timing": 0.15, "catalyst": 0.25}
# Inside the catalyst part.
CATALYST_WEIGHTS = {"earnings": 0.25, "revisions": 0.20, "trend": 0.20, "insiders": 0.15,
                    "activist": 0.10, "buyback": 0.10}

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
        return None                        # not on this plan: the rules cope with None
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


def _band(value, bands, default):
    """LOWER IS BETTER. bands: (upper_bound_exclusive, score), ascending."""
    if value is None:
        return None
    for bound, s in bands:
        if value < bound:
            return s
    return default


def _band_high(value, bands, default):
    """HIGHER IS BETTER. bands: (lower_bound_inclusive, score), descending."""
    if value is None:
        return None
    for bound, s in bands:
        if value >= bound:
            return s
    return default


def _wavg(parts):
    """parts: (weight, score) with score possibly None. None entries drop
    out and the weights renormalise over what is left."""
    live = [(w, s) for w, s in parts if s is not None]
    if not live:
        return None
    return sum(w * s for w, s in live) / sum(w for w, _ in live)


def range_position(f):
    lo, hi, px = f.get("year_low"), f.get("year_high"), f.get("price")
    if not (px and lo and hi and hi > lo):
        return None
    return round(max(0.0, min(100.0, (px - lo) / (hi - lo) * 100)), 1)


# ---- the rules ----------------------------------------------------------------

def fair_value(pe, rev_growth, net_margin, analyst_upside=None, price=None):
    """What the stock is worth versus what it costs -- the whole point of the
    screen, expressed in dollars rather than only as a percentage.

    The fair multiple is anchored at 12x (roughly where a no-growth,
    average-margin business belongs), adjusted up for real growth and real
    margins, and hard-capped at 26x so a fast grower can't be handed an
    absurd target. Blended 60/40 with analyst consensus upside when that
    exists, as a tether to what people actually modelling the company think.

    A valuation estimate, not a target and not a forecast: it says what the
    multiple implies today, with no view on whether or when the market will
    agree. Returns a dict (all keys None-safe)."""
    empty = {"upside": None, "fair_pe": None, "fair_price": None, "note": None}
    if not pe or pe <= 0:
        return empty
    fpe = 12.0
    if rev_growth is not None:
        fpe += max(-4.0, min(9.0, rev_growth * 0.35))     # +0.35x per point of growth, capped
    if net_margin is not None:
        fpe += max(-3.0, min(5.0, (net_margin - 8.0) * 0.20))
    fpe = max(9.0, min(26.0, fpe))
    implied = max(-60.0, min(150.0, (fpe / pe - 1) * 100))
    if analyst_upside is not None:
        blended = 0.6 * implied + 0.4 * analyst_upside
        note = (f"Trades at {pe:.1f}x earnings; its growth and margins justify roughly {fpe:.1f}x, "
                f"implying {implied:+.0f}% on a re-rating alone. Analyst targets imply "
                f"{analyst_upside:+.0f}%. Blended: {blended:+.0f}%.")
    else:
        blended = implied
        note = (f"Trades at {pe:.1f}x earnings; its growth and margins justify roughly {fpe:.1f}x, "
                f"implying {implied:+.0f}% if it re-rates. No analyst target to cross-check against.")
    blended = round(max(-60.0, min(150.0, blended)), 1)
    fp = round(price * (1 + blended / 100.0), 2) if price else None
    if fp is not None:
        note += f" That puts fair value near ${fp:,.2f} against ${price:,.2f} today."
    return {"upside": blended, "fair_pe": round(fpe, 1), "fair_price": fp, "note": note}


def early_vetoes(f):
    """The vetoes that ratios and a quote already settle. Pure. Returns a
    list of plain-English reasons; empty means keep going."""
    v = []
    pe, nm, de, fcfy = f.get("pe"), f.get("net_margin"), f.get("debt_to_equity"), f.get("fcf_yield")
    if pe is None:
        v.append("no usable P/E — can't establish it's cheap on earnings")
    elif pe <= 0:
        v.append("unprofitable — a low price isn't the same as undervalued")
    elif pe > MAX_PE:
        v.append(f"P/E {pe:.1f} — not undervalued by any reading")
    if nm is not None and nm < MIN_NET_MARGIN:
        v.append(f"net margin {nm:.1f}% — too thin to call this a quality business")
    if fcfy is not None and fcfy <= 0:
        v.append("negative free cash flow")
    if de is not None and de > MAX_DEBT_TO_EQUITY:
        v.append(f"debt/equity {de:.1f} — the balance sheet, not the valuation, is the story")
    rp = range_position(f)
    if rp is not None and rp > MAX_RANGE_POSITION:
        v.append(f"already at {rp:.0f}% of its 52-week range — the re-rating has largely happened")
    return v


def late_vetoes(f, fv):
    """The vetoes that need growth, price history or the fair-value arithmetic."""
    v = []
    rg, run = f.get("rev_growth"), f.get("run_10d")
    if rg is not None and rg < 0:
        v.append(f"revenue shrinking ({rg:+.1f}%)")
    if run is not None and run > MAX_RECENT_RUN_PCT:
        v.append(f"up {run:+.0f}% in the last 10 sessions — whatever the catalyst was, it already fired")
    if fv.get("upside") is not None and fv["upside"] < MIN_RERATING_UPSIDE:
        v.append(f"its own numbers imply only {fv['upside']:+.0f}% from a re-rating — priced about right")
    return v


def gates(f):
    """Every veto that applies, as one string, or None. Pure."""
    fv = fair_value(f.get("pe"), f.get("rev_growth"), f.get("net_margin"), f.get("upside"), f.get("price"))
    v = early_vetoes(f) + late_vetoes(f, fv)
    return " · ".join(v) if v else None


def catalyst_score(f):
    """0-100 or None, plus notes. Each signal is banded on its own and the
    weights renormalise over the signals that have data."""
    notes, parts = [], []
    beats, last_sur = f.get("beats_4q"), f.get("last_surprise")
    if beats is not None:
        e = {4: 95, 3: 80, 2: 55, 1: 35}.get(beats, 15)
        if last_sur is not None and last_sur < 0:
            e = max(0, e - 15)
        parts.append((CATALYST_WEIGHTS["earnings"], e))
        notes.append(f"Beat earnings estimates in {beats} of the last 4 quarters"
                     + (f"; last one {last_sur:+.0f}% vs the estimate." if last_sur is not None else "."))
    up, down = f.get("upgrades"), f.get("downgrades")
    if up is not None and down is not None and (up or down or f.get("grades_seen")):
        net = up - down
        r = 95 if net >= 2 else 78 if net == 1 else 50 if net == 0 else 30 if net == -1 else 12
        parts.append((CATALYST_WEIGHTS["revisions"], r))
        if up or down:
            notes.append(f"{up} analyst upgrade{'s' if up != 1 else ''}, {down} downgrade{'s' if down != 1 else ''} in the last {GRADES_DAYS} days.")
    r6, px, a50 = f.get("ret_6m"), f.get("price"), f.get("avg50")
    if r6 is not None:
        t = 15 if r6 < -15 else 35 if r6 < 0 else 55 if r6 < 5 else 90 if r6 < 40 else 70 if r6 < 60 else 45
        if px and a50:
            t = max(0, min(100, t + (10 if px > a50 else -10)))
        parts.append((CATALYST_WEIGHTS["trend"], t))
        notes.append(f"{r6:+.0f}% over six months — "
                     + ("still falling; cheap stocks that keep falling tend to keep being cheap." if r6 < 0
                        else "starting to work, not yet extended." if r6 < 40 else "well into its move."))
    buys, buyers = f.get("insider_buys"), f.get("insider_buyers") or 0
    if buys is not None:
        i = 95 if buyers >= 2 else 78 if buys else 40 if f.get("insider_sells") else 50
        parts.append((CATALYST_WEIGHTS["insiders"], i))
        if buys:
            notes.append(f"{buys} open-market insider purchase{'s' if buys != 1 else ''} worth "
                         f"${f.get('insider_buy_value') or 0:,.0f} in the last {INSIDER_DAYS} days — people who know "
                         f"the business are buying it at this price.")
    act = f.get("activist_13d")
    if act is not None:
        parts.append((CATALYST_WEIGHTS["activist"], 100 if act else 40))
        if act:
            who, pct = f.get("activist_who") or "an investor", f.get("activist_pct")
            notes.append(f"Schedule 13D on file: {who}" + (f" at {pct:.1f}%" if pct else "")
                         + " — someone with a stake big enough to push for a re-rating.")
    sh = f.get("shares_growth")
    if sh is not None:
        b = 95 if sh <= -3 else 75 if sh < 0 else 50 if sh <= 2 else 20
        parts.append((CATALYST_WEIGHTS["buyback"], b))
        if sh < 0:
            notes.append(f"Share count down {abs(sh):.1f}% — the company is buying itself back.")
        elif sh > 2:
            notes.append(f"Share count up {sh:.1f}% — dilution, not buybacks.")
    return _wavg(parts), notes


def score(f):
    """(total, parts, notes). Parts are each 0-100 with the weights in
    WEIGHTS; the notes say which rule fired. Pure, so the page's explanation
    is the same arithmetic."""
    notes = []
    pe, ev, fcfy, pb = f.get("pe"), f.get("ev_ebitda"), f.get("fcf_yield"), f.get("pb")
    rg, eg = f.get("rev_growth"), f.get("eps_growth")
    nm, roe, fcfm, de = f.get("net_margin"), f.get("roe"), f.get("fcf_margin"), f.get("debt_to_equity")

    # 1. Cheap -----------------------------------------------------------
    cheap = _wavg([
        (0.40, _band(pe, [(8, 100), (12, 92), (16, 80), (20, 66), (25, 50), (30, 36)], 20) if pe and pe > 0 else None),
        (0.20, _band(ev, [(6, 100), (8, 90), (10, 78), (13, 62), (16, 46), (20, 32)], 18) if ev and ev > 0 else None),
        (0.25, _band_high(fcfy, [(10, 100), (7, 90), (5, 78), (3, 60), (1.5, 42)], 25)),
        (0.15, _band(pb, [(1.0, 100), (1.5, 88), (2.5, 72), (4.0, 55), (6.0, 38)], 22) if pb and pb > 0 else None),
    ])
    if pe is not None and pe > 0:
        if pe < 12:
            notes.append(f"P/E {pe:.1f} — genuinely cheap, not just cheaper than peers.")
        elif pe < 20:
            notes.append(f"P/E {pe:.1f} — modestly valued.")
        else:
            notes.append(f"P/E {pe:.1f} — only mildly cheap; the discount here is thin.")
    if ev is not None and 0 < ev < 10:
        notes.append(f"EV/EBITDA {ev:.1f} — cheap on cash earnings.")
    if fcfy is not None and fcfy >= 5:
        notes.append(f"Free-cash-flow yield {fcfy:.1f}% — pays for itself fast.")

    # 2. Quality ---------------------------------------------------------
    quality = _wavg([
        (0.30, _band_high(nm, [(20, 100), (12, 86), (7, 70), (3, 55)], 38)),
        (0.25, _band_high(roe, [(20, 100), (12, 84), (7, 66), (3, 48)], 25)),
        (0.25, _band_high(fcfm, [(15, 100), (8, 86), (3, 68), (0, 52)], 25)),
        (0.20, _band(de, [(0.3, 100), (0.8, 86), (1.5, 68), (2.5, 50), (4.0, 32)], 18)),
    ])
    if nm is not None:
        notes.append(f"Net margin {nm:.1f}%.")
    if roe is not None:
        notes.append(f"Return on equity {roe:.1f}%.")
    if fcfm is not None:
        notes.append(f"Free cash flow margin {fcfm:.1f}% — "
                     + ("real cash generation behind the earnings." if fcfm > 3
                        else "thin cash conversion; earnings quality is the thing to check."))
    if de is not None:
        notes.append(f"Debt/equity {de:.2f}.")

    # 3. Growth ----------------------------------------------------------
    growth = _wavg([
        (0.60, _band_high(rg, [(25, 100), (12, 86), (5, 70), (0, 52)], 28)),
        (0.40, _band_high(eg, [(25, 100), (12, 84), (5, 66), (0, 50)], 30)),
    ])
    if rg is not None:
        notes.append(f"Revenue {rg:+.1f}% YoY.")
    if eg is not None:
        notes.append(f"Earnings per share {eg:+.1f}% YoY.")

    # 4. Hasn't moved yet -----------------------------------------------
    rp, run = range_position(f), f.get("run_10d")
    timing = _wavg([
        (0.5, max(0.0, min(100.0, 100.0 - rp * 1.15)) if rp is not None else None),
        (0.5, (_band(abs(run), [(5, 100), (12, 88), (20, 70), (30, 48)], 28) if run is not None else None)),
    ])
    if rp is not None:
        notes.append(f"Sitting at {rp:.0f}% of its 52-week range — "
                     + ("near the lows, the market hasn't re-rated it." if rp < 35 else "mid-range; partially re-rated already."))
    if run is not None:
        notes.append(f"{run:+.1f}% over 10 sessions — " + ("still quiet, no crowd yet." if abs(run) < 12 else "starting to move."))

    # 5. Catalyst: is anything happening? -------------------------------
    catalyst, cnotes = catalyst_score(f)
    notes.extend(cnotes)

    # Quality throttles cheapness. A weighted sum lets an extreme discount
    # drag a genuinely bad business up to a respectable score, which is
    # precisely backwards: a stock is usually that cheap FOR a reason.
    if cheap is not None:
        q = quality if quality is not None else 45.0     # unverified quality is the value-trap shape
        throttled = cheap * (0.35 + 0.65 * q / 100.0)
        if q < 70:
            notes.append(f"Cheapness counted at {throttled / cheap * 100:.0f}% — quality of {q:.0f} throttles the discount.")
        cheap = throttled

    parts = {"cheap": cheap, "quality": quality, "growth": growth, "timing": timing, "catalyst": catalyst}
    total = _wavg([(WEIGHTS[k], v) for k, v in parts.items()])
    parts = {k: (round(v) if v is not None else None) for k, v in parts.items()}
    return (round(total, 1) if total is not None else 0), parts, notes


def verdict(total):
    if total >= 75:
        return "Deep value + quality"
    if total >= 62:
        return "Undervalued, worth the work"
    if total >= MIN_SCORE_TO_SHOW:
        return "Mildly cheap"
    return "Not compelling"


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


def cheap_facts(symbol):
    """Stage one: ratios and a quote. Enough to settle most vetoes."""
    ratios = _first(_get("ratios", {"symbol": symbol, "limit": 1})) or {}
    time.sleep(PACE_SECONDS)
    q = _first(_get("quote", {"symbol": symbol})) or {}
    time.sleep(PACE_SECONDS)
    price = _num(q.get("price"))
    fcf_ps, rev_ps = _num(ratios.get("freeCashFlowPerShare")), _num(ratios.get("revenuePerShare"))
    return {
        "price": price,
        "pe": _num(q.get("pe")) or _num(ratios.get("priceToEarningsRatio")),
        "ev_ebitda": _num(ratios.get("enterpriseValueMultiple")),
        "ps": _num(ratios.get("priceToSalesRatio")),
        "pb": _num(ratios.get("priceToBookRatio")),
        "fcf_yield": round(fcf_ps / price * 100, 2) if (fcf_ps is not None and price) else None,
        "fcf_margin": round(fcf_ps / rev_ps * 100, 2) if (fcf_ps is not None and rev_ps) else None,
        "net_margin": _pct(ratios.get("netProfitMargin")),
        "op_margin": _pct(ratios.get("operatingProfitMargin")),
        "roe": _pct(ratios.get("returnOnEquity")),
        "debt_to_equity": _num(ratios.get("debtToEquityRatio")),
        "current_ratio": _num(ratios.get("currentRatio")),
        "dividend_yield": _pct(ratios.get("dividendYield")),
        "year_low": _num(q.get("yearLow")), "year_high": _num(q.get("yearHigh")),
        "avg50": _num(q.get("priceAvg50")), "avg200": _num(q.get("priceAvg200")),
        "market_cap": _num(q.get("marketCap")),
    }


def _closes(symbol, days):
    """Daily closes oldest-first over the last `days` calendar days."""
    today = datetime.now(timezone.utc).date()
    rows = _get("historical-price-eod/light", {"symbol": symbol, "from": (today - timedelta(days=days)).isoformat(),
                                               "to": today.isoformat()})
    if isinstance(rows, dict):
        rows = rows.get("historical") or []
    if not isinstance(rows, list):
        return []
    pairs = sorted(((r.get("date") or ""), _num(r.get("price") if r.get("price") is not None else r.get("close")))
                   for r in rows if r.get("date"))
    return [c for _, c in pairs if c]


def _returns(symbol, price):
    """10-session, 3-month and 6-month returns, each None when history is short."""
    closes = _closes(symbol, 200)
    last = price or (closes[-1] if closes else None)
    def back(n):
        if len(closes) <= n or not last:
            return None
        base = closes[-1 - n]
        return round((last / base - 1) * 100, 2) if base else None
    return {"run_10d": back(10), "ret_3m": back(63), "ret_6m": back(126)}


def _earnings(symbol):
    """Beats in the last four reported quarters and the latest surprise."""
    rows = _get("earnings", {"symbol": symbol, "limit": 12})
    if not isinstance(rows, list):
        return {}
    done = [r for r in rows if _num(r.get("epsActual")) is not None and _num(r.get("epsEstimated")) is not None]
    done.sort(key=lambda r: r.get("date") or "", reverse=True)
    done = done[:4]
    if not done:
        return {}
    beats = sum(1 for r in done if _num(r["epsActual"]) > _num(r["epsEstimated"]))
    a, e = _num(done[0]["epsActual"]), _num(done[0]["epsEstimated"])
    sur = round((a - e) / abs(e) * 100, 1) if e else None
    return {"beats_4q": beats, "quarters_seen": len(done), "last_surprise": sur}


def _grades(symbol):
    """Analyst upgrades and downgrades inside GRADES_DAYS."""
    rows = _get("grades", {"symbol": symbol, "limit": 40})
    if not isinstance(rows, list):
        return {}
    since = (datetime.now(timezone.utc) - timedelta(days=GRADES_DAYS)).date().isoformat()
    up = down = seen = 0
    for r in rows:
        if (r.get("date") or "")[:10] < since:
            continue
        seen += 1
        a = (r.get("action") or "").lower()
        if "upgrade" in a:
            up += 1
        elif "downgrade" in a:
            down += 1
    return {"upgrades": up, "downgrades": down, "grades_seen": seen}


def _insiders(symbol):
    """Open-market purchases and sales inside INSIDER_DAYS from Form 4s.
    Only P-Purchase and S-Sale carry a real market opinion; option
    exercises, grants, gifts and tax withholding are excluded."""
    rows = _get("insider-trading/search", {"symbol": symbol, "limit": 50})
    if not isinstance(rows, list):
        return {}
    since = (datetime.now(timezone.utc) - timedelta(days=INSIDER_DAYS)).date().isoformat()
    buys, sells, buy_value, buyers = 0, 0, 0.0, set()
    for r in rows:
        d = (r.get("transactionDate") or r.get("filingDate") or "")[:10]
        if d and d < since:
            continue
        t = r.get("transactionType") or ""
        val = (_num(r.get("price")) or 0) * (_num(r.get("securitiesTransacted")) or 0)
        if t == "P-Purchase":
            buys += 1; buy_value += val; buyers.add(r.get("reportingName"))
        elif t == "S-Sale":
            sells += 1
    return {"insider_buys": buys, "insider_sells": sells, "insider_buy_value": round(buy_value, 2),
            "insider_buyers": len([b for b in buyers if b])}


def deep_facts(symbol, f):
    """Stage two, for survivors only: growth, analyst target, ten-session
    run, insider filings. Missing pieces are None and the rules cope."""
    growth = _first(_get("financial-growth", {"symbol": symbol, "limit": 1})) or {}
    time.sleep(PACE_SECONDS)
    try:
        target = _first(_get("price-target-consensus", {"symbol": symbol})) or {}
    except Exception:
        target = {}
    time.sleep(PACE_SECONDS)
    extra = {}
    for fn in (lambda: _returns(symbol, f.get("price")), lambda: _insiders(symbol),
               lambda: _earnings(symbol), lambda: _grades(symbol)):
        try:
            extra.update(fn() or {})
        except Exception:
            pass
        time.sleep(PACE_SECONDS)
    price = f.get("price")
    tgt = _num(target.get("targetConsensus") or target.get("targetMedian"))
    f.update({
        "rev_growth": _pct(growth.get("growthRevenue")),
        "eps_growth": _pct(growth.get("growthEPS") if growth.get("growthEPS") is not None else growth.get("growthNetIncome")),
        "shares_growth": _pct(growth.get("growthWeightedAverageShsOutDil") if growth.get("growthWeightedAverageShsOutDil") is not None
                              else growth.get("growthWeightedAverageShsOut")),
        "upside": round((tgt - price) / price * 100, 1) if (tgt and price) else None,
        "analyst_target": tgt,
    })
    f.update(extra)
    try:
        f.update(store.activist_13d(symbol, ACTIVIST_DAYS))
    except Exception:
        pass
    return f


def facts_for(symbol):
    """Both stages, for callers that want everything (tests, the research panel)."""
    return deep_facts(symbol, cheap_facts(symbol))


# ---- the pass -------------------------------------------------------------------

def _record(n, f, why_not, log):
    if why_not:
        total, parts, notes, fv = 0, {}, [], {}
    else:
        total, parts, notes = score(f)
        fv = fair_value(f.get("pe"), f.get("rev_growth"), f.get("net_margin"), f.get("upside"), f.get("price"))
        if fv.get("note"):
            notes.append(fv["note"])
    store.upsert_gem({"symbol": n["symbol"], "name": n["name"], "sector": n.get("sector"),
                      "industry": n.get("industry"), "exchange": n.get("exchange"),
                      "market_cap": f.get("market_cap"), "price": f.get("price"),
                      "score": total, "parts": parts, "facts": f, "notes": notes,
                      "fair_price": fv.get("fair_price"), "upside": fv.get("upside"), "rule_version": RULE_VERSION,
                      "disqualified": why_not, "verdict": None if why_not else verdict(total)})


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
        for n in names:
            if _stop_flag.is_set():
                break
            try:
                f = cheap_facts(n["symbol"])
                f["market_cap"] = f.get("market_cap") or n.get("market_cap")
                f["price"] = f.get("price") or n.get("price")
                early = early_vetoes(f)
                if early:
                    _record(n, f, " · ".join(early), log)
                else:
                    deep_facts(n["symbol"], f)
                    fv = fair_value(f.get("pe"), f.get("rev_growth"), f.get("net_margin"), f.get("upside"), f.get("price"))
                    late = late_vetoes(f, fv)
                    _record(n, f, " · ".join(late) if late else None, log)
                    if not late:
                        passed += 1
            except Exception as e:
                errors += 1
                with _lock:
                    _state["errors"] = errors
                    _state["last_error"] = f"{n['symbol']}: {str(e)[:100]}"
                if errors > 50 and errors > scored:
                    log(f"gems: too many errors ({errors}), stopping the pass: {str(e)[:100]}")
                    break
                continue
            scored += 1
            if scored % 100 == 0:
                with _lock:
                    _state.update(scored=scored, passed=passed)
                log(f"gems: {scored}/{len(names)} scored, {passed} survive the vetoes")
        log(f"gems: pass done -- {scored} scored, {passed} survive the vetoes, {errors} errors")
        if scored and not _stop_flag.is_set():
            try:
                n = record_picks(log)
                log(f"gems: {n} new pick{'s' if n != 1 else ''} recorded for the track record")
            except Exception as e:
                log(f"gems: could not record picks (non-fatal): {e}")
    finally:
        with _lock:
            _state.update(running=False, finished_at=datetime.now(timezone.utc).isoformat(),
                          scored=scored, passed=passed, errors=errors, passes=_state["passes"] + 1)
    return scored, passed


# ---- the track record ----------------------------------------------------------

def _spy_price():
    q = _first(_get("quote", {"symbol": SPY})) or {}
    return _num(q.get("price"))


def record_picks(log=print):
    """Write today's shown list down with today's prices. Idempotent per day."""
    rows = store.top_gems(limit=TOP_N, min_score=MIN_SCORE_TO_SHOW, rule_version=RULE_VERSION)
    if not rows:
        return 0
    spy = _spy_price()
    time.sleep(PACE_SECONDS)
    return store.record_gem_picks(rows, spy)


def grade_picks(log=print):
    """Fill in returns for picks whose 1/3/6-month windows have closed."""
    if not _key():
        return 0
    due = {h: store.gem_picks_due(h) for h in store.HORIZONS}
    if not any(due.values()):
        return 0
    spy_now = _spy_price()
    time.sleep(PACE_SECONDS)
    if not spy_now:
        return 0
    graded, quotes = 0, {}
    for h, picks in due.items():
        for p in picks:
            sym = p["symbol"]
            if sym not in quotes:
                try:
                    quotes[sym] = _num((_first(_get("quote", {"symbol": sym})) or {}).get("price"))
                except Exception:
                    quotes[sym] = None
                time.sleep(PACE_SECONDS)
            px = quotes[sym]
            if not px or not p.get("price"):
                continue
            ret = round((px / p["price"] - 1) * 100, 2)
            spy_ret = round((spy_now / p["spy"] - 1) * 100, 2) if p.get("spy") else None
            store.grade_gem_pick(p["id"], h, ret, spy_ret)
            graded += 1
    if graded:
        log(f"gems: graded {graded} pick-horizon{'s' if graded != 1 else ''} against {SPY}")
    return graded


def snapshot(limit=TOP_N, sector=None, size=None):
    rows = store.top_gems(limit=limit, sector=sector, size=size, min_score=MIN_SCORE_TO_SHOW, rule_version=RULE_VERSION)
    with _lock:
        st = dict(_state)
    st.update({"min_market_cap": MIN_MARKET_CAP, "small_cap": SMALL_CAP, "mid_cap": MID_CAP,
               "min_score": MIN_SCORE_TO_SHOW, "weights": WEIGHTS, "rule_version": RULE_VERSION,
               "gems": rows, "counts": store.gem_counts(min_score=MIN_SCORE_TO_SHOW, rule_version=RULE_VERSION),
               "sectors": store.gem_sectors(min_score=MIN_SCORE_TO_SHOW, rule_version=RULE_VERSION),
               "last_scored_at": store.gems_last_scored(),
               "track": store.gem_track_summary(), "picks": store.gem_picks_recent(60)})
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
    try:
        grade_picks(log)
    except Exception as e:
        log(f"gems: grading failed (non-fatal): {e}")
    if stale or store.gems_rule_version() != RULE_VERSION:
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
        try:
            grade_picks(log)
        except Exception as e:
            log(f"gems: grading failed (non-fatal): {e}")


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
