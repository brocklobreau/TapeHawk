"""
Trading halts, from the exchange's own feed.

Nasdaq publishes every halt and resumption as a small RSS document. It is the
authoritative record -- the same feed the terminals read -- and it carries
three things a news headline about a halt does not: the REASON CODE, the
resumption times, and, for a volatility pause, the band price that triggered
it.

Why this is worth its own module rather than a keyword match on the wire: the
code is the whole story. LUDP is a five-minute pause. T12 is an exchange
asking the company questions and can run for days. A headline saying "shares
halted" does not distinguish them, and this project has already shipped one
bug where a circuit-breaker halt slipped past a pattern that wanted the exact
phrase "trading halted".

TIMEZONES. The feed stamps halt and resumption times in US market time with no
offset attached -- "09/16/2026" and "08:25:00.000". Read as UTC they are four
or five hours wrong, which is exactly the error that once made this project
report FMP's news as four hours stale. Every timestamp here is localised to
America/New_York and then converted, and a row whose time cannot be parsed is
stored without one rather than given a plausible-looking wrong one.
"""
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

import notify

FEED_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
NS = {"ndaq": "http://www.nasdaqtrader.com/"}
MARKET_TZ = ZoneInfo("America/New_York")
TIMEOUT = 15
POLL_SECONDS = 45
ALERT_MAX_AGE_MINUTES = 20

# What each code actually means, and how much it should worry you. The
# severity is not a prediction of price -- it is how long you are stuck and
# how much discretion the exchange is exercising.
CODES = {
    "LUDP": ("Volatility pause", "LULD band breached — five-minute pause", "pause"),
    "LUDS": ("Volatility pause", "Quote straddled the LULD band", "pause"),
    "T1":   ("News pending", "Exchange knows news is coming; you cannot read it yet", "news"),
    "T2":   ("News released", "News is public — the halt is a reading period", "news"),
    "T3":   ("News and resumption times", "News released, resumption schedule published", "news"),
    "T5":   ("Single-stock pause", "Price volatility pause (pre-LULD style)", "pause"),
    "T6":   ("Extraordinary activity", "Exchange investigating unusual trading", "grave"),
    "T7":   ("Single-stock pause", "Correction of a transaction or quote", "pause"),
    "T8":   ("ETF halt", "Component or NAV issue in an exchange-traded product", "news"),
    "T12":  ("Additional information requested", "Can run for DAYS. Frequently reopens far lower", "grave"),
    "H4":   ("Listing non-compliance", "Company fails listing requirements — delisting risk", "grave"),
    "H9":   ("Not current in filings", "Company is delinquent on its filings", "grave"),
    "H10":  ("SEC suspension", "Up to ten business days. Often terminal", "grave"),
    "H11":  ("Regulatory concern", "Halt in a related security or regulatory matter", "grave"),
    "D":    ("Delisted", "Security has been removed from the exchange", "grave"),
    "M":    ("Market-wide halt", "Circuit breaker — nothing specific to this stock", "market"),
    "MWC1": ("Circuit breaker L1", "S&P 500 down 7% from the prior close", "market"),
    "MWC2": ("Circuit breaker L2", "S&P 500 down 13% from the prior close", "market"),
    "MWC3": ("Circuit breaker L3", "S&P 500 down 20% — trading stops for the day", "market"),
    "IPO1": ("IPO not yet trading", "New listing awaiting its opening cross", "ipo"),
    "IPOQ": ("IPO order entry", "Order entry period open for a new listing", "ipo"),
    "IPOE": ("IPO price discovery done", "New listing about to open", "ipo"),
    "C3":   ("Issuer request", "Halt at the company's own request", "news"),
    "C4":   ("Operations halt", "Exchange operational issue", "market"),
    "C9":   ("Company not current", "Issuer not current in required filings", "grave"),
    "C11":  ("Regulatory halt", "Regulatory concern in the security", "grave"),
    "R1":   ("Not registered", "Security is not registered or is suspended", "grave"),
    "R4":   ("Qualification issue", "Security does not meet exchange qualifications", "grave"),
}
# The codes worth waking someone up for. A volatility pause is routine; these
# are the ones that change what a position is worth.
ALERT_CODES = {"T12", "H4", "H9", "H10", "H11", "D", "T6"}

_status = {"last_poll": None, "last_count": 0, "stored": 0, "polls": 0,
           "last_error": None}
_primed = [False]
_thread = None


def describe(code):
    """(short label, what it implies, severity). Never invents a meaning for a
    code it does not know -- an unrecognised code is reported as unrecognised
    so the reader goes and looks it up rather than trusting a guess."""
    c = (code or "").strip().upper()
    if c in CODES:
        label, note, sev = CODES[c]
        return {"code": c, "label": label, "note": note, "severity": sev, "known": True}
    return {"code": c or "?", "label": "Unrecognised code",
            "note": "Not in this glossary — check the listing exchange before trading it",
            "severity": "unknown", "known": False}


# --- parsing ----------------------------------------------------------------

def _text(item, tag):
    el = item.find(tag, NS)
    return (el.text or "").strip() if el is not None and el.text else ""


def _et_to_utc(date_s, time_s):
    """'09/16/2026' + '08:25:00.000' in market time -> aware UTC datetime.

    Returns None rather than guessing. A halt row with no usable time is still
    worth showing; a halt row with a time that is silently five hours wrong
    poisons every measurement taken from it.
    """
    date_s, time_s = (date_s or "").strip(), (time_s or "").strip()
    if not date_s or not time_s:
        return None
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", date_s)
    t = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?", time_s)
    if not m or not t:
        return None
    try:
        naive = datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)),
                         int(t.group(1)), int(t.group(2)), int(t.group(3) or 0))
    except ValueError:
        return None
    return naive.replace(tzinfo=MARKET_TZ).astimezone(timezone.utc)


def _num(v):
    try:
        f = float(str(v).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def parse_feed(xml_text):
    """Halt rows out of the Nasdaq RSS document, newest first."""
    root = ET.fromstring(xml_text)
    out = []
    for item in root.iter("item"):
        symbol = _text(item, "ndaq:IssueSymbol").upper()
        halt_date = _text(item, "ndaq:HaltDate")
        halt_time = _text(item, "ndaq:HaltTime")
        if not symbol or not halt_date:
            continue
        code = _text(item, "ndaq:ReasonCode").upper()
        resumed = _et_to_utc(_text(item, "ndaq:ResumptionDate"),
                             _text(item, "ndaq:ResumptionTradeTime"))
        out.append({
            # A symbol can halt many times in a morning, so identity is the
            # symbol AND the exact halt time -- not the symbol alone.
            "halt_key": f"{symbol}|{halt_date}|{halt_time}",
            "symbol": symbol,
            "name": _text(item, "ndaq:IssueName") or None,
            "market": _text(item, "ndaq:Market") or None,
            "code": code or None,
            "halted_at": (lambda d: d.isoformat() if d else None)(
                _et_to_utc(halt_date, halt_time)),
            "halt_date": halt_date,
            "resumed_at": resumed.isoformat() if resumed else None,
            "quote_at": (lambda d: d.isoformat() if d else None)(
                _et_to_utc(_text(item, "ndaq:ResumptionDate"),
                           _text(item, "ndaq:ResumptionQuoteTime"))),
            "band_price": _num(_text(item, "ndaq:PauseThresholdPrice")),
        })
    out.sort(key=lambda r: str(r.get("halted_at") or ""), reverse=True)
    return out


# --- polling ----------------------------------------------------------------

def poll_once(store, log=print):
    try:
        r = requests.get(FEED_URL, timeout=TIMEOUT, headers={
            "User-Agent": os.environ.get("SEC_USER_AGENT", "").strip()
                          or "Tapehawk/1.0",
            "Accept": "application/rss+xml, application/xml, text/xml, */*"})
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} from the halt feed")
        rows = parse_feed(r.text)
    except Exception as e:
        _status["last_error"] = str(e)
        log(f"halts: feed unavailable (non-fatal) -- {e}")
        return 0

    _status.update(last_poll=datetime.now(timezone.utc).isoformat(),
                   last_count=len(rows), polls=_status["polls"] + 1,
                   last_error=None)
    stored = updated = 0
    for h in rows:
        result = store.upsert_halt(h)
        if result == "new":
            stored += 1
            d = describe(h.get("code"))
            log(f"halts: {h['symbol']} {d['code']} — {d['label']}"
                + (f" (band ${h['band_price']:.2f})" if h.get("band_price") else ""))
            # Only the codes that mean something has gone wrong, and only while
            # the halt is still current. The first poll after a restart sees a
            # whole day of halts as new and must stay silent.
            if (_primed[0] and os.environ.get("NTFY_HALTS") == "1"
                    and d["code"] in ALERT_CODES and _recent(h.get("halted_at"))):
                notify.send(f"HALT {h['symbol']}: {d['code']}",
                            f"{h.get('name') or h['symbol']} — {d['label']}. {d['note']}.",
                            key=f"halt:{h['halt_key']}", tags=["octagonal_sign"],
                            priority=4, log=log)
        elif result == "resumed":
            updated += 1
    _status["stored"] += stored
    if stored or updated:
        log(f"halts: {stored} new, {updated} resumption(s) filled in"
            + ("" if _primed[0] else " -- first pass, alerts suppressed"))
    _primed[0] = True
    return stored


def _recent(iso, minutes=ALERT_MAX_AGE_MINUTES):
    if not iso:
        return False
    try:
        t = datetime.fromisoformat(str(iso))
    except ValueError:
        return False
    if t.tzinfo is None:
        return False
    return 0 <= (datetime.now(timezone.utc) - t).total_seconds() <= minutes * 60


def status():
    return dict(_status, alerts_on=os.environ.get("NTFY_HALTS") == "1")


def start(store, log=print):
    global _thread
    if _thread and _thread.is_alive():
        return

    def loop():
        time.sleep(12)
        while True:
            try:
                poll_once(store, log=log)
            except Exception as e:
                _status["last_error"] = str(e)
                log(f"halt poll failed (non-fatal): {e}")
            time.sleep(POLL_SECONDS)

    _thread = threading.Thread(target=loop, daemon=True, name="halts")
    _thread.start()
    log("halts: Nasdaq halt feed watcher started")


# --- what the stock actually did -------------------------------------------
# The point of storing halts is the base rate: across every halt, not just the
# ones someone traded, what does the reopening print do? Measured the same way
# the headline grader measures news, so the two are comparable.

GRADE_AFTER_MINUTES = 75          # let the +60 horizon complete first
MAX_GRADE_PER_RUN = 20


def grade_pending(store, outcomes, log=print):
    """Gap at the reopen, then 15 and 60 minutes on from there."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(minutes=GRADE_AFTER_MINUTES)).isoformat()
    todo = store.halts_needing_grade(cutoff, limit=MAX_GRADE_PER_RUN)
    done = 0
    for h in todo:
        try:
            result = measure_halt(h, outcomes)
        except Exception as e:
            log(f"halts: could not grade {h.get('symbol')} ({e})")
            result = None
        # Always stamp, including on a miss. A halt that cannot be measured --
        # resumed after the close, no bars, a symbol FMP does not carry -- must
        # not be retried on every pass forever.
        store.mark_halt_graded(h["halt_key"], result or {})
        done += 1
    if done:
        log(f"halts: graded {done}")
    return done


def measure_halt(h, outcomes):
    """Returns the reopening gap and the follow-through, or None.

    pre   = close of the last bar ENDING at or before the halt
    open  = open of the first bar STARTING at or after the resumption
    The bar containing the halt is deliberately excluded from `pre`: it holds
    the spike that caused the halt, so using it would measure the move against
    a price the market never settled at.
    """
    symbol, resumed = h.get("symbol"), h.get("resumed_at")
    if not symbol or not resumed or not h.get("halted_at"):
        return None
    t_halt = outcomes.to_market_time(h["halted_at"])
    t_open = outcomes.to_market_time(resumed)
    if t_halt is None or t_open is None:
        return None
    bars = outcomes.fetch_bars(symbol, t_open.date())
    if not bars:
        return None

    pre = None
    for dt, _o, c in bars:
        if dt + timedelta(minutes=5) <= t_halt:
            pre = c
        else:
            break
    # The bar CONTAINING the resumption is the reopening bar, and its open is
    # the reopening print. This is where halts differ from headlines: a bar
    # containing a headline also holds prints from before it, so the news
    # grader has to step to the NEXT bar. During a halt there are no prints at
    # all, so the first tick in that bar is the cross itself. Stepping to the
    # next bar here would skip the reopening print and measure from five
    # minutes of post-reopen drift instead.
    reopen = None
    reopen_idx = None
    for i, (dt, o, _c) in enumerate(bars):
        if dt + timedelta(minutes=5) > t_open:
            reopen, reopen_idx = o, i
            break
    if not pre or not reopen or pre <= 0:
        return None

    out = {"pre_price": round(pre, 4), "reopen_price": round(reopen, 4),
           "gap_pct": round((reopen - pre) / pre * 100, 2)}
    for mins, field in ((15, "move_15m"), (60, "move_60m")):
        target = t_open + timedelta(minutes=mins)
        val = None
        for dt, _o, c in bars[reopen_idx:]:
            if dt >= target:
                val = round((c - reopen) / reopen * 100, 2)
                break
        out[field] = val
    return out
