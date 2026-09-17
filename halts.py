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

FEED_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
NS = {"ndaq": "http://www.nasdaqtrader.com/"}
MARKET_TZ = ZoneInfo("America/New_York")
TIMEOUT = 15
POLL_SECONDS = 45

# What each code actually means, and how much it should worry you. The
# severity is not a prediction of price -- it is how long you are stuck and
# how much discretion the exchange is exercising.
CODES = {
    "LUDP": ("Volatility pause", "LULD band breached — five-minute pause", "pause"),
    "LUDS": ("Volatility pause", "Quote straddled the LULD band", "pause"),
    "T1":   ("News pending", "Exchange knows news is coming; you cannot read it yet", "news"),
    "T2":   ("News released", "Dissemination has BEGUN — may not be readable yet", "news"),
    "T3":   ("News fully disseminated", "News is out and both resumption times are posted", "news"),
    "T5":   ("Single-stock pause", "Paused after a 10%+ move within five minutes", "pause"),
    "T6":   ("Extraordinary activity", "Exchange investigating unusual trading", "grave"),
    "T7":   ("Quote-only period", "Pause continues; quotes are live but nothing trades", "pause"),
    "T8":   ("ETF halt", "Component or NAV issue in an exchange-traded product", "news"),
    "T12":  ("Additional information requested", "Can run for DAYS. Frequently reopens far lower", "grave"),
    "H4":   ("Listing non-compliance", "Company fails listing requirements — delisting risk", "grave"),
    "H9":   ("Not current in filings", "Company is delinquent on its filings", "grave"),
    "H10":  ("SEC suspension", "Up to ten business days. Often terminal", "grave"),
    "H11":  ("Regulatory concern", "Halt in a related security or regulatory matter", "grave"),
    "D":    ("Delisted", "Security has been removed from the exchange", "grave"),
    "M":    ("Volatility pause", "Pause in an exchange-listed (non-Nasdaq) issue", "pause"),
    "MWC0": ("Circuit breaker carryover", "Market-wide halt carried over from the previous day", "market"),
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
# The codes that change what a position is worth, as opposed to the routine
# five-minute pauses. Nothing acts on this today -- phone alerts are switched
# off -- but it is the list a notifier would use, and it is the honest
# separation between "paused" and "in trouble".
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


def _describe(r):
    """What the feed actually sent, for the log. A parse error alone says
    nothing; this is what turns "invalid token, column 1" into a diagnosis."""
    body = r.content[:240] if isinstance(r.content, bytes) else b""
    head = body.decode("utf-8", "replace").replace("\n", " ").replace("\r", " ")
    kind = ("an HTML page" if b"<html" in body.lower() or b"<!doctype" in body.lower()
            else "an empty body" if not body else f"{len(r.content)} bytes")
    return (f"HTTP {r.status_code}, {r.headers.get('Content-Type', 'no content-type')}, "
            f"{kind}; starts: {head[:120]!r}")


def parse_feed(xml_text):
    """Halt rows out of the Nasdaq RSS document, newest first.

    Takes bytes or text. Bytes are preferred by the caller: this feed is
    served by ASP.NET, which writes a UTF-8 byte-order mark before the
    document, and a BOM at the start of a *string* is an invalid token to the
    XML parser -- the exact failure the feed produced on every poll for its
    first day in production. Given bytes, the parser reads the BOM as an
    encoding hint, which is what it is. A text BOM is stripped as well so
    neither path can fail on it.
    """
    if isinstance(xml_text, bytes):
        xml_text = xml_text.lstrip(b"\xef\xbb\xbf").lstrip()
    else:
        xml_text = xml_text.lstrip("\ufeff").lstrip()
    root = ET.fromstring(xml_text)
    # A challenge page is often well-formed XML -- "<html>...</html>" parses
    # cleanly and contains no <item>, which would read as "no halts today"
    # and be believed. Only an RSS document counts.
    if root.tag.lower() != "rss":
        raise ValueError(f"not an RSS document (root element is <{root.tag}>)")
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
            # A plain browser-style UA. The SEC's "name and email" convention
            # is the SEC's; Nasdaq's edge does not want it and an unusual UA
            # is the commonest reason a CDN serves a challenge page instead.
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/128.0 Safari/537.36 Tapehawk/1.0",
            "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.5",
            "Accept-Language": "en-US,en;q=0.8"})
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} from the halt feed -- {_describe(r)}")
        try:
            rows = parse_feed(r.content)
        except (ET.ParseError, ValueError) as e:
            # Say what came back. Without this the log reads "invalid token"
            # forever and nobody can tell a BOM from a bot-block page.
            raise RuntimeError(f"could not parse the feed ({e}) -- {_describe(r)}")
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
        elif result == "resumed":
            updated += 1
    _status["stored"] += stored
    if stored or updated:
        log(f"halts: {stored} new, {updated} resumption(s) filled in"
            + ("" if _primed[0] else " -- first pass"))
    _primed[0] = True
    # Float for today's names, a few per pass, so it is on the page by the
    # time a reader looks. Still-halted first: those are the ones being
    # decided on right now.
    try:
        import floats
        order = ([h["symbol"] for h in rows if not h.get("resumed_at")]
                 + [h["symbol"] for h in rows])
        floats.warm(order, log=log)
    except Exception as e:
        log(f"floats: warm-up skipped ({e})")
    return stored


def status():
    return dict(_status)


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
RUN_IN_MINUTES = 15               # how far back the pre-halt move is measured
# Below this the halt was not a directional move -- a news halt on a quiet
# stock, say -- and calling it "up" on a 0.4% drift would put it in a table
# whose whole purpose is to separate runners from fallers.
MIN_RUN_IN_PCT = 1.0
MAX_DIRECTION_BACKFILL = 10


def classify_direction(run_in_pct):
    if run_in_pct is None:
        return None
    if run_in_pct >= MIN_RUN_IN_PCT:
        return "up"
    if run_in_pct <= -MIN_RUN_IN_PCT:
        return "down"
    return "flat"


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
    # Halts measured before direction existed get measured again, a few per
    # pass, so the split tables draw on history instead of starting empty.
    # One bar request each; capped so it never competes with fresh grading.
    redo = store.halts_needing_direction(limit=MAX_DIRECTION_BACKFILL)
    fixed = 0
    for h in redo:
        try:
            result = measure_halt(h, outcomes)
        except Exception as e:
            log(f"halts: could not re-measure {h.get('symbol')} ({e})")
            result = None
        store.mark_halt_graded(h["halt_key"], result or {})
        fixed += 1 if result and result.get("direction") else 0
    if redo:
        log(f"halts: direction filled in for {fixed} of {len(redo)} older halt(s)")
    return done


def measure_halt(h, outcomes):
    """Returns the reopening gap, the run-in, and the follow-through, or None.

    into  = close of the bar CONTAINING the halt: the last print before it.
            Nothing trades during a halt, so that bar's close is the halt
            price itself, which is the number a trader is holding against.
    open  = open of the bar containing the resumption: the reopening print.
    pre   = close of the last bar ending before the halt -- the settled price
            before the move that caused it. Kept for the record; the gap is
            no longer measured from it, because for a volatility halt the
            move between pre and into IS the halt, and a "gap" that included
            it made every up-halt look like it gapped up at the reopen.
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
    five = timedelta(minutes=5)

    pre = None
    for dt, _o, c in bars:
        if dt + five <= t_halt:
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
        if dt + five > t_open:
            reopen, reopen_idx = o, i
            break
    # The bar containing the halt. Its close is the last print before the
    # halt -- unless the resumption fell inside the same bar, in which case
    # its close is post-reopen and cannot be used.
    into = None
    for dt, _o, c in bars:
        if dt <= t_halt < dt + five:
            if dt + five <= t_open:
                into = c
            break
    if into is None:
        into = pre
    if not into or not reopen or into <= 0:
        return None

    # Which way was it moving when it halted? The feed does not say; the
    # bars do. Measured from the last close at least RUN_IN_MINUTES before the
    # halt to the halt price. A stock halted in its first bars of the day has
    # no such close, so the session's opening print stands in -- the run-in
    # is then "since the open", which is what a trader watching it would have
    # called it too.
    ref = None
    t_ref = t_halt - timedelta(minutes=RUN_IN_MINUTES)
    for dt, _o, c in bars:
        if dt + five <= t_ref:
            ref = c
        else:
            break
    if ref is None and bars and bars[0][0] < t_halt:
        ref = bars[0][1]
    run_in = round((into - ref) / ref * 100, 2) if ref else None

    out = {"pre_price": round(pre, 4) if pre else None,
           "into_price": round(into, 4), "reopen_price": round(reopen, 4),
           "gap_pct": round((reopen - into) / into * 100, 2),
           "run_in_pct": run_in, "direction": classify_direction(run_in)}
    for mins, field in ((15, "move_15m"), (60, "move_60m")):
        target = t_open + timedelta(minutes=mins)
        val = None
        for dt, _o, c in bars[reopen_idx:]:
            if dt >= target:
                val = round((c - reopen) / reopen * 100, 2)
                break
        out[field] = val
    return out
