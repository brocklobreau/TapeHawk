"""
What actually happened after the headline.

Every news site tells you the news. This measures what the stock did next,
using the archive this service already keeps: each headline is stored with a
precise timestamp and its tickers, and FMP serves 5-minute bars going back
years. So for any headline received an hour ago, the answer is retrievable.

Two things fall out of that, and both are unusual for a news product:

  * THE SITE GRADES ITSELF. "Likely a big mover" stops being an assertion and
    becomes a measured claim with a hit rate attached -- including the times
    it was wrong. Publishing that is the fastest way to be trusted by people
    who assume everything is marketing.
  * PRIORS FROM YOUR OWN DATA. Once enough headlines are graded, "FDA
    approval" carries what the last N approvals in THIS feed actually did,
    rather than my general impression of what such news usually does.

THE TIMEZONE TRAP, because it has already bitten this project once.
Alpaca stamps headlines in UTC. FMP stamps bars in US Eastern, with no
offset in the string. Comparing them directly is a four-hour error that
silently grades every headline against the wrong bars -- and would not look
like a bug, just like noise. Every comparison below converts explicitly.
"""
import os
from datetime import datetime, timedelta, timezone

import requests

try:
    from zoneinfo import ZoneInfo
    MARKET_TZ = ZoneInfo("America/New_York")
except Exception:                                    # pragma: no cover
    MARKET_TZ = None

BASE = "https://financialmodelingprep.com/stable"
TIMEOUT = 20

# Wait this long before grading: a 60-minute horizon needs 60 minutes of bars
# to exist, plus slack for the data to land.
GRADE_AFTER_MINUTES = 90
HORIZONS = (15, 60)
BAR_MIN = 5
MAX_PER_RUN = 25          # keep a single pass cheap against the API budget

# Symbols the bar endpoint will not usefully answer for.
_SKIP_SUFFIXES = ("USD", "USDT", "BTC", "ETH")


def _key():
    k = os.environ.get("FMP_API_KEY")
    if not k:
        raise RuntimeError("FMP_API_KEY is not set")
    return k


def gradeable_symbol(symbols):
    """First symbol worth asking about. Crypto pairs and index-ish tickers
    come through the news feed but are not equities the bar endpoint covers
    the same way, so they are skipped rather than silently mis-graded."""
    for s in symbols or []:
        s = (s or "").strip().upper()
        if not s or len(s) > 6 or not s.isalnum():
            continue
        if any(s.endswith(x) for x in _SKIP_SUFFIXES):
            continue
        return s
    return None


def to_market_time(iso_utc):
    """Headline timestamp -> naive US Eastern, matching how FMP stamps bars."""
    if not iso_utc:
        return None
    try:
        dt = datetime.fromisoformat(str(iso_utc).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if MARKET_TZ is None:                            # pragma: no cover
        return (dt.astimezone(timezone.utc) - timedelta(hours=4)).replace(tzinfo=None)
    return dt.astimezone(MARKET_TZ).replace(tzinfo=None)


def _parse_bar_dt(v):
    try:
        return datetime.strptime(str(v)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def fetch_bars(symbol, day):
    """5-minute bars for one session, oldest first."""
    r = requests.get(f"{BASE}/historical-chart/{BAR_MIN}min",
                     params={"symbol": symbol, "from": day.isoformat(),
                             "to": day.isoformat(), "apikey": _key()},
                     timeout=TIMEOUT)
    if r.status_code != 200:
        return []
    rows = r.json()
    out = []
    for b in rows if isinstance(rows, list) else []:
        dt = _parse_bar_dt(b.get("date"))
        if dt is None:
            continue
        try:
            out.append((dt, float(b["open"]), float(b["close"])))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda x: x[0])
    return out


def measure(symbol, headline_time_utc):
    """Move over each horizon, entered at the first bar AT OR AFTER the
    headline. Entry is that bar's OPEN, not its close: the headline landed
    inside that bar, so its close already contains part of the reaction and
    using it would hand the measurement a head start.

    Returns None when the headline sits outside a session -- pre-market,
    overnight, weekend. That is a real and common case (a third of this feed
    arrives outside market hours) and reporting it as a zero move would be a
    lie the aggregates then average in."""
    t = to_market_time(headline_time_utc)
    if t is None:
        return None
    bars = fetch_bars(symbol, t.date())
    if not bars:
        return None
    idx = None
    for i, (dt, _o, _c) in enumerate(bars):
        if dt >= t:
            idx = i
            break
    if idx is None:
        return None                                  # after the close
    entry = bars[idx][1]
    if not entry:
        return None
    out = {"entry": round(entry, 4), "entry_at": bars[idx][0].isoformat(),
           "bars": len(bars)}
    for h in HORIZONS:
        j = idx + h // BAR_MIN
        out[f"move_{h}m"] = (round((bars[j][2] - entry) / entry * 100, 3)
                             if j < len(bars) else None)
    return out


def grade_pending(store, log=print, limit=MAX_PER_RUN):
    """Grade headlines old enough to have an answer. Cheap and incremental:
    one API call per headline, capped per run, and each row is marked so it is
    never graded twice."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(minutes=GRADE_AFTER_MINUTES)).isoformat()
    rows = store.ungraded(cutoff, limit=limit)
    if not rows:
        return {"graded": 0, "skipped": 0}
    graded = skipped = 0
    for r in rows:
        sym = gradeable_symbol(r["symbols"])
        if not sym:
            store.mark_graded(r["id"], None, None, None, None, reason="no equity ticker")
            skipped += 1
            continue
        try:
            m = measure(sym, r["created_at"])
        except Exception as e:
            log(f"grade {sym}: {str(e)[:90]}")
            continue
        if not m:
            store.mark_graded(r["id"], sym, None, None, None, reason="outside market hours")
            skipped += 1
            continue
        store.mark_graded(r["id"], sym, m.get("move_15m"), m.get("move_60m"),
                          m.get("entry"))
        graded += 1
    if graded or skipped:
        log(f"outcomes: graded {graded}, skipped {skipped} "
            f"(no ticker or outside a session)")
    return {"graded": graded, "skipped": skipped}
