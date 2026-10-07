"""
The Snipe tab: get in BEFORE the halt.

A circuit-breaker halt is not an event, it is a consequence. A stock halts
to the upside because it moved more than its LULD band -- 10% for most
stocks over $3, 20% under $3, 5% for the big index names -- away from its
five-minute reference price and sat there for fifteen seconds. By the time
the wire says "halted on circuit breaker to the upside" the move has
happened and the door is shut for five minutes.

This module watches the step before that. The site's one stream (bus.py:
the press-release wires since v2 step 9; the Benzinga socket is off) is
already in this process; every headline it delivers is judged here, and the
ones that move stocks -- FDA approvals and clearances, buyouts, contract
wins, trial results, partnerships, or anything the classifier already calls
Big News with a positive tone -- become a SETUP. A piece written after the
move ("Acme shares are trading higher after...", Benzinga's why-is-it-moving
shape) never opens one: by then the move it describes has happened. From the moment the story lands the
setup's stock is polled every couple of seconds for its latest trade, and
the page shows, live: how far it has moved since the story, how fast it is
moving right now, where the halt band is, and how close it is to it. The
page beeps when a setup starts moving and again when it is near the band.
Whether to buy is the reader's call, through their own broker; this is the
clock, not the trigger finger.

Three ways a setup is seen to halt, fastest first: a halt headline on the
stream (Benzinga's "halted on circuit breaker" pieces; with that socket off
the wires send none, so this path is kept for the record and rarely fires),
the exchange's halt feed (the Halts tab's poller, authoritative, up to 45
seconds behind), and silence -- a stock that was running and has not
printed for a minute. It reopens when it prints again or the exchange says
so.

Honesty about the prices: the free Alpaca feed is IEX, one exchange, a
slice of the prints. A stock's last IEX trade can lag the market by a tick
or two on a thin name, and an illiquid stock can go quiet on IEX while it
trades elsewhere. The band price is an approximation: the real reference is
a rolling five-minute average of every trade, and the bands double in the
first fifteen and last twenty-five minutes of the day. Close enough to see
it coming; not a quote. Nothing here places an order.
"""
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone

import requests

try:
    from zoneinfo import ZoneInfo
    MARKET_TZ = ZoneInfo("America/New_York")
except Exception:                                    # pragma: no cover
    MARKET_TZ = None

SNAPSHOT_URL = "https://data.alpaca.markets/v2/stocks/snapshots"
TIMEOUT = 8
POLL_S = 2.0                   # how often the watched stocks' latest trades are read
WATCH_S = 30 * 60              # a setup is watched this long after its story
MAX_WATCH = 40                 # the snapshot request is one call for all of them
HISTORY_KEEP = 200
EVENTS_KEEP = 80
MOVE_PCT = 3.0                 # "moving": up this much since the story
NEAR_BAND_PCT = 3.0            # "near the band": within this much of the halt band
FADE_PCT = 1.0                 # a mover that falls back under this is "faded"
SILENCE_S = 60                 # no new print this long while running = probably halted
HALT_CHECK_S = 20              # how often the exchange halt table is consulted
BIG_NEWS_MIN = 5

# The kinds of headline that move a stock on their own. Same shapes the
# Halthawk backtest uses, so what the Snipe tab shows live is what the
# backtest measured. Tight on purpose.
KINDS = [
    ("fda", re.compile(r"\bFDA\b[^.]{0,80}\b(approv|clear(s|ed|ance)|grant|authoriz|accept)|\b(approv|clearance)\w*\b[^.]{0,60}\bFDA\b|"
                       r"breakthrough (therapy|device) designation|\b510\(k\)|\bCE mark|emergency use authorization", re.I)),
    ("buyout", re.compile(r"to be acquired|agree[sd]? to (be )?acqui|definitive (merger )?agreement|merger agreement|\bbuyout\b|\btakeover\b|"
                          r"all-cash|per share in cash|\bto acquire\b|\bto merge\b|going private|take-private", re.I)),
    ("contract", re.compile(r"\b(wins?|awarded|secures?|receives?|lands?|books?)\b[^.]{0,80}\b(contract|award|purchase order|order(s)? (worth|valued|for)|supply agreement)|"
                            r"\$\d[\d.,]*\s?(million|billion|M\b|B\b)[^.]{0,60}\b(contract|order|award)", re.I)),
    ("trial", re.compile(r"positive (topline|top-line|phase|interim|pivotal)|\bphase [123]\b[^.]{0,80}\b(met|meets|achiev|positive|success|statistically significant)|"
                         r"primary endpoint", re.I)),
    ("partner", re.compile(r"strategic (partnership|collaboration|cooperation|alliance)|\bpartners? with\b|collaboration agreement|licens(e|ing) agreement|"
                           r"joint venture|memorandum of understanding", re.I)),
]
EXCLUDE = re.compile(r"analyst|price target|upgrade|downgrade|\brating|initiates coverage|earnings|conference call|webcast|to present|presents? at|"
                     r"investor day|\bQ[1-4]\b|quarter|fiscal|full[- ]year|year-end|annual results|financial results|dividend|offering|pricing of|"
                     r"warrant|reverse split|compliance|delist|short report|lawsuit|class action|investigat|\bETF\b|what's going on|why .* (shares|stock)|"
                     r"movers|top (gainers|losers)|\bresum|"
                     # after-the-move pieces (v2 step 9): the move they describe has already happened, so they open nothing
                     r"shares (are|were) (trading|moving) (higher|lower)|(shares|stock) (is|are|now) (trading )?(up|down|higher|lower)\b|"
                     r"trading (higher|lower) (after|on|following|as)\b|\bWIIM\b|why (is|are) .* (up|down|moving|rising|falling|trading)|"
                     r"here'?s why|[:\-–—]\s*what you need to know", re.I)
# ... and the stock itself moving: "Acme Soars After", "Acme Shares Jump On". A business number moving in a real
# release ("Acme Revenue Jumps On New Contract Win", "Sales Surge As ...") is news, not a piece about the move.
MOVE_VERB = r"(soars?|surges?|jumps?|plunges?|tumbles?|spikes?|rall(?:y|ies)|falls?|sinks?) (?:after|on|following|as)\b"
AFTER_MOVE = re.compile(MOVE_VERB, re.I)
METRIC_MOVE = re.compile(r"\b(?:revenues?|sales|profits?|income|earnings|ebitda|margins?|volumes?|production|output|bookings|backlog|"
                         r"orders|shipments|deliveries|subscribers|users|traffic|demand|costs?|prices?|yields?) " + MOVE_VERB, re.I)
HALT_UP = re.compile(r"halt(?:ed|s)?\b.*?(?:to the )?upside|circuit breaker[^.]*?upside|halt(?:ed|s)?\b.*?\bup\s+\d", re.I)
HALT_ANY = re.compile(r"\bhalt(?:ed|s)?\b|circuit breaker", re.I)
HALT_DOWN = re.compile(r"downside|\bdown\s+\d", re.I)

_state = {"running": False, "polls": 0, "poll_errors": 0, "last_poll_at": None, "last_error": None,
          "headlines_seen": 0, "setups": 0, "watching": 0, "configured": False}
_lock = threading.Lock()
_watch = {}                    # id -> setup
_history = deque(maxlen=HISTORY_KEEP)
_events = deque(maxlen=EVENTS_KEEP)
_next_id = [1]
_last_halt_check = [0.0]
_log = print


def _creds():
    kid = os.environ.get("ALPACA_KEY_ID") or os.environ.get("APCA_API_KEY_ID")
    sec = os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("APCA_API_SECRET_KEY")
    return kid, sec


def configured():
    kid, sec = _creds()
    return bool(kid and sec)


def status():
    with _lock:
        s = dict(_state)
        s["watching"] = len(_watch)
    s["configured"] = configured()
    return s


# ---- what counts as a setup ----------------------------------------------------------

def setup_kind(headline, symbols, importance=0, tone=None, big=False):
    """The category of a headline worth watching, or None. One to three
    tickers only. A category match counts on its own; so does anything the
    classifier already calls Big News with a positive tone."""
    if not headline or not symbols or len(symbols) > 3:
        return None
    if HALT_ANY.search(headline) or EXCLUDE.search(headline):
        return None
    if AFTER_MOVE.search(headline) and not METRIC_MOVE.search(headline):
        return None
    for name, rx in KINDS:
        if rx.search(headline):
            return name
    if big and (tone or "").lower() == "positive" and (importance or 0) >= BIG_NEWS_MIN:
        return "big"
    return None


# ---- the LULD band ----------------------------------------------------------------------

def band_pct(price, when=None):
    """The LULD percentage band for a price at a time of day. Tier 2 rules
    (every stock that is not in the S&P 500 or Russell 1000 -- the names
    that halt on news almost never are). Doubled in the opening and closing
    periods. An approximation: the exchange's reference is a rolling
    five-minute average, which band_price() approximates with the prices
    seen so far."""
    if price is None or price <= 0:
        return None
    if price >= 3.0:
        pct = 10.0
    elif price >= 0.75:
        pct = 20.0
    else:
        pct = min(75.0, 0.15 / price * 100)
    if when is not None and MARKET_TZ is not None:
        d = datetime.fromtimestamp(when, timezone.utc).astimezone(MARKET_TZ)
        m = d.hour * 60 + d.minute
        if 9 * 60 + 30 <= m < 9 * 60 + 45 or 15 * 60 + 35 <= m < 16 * 60:
            pct *= 2
    return pct


# ---- setups ---------------------------------------------------------------------------

def _new_setup(symbol, article, category, now_s):
    with _lock:
        sid = _next_id[0]
        _next_id[0] += 1
    story_ts = now_s
    try:
        story_ts = datetime.fromisoformat(str(article.get("created_at")).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        pass
    return {
        "id": sid, "symbol": symbol, "headline": article.get("headline"), "url": article.get("url"),
        "category": category, "importance": article.get("importance"), "tone": article.get("tone"),
        "article_id": article.get("id"),
        "story_at": datetime.fromtimestamp(story_ts, timezone.utc).isoformat(), "story_ts": story_ts,
        "ref": None, "last": None, "last_ts": None, "high": None, "high_ts": None, "low": None,
        "pct": None, "peak_pct": None, "pct_60s": None, "band_pct": None, "band_price": None, "to_band_pct": None,
        "state": "fresh", "halted_ts": None, "halt_source": None, "reopen_ts": None, "reopen_price": None,
        "reopen_pct": None, "halts": 0, "alerts": [], "prices": deque(), "ended": None, "end_reason": None,
    }


def _public(s):
    out = {k: v for k, v in s.items() if k != "prices"}
    out["age_s"] = round(time.time() - s["story_ts"])
    return out


def _event(kind, s, text):
    e = {"at": datetime.now(timezone.utc).isoformat(), "ts": time.time(), "kind": kind, "symbol": s["symbol"],
         "setup_id": s["id"], "text": text, "pct": s.get("pct"), "last": s.get("last")}
    with _lock:
        _events.append(e)
    _log(f"snipe: {s['symbol']} {kind}: {text}")


def on_headline(article, now_s=None):
    """Every headline the socket delivers comes through here. Returns the
    setup it opened, if any. A halt headline on a watched stock marks it
    halted (up) or ends it (down)."""
    now_s = now_s or time.time()
    with _lock:
        _state["headlines_seen"] += 1
    head = article.get("headline") or ""
    syms = [str(x).upper() for x in (article.get("symbols") or []) if x and str(x).isalnum() and len(str(x)) <= 5]
    if HALT_ANY.search(head):
        for s in list(_watch.values()):
            if s["symbol"] in syms and s["state"] not in ("halted",):
                if HALT_UP.search(head) and not HALT_DOWN.search(head):
                    _mark_halted(s, now_s, "wire")
                elif HALT_DOWN.search(head):
                    _end(s, now_s, "halted to the downside")
        return None
    cat = setup_kind(head, syms, article.get("importance") or 0, article.get("tone"), bool(article.get("big")))
    if not cat:
        return None
    sym = syms[0]
    # one setup per symbol at a time: a second story on a stock already being watched is noted, not doubled
    for s in _watch.values():
        if s["symbol"] == sym and s["state"] not in ("done",):
            s["alerts"].append(f"another story: {head[:80]}")
            return None
    s = _new_setup(sym, article, cat, now_s)
    with _lock:
        if len(_watch) >= MAX_WATCH:
            oldest = min(_watch.values(), key=lambda x: x["story_ts"])
            _end(oldest, now_s, "made room for a newer setup", locked=True)
        _watch[s["id"]] = s
        _state["setups"] += 1
    _event("new", s, f"{cat}: {head[:100]}")
    return s


def _mark_halted(s, now_s, source):
    if s["state"] == "halted":
        return
    s["state"] = "halted"
    s["halted_ts"] = now_s
    s["halt_source"] = source
    s["halts"] += 1
    _event("halted", s, f"halted ({source}) at {s['last'] if s['last'] else '?'}"
           + (f", {s['pct']:+.1f}% since the story" if s.get("pct") is not None else ""))


def _end(s, now_s, reason, locked=False):
    if s.get("ended"):
        return
    s["ended"] = datetime.fromtimestamp(now_s, timezone.utc).isoformat()
    s["end_reason"] = reason
    s["state"] = "done"
    pub = _public(s)
    if locked:
        _watch.pop(s["id"], None)
        _history.appendleft(pub)
    else:
        with _lock:
            _watch.pop(s["id"], None)
            _history.appendleft(pub)


def _apply_trade(s, price, trade_ts, now_s):
    """A fresh latest-trade reading for a setup. Pure bookkeeping plus the
    state machine; returns the list of alert kinds that fired."""
    fired = []
    if price is None or price <= 0:
        return fired
    first = s["ref"] is None
    if first:
        s["ref"] = price            # the latest trade at the first poll: within seconds of the story, often before it
        s["high"], s["high_ts"], s["low"] = price, trade_ts, price
    new_print = s["last_ts"] is None or (trade_ts or 0) > s["last_ts"]
    s["last"], s["last_ts"] = price, trade_ts or now_s
    s["prices"].append((now_s, price))
    while s["prices"] and s["prices"][0][0] < now_s - 300:
        s["prices"].popleft()
    if price > (s["high"] or 0):
        s["high"], s["high_ts"] = price, trade_ts or now_s
    if s["low"] is None or price < s["low"]:
        s["low"] = price
    s["pct"] = round((price / s["ref"] - 1) * 100, 2)
    s["peak_pct"] = round((s["high"] / s["ref"] - 1) * 100, 2)
    ago = [p for t, p in s["prices"] if t <= now_s - 60]
    s["pct_60s"] = round((price / ago[-1] - 1) * 100, 2) if ago else None
    # the band: the reference is the five-minute average of what has been seen
    ref5 = sum(p for _t, p in s["prices"]) / len(s["prices"])
    bp = band_pct(price, now_s)
    s["band_pct"] = bp
    if bp:
        s["band_price"] = round(ref5 * (1 + bp / 100), 4)
        s["to_band_pct"] = round((s["band_price"] / price - 1) * 100, 2)
    # reopen after a halt: a print newer than the halt
    if s["state"] == "halted":
        if new_print and trade_ts and s["halted_ts"] and trade_ts > s["halted_ts"] + 30:
            s["state"] = "reopened"
            s["reopen_ts"], s["reopen_price"] = trade_ts, price
            s["reopen_pct"] = round((price / s["ref"] - 1) * 100, 2)
            _event("reopened", s, f"reopened at {price} ({s['reopen_pct']:+.1f}% vs the story)")
            fired.append("reopened")
        return fired
    if s["state"] == "reopened":
        return fired
    # fresh -> moving -> near; or faded
    if s["to_band_pct"] is not None and s["to_band_pct"] <= NEAR_BAND_PCT and s["pct"] >= MOVE_PCT:
        if s["state"] != "near":
            s["state"] = "near"
            _event("near", s, f"{s['pct']:+.1f}% and within {s['to_band_pct']:.1f}% of the halt band (~{s['band_price']})")
            fired.append("near")
    elif s["pct"] >= MOVE_PCT:
        if s["state"] in ("fresh", "faded"):
            s["state"] = "moving"
            _event("moving", s, f"moving: {s['pct']:+.1f}% since the story" + (f", {s['pct_60s']:+.1f}% in the last minute" if s["pct_60s"] is not None else ""))
            fired.append("moving")
    elif s["state"] in ("moving", "near") and s["pct"] < FADE_PCT:
        s["state"] = "faded"
        _event("faded", s, f"faded back to {s['pct']:+.1f}%")
    return fired


def _check_silence(s, now_s):
    """A stock that was running and has not printed for a minute has very
    likely halted; IEX is thin, so this only fires for a stock that was
    clearly moving."""
    if s["state"] in ("moving", "near") and s["last_ts"] and now_s - s["last_ts"] >= SILENCE_S and (s["pct"] or 0) >= MOVE_PCT:
        _mark_halted(s, now_s, "silence")


def _expire(now_s):
    for s in list(_watch.values()):
        if now_s - s["story_ts"] >= WATCH_S:
            _end(s, now_s, "watched for 30 minutes")


# ---- the poll -----------------------------------------------------------------------------

def fetch_snapshots(symbols):
    """Latest trade per symbol from Alpaca, one request. {SYM: (price, epoch_s)}."""
    kid, sec = _creds()
    r = requests.get(SNAPSHOT_URL, params={"symbols": ",".join(sorted(set(symbols))), "feed": "iex"},
                     headers={"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": sec}, timeout=TIMEOUT)
    r.raise_for_status()
    out = {}
    for sym, snap in (r.json() or {}).items():
        t = (snap or {}).get("latestTrade") or {}
        if t.get("p") is None:
            continue
        ts = None
        try:
            ts = datetime.fromisoformat(str(t.get("t")).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            pass
        out[sym.upper()] = (float(t["p"]), ts)
    return out


def poll_once(store, snapshots=fetch_snapshots, now_s=None):
    """One pass: read the latest trades for every watched stock, update the
    setups, consult the exchange halt table. Returns the alerts fired."""
    now_s = now_s or time.time()
    _expire(now_s)
    with _lock:
        setups = list(_watch.values())
    if not setups:
        return []
    fired = []
    try:
        snaps = snapshots([s["symbol"] for s in setups])
        with _lock:
            _state["polls"] += 1
            _state["last_poll_at"] = datetime.now(timezone.utc).isoformat()
            _state["last_error"] = None
    except Exception as e:
        with _lock:
            _state["poll_errors"] += 1
            _state["last_error"] = str(e)[:160]
        return []
    for s in setups:
        snap = snaps.get(s["symbol"])
        if snap:
            for k in _apply_trade(s, snap[0], snap[1], now_s):
                fired.append((k, s["symbol"]))
        _check_silence(s, now_s)
    # the exchange's word, up to a poll behind: authoritative on halts and reopens
    if store is not None and now_s - _last_halt_check[0] >= HALT_CHECK_S:
        _last_halt_check[0] = now_s
        try:
            for s in setups:
                for h in store.recent_halts(limit=3, symbol=s["symbol"]):
                    ht = _ts(h.get("halted_at"))
                    if not ht or ht < s["story_ts"] - 120:
                        continue
                    if s["state"] != "halted" and not h.get("resumed_at") and s["state"] not in ("reopened", "done"):
                        _mark_halted(s, ht, f"exchange {h.get('code') or ''}".strip())
                    break
        except Exception as e:
            _log(f"snipe: halt table check failed: {e}")
    return fired


def _ts(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


# ---- what the page reads ----------------------------------------------------------------

ORDER = {"near": 0, "moving": 1, "halted": 2, "reopened": 3, "fresh": 4, "faded": 5}


def snapshot():
    with _lock:
        watching = [_public(s) for s in _watch.values()]
        history = list(_history)
        events = list(_events)[-40:]
    watching.sort(key=lambda s: (ORDER.get(s["state"], 9), -(s.get("pct") or 0), s["story_ts"]))
    return {"watching": watching, "history": history[:60], "events": events, "status": status(),
            "rules": {"move_pct": MOVE_PCT, "near_band_pct": NEAR_BAND_PCT, "watch_minutes": WATCH_S // 60,
                      "silence_s": SILENCE_S},
            "now": datetime.now(timezone.utc).isoformat()}


# ---- threads ------------------------------------------------------------------------------

def _listen(stream, log):
    q = stream.subscribe()
    while True:
        try:
            article = q.get(timeout=30)
        except Exception:
            continue
        try:
            on_headline(article)
        except Exception as e:
            log(f"snipe: headline failed: {e}")


def _poll_loop(store, log):
    while True:
        try:
            poll_once(store)
        except Exception as e:
            log(f"snipe: poll failed: {e}")
        time.sleep(POLL_S)


def start(store, stream, log=print):
    """stream: anything with subscribe() -> queue (bus.py; news_stream's
    names point at it too)."""
    global _log
    _log = log
    if not configured():
        log("snipe: ALPACA_KEY_ID / ALPACA_SECRET_KEY not set -- the Snipe tab has no prices")
    with _lock:
        if _state["running"]:
            return
        _state["running"] = True
    threading.Thread(target=_listen, args=(stream, log), daemon=True, name="snipe-news").start()
    threading.Thread(target=_poll_loop, args=(store, log), daemon=True, name="snipe-poll").start()
    log("snipe: watching the wire for setups; prices every %.0fs" % POLL_S)
