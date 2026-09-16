"""
Schedule 13D watcher: someone crossed 5% of a company with activist intent.

Why this filing and not the rest. Most SEC filings are routine paperwork, and
the most reliably market-moving one for a small cap is bearish (a 424B5 share
offering), so "filings are bullish" is not a thing this module believes. The
13D is the exception worth a tab of its own: it means a holder went past 5%
AND is not claiming to be passive -- they may push for board seats, a sale, a
strategy change. Since the SEC's 2023 amendments took effect the initial
deadline is 5 business days rather than 10, with amendments at 2, so the
disclosure now arrives while it still describes something current.

Sources, in order of preference, all logged so the live service tells us which
one actually answered:

  1. EDGAR's current-filings Atom feed -- disseminated in real time. Entries
     appear once for the subject company and once for the filer; the subject
     entry is the one carrying the issuer whose stock moves.
  2. The daily index under /Archives/ -- end-of-day, but a fixed published
     format that cannot quietly change shape. This is the backstop that keeps
     the tab populated if the feed above stops parsing.

Nothing here can be exercised against sec.gov from the build environment, so
every parser is written to fail soft and say what it saw. A filing that
arrives without a percentage is still a filing worth showing.

Fair access: the SEC asks for a declared User-Agent carrying a contact address
and caps everyone at 10 requests a second. Both are honoured below. Set
SEC_USER_AGENT to "Your Name your@email.com" -- requests without a contact can
be refused, and the address is yours to supply rather than something this code
should invent.
"""
import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

ATOM_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
            "&type=SC+13D&company=&dateb=&owner=include&count=100&output=atom")
DAILY_INDEX = "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{qtr}/form.{ymd}.idx"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}"

TIMEOUT = 20
POLL_SECONDS = 90
MIN_REQUEST_INTERVAL = 0.15      # ~7/s, comfortably under the SEC's cap of 10
ATOM_NS = "{http://www.w3.org/2005/Atom}"

_rate_lock = threading.Lock()
_last_request = [0.0]
_ticker_cache = {"at": 0.0, "map": {}}
_status = {"last_poll": None, "last_source": None, "last_count": 0,
           "last_error": None, "stored": 0, "polls": 0}


def user_agent():
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    return ua or "Tapehawk/1.0 (SEC_USER_AGENT not set)"


def _throttle():
    with _rate_lock:
        wait = MIN_REQUEST_INTERVAL - (time.time() - _last_request[0])
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.time()


def _get(url, as_json=False):
    _throttle()
    r = requests.get(url, timeout=TIMEOUT, headers={
        "User-Agent": user_agent(),
        "Accept-Encoding": "gzip, deflate",
    })
    r.raise_for_status()
    return r.json() if as_json else r.text


# --- CIK -> ticker ----------------------------------------------------------

def ticker_map(log=print):
    """SEC publishes the whole ticker table as one small file. Cached for a day
    because it changes about as often as companies list and delist."""
    if _ticker_cache["map"] and time.time() - _ticker_cache["at"] < 86400:
        return _ticker_cache["map"]
    try:
        raw = _get(TICKERS_URL, as_json=True)
        out = {}
        # Shipped as {"0": {...}, "1": {...}}; tolerate a plain list too.
        rows = raw.values() if isinstance(raw, dict) else raw
        for row in rows:
            cik, tic = row.get("cik_str"), row.get("ticker")
            if cik and tic:
                out[int(cik)] = str(tic).upper()
        if out:
            _ticker_cache.update(at=time.time(), map=out)
            log(f"filings: ticker table loaded ({len(out)} symbols)")
    except Exception as e:
        log(f"filings: ticker table unavailable ({e}) -- filings will show without symbols")
    return _ticker_cache["map"]


# --- source 1: the real-time Atom feed --------------------------------------

_CIK_IN_TITLE = re.compile(r"\((\d{7,10})\)")
_ACC_IN_URL = re.compile(r"(\d{10}-\d{2}-\d{6})")


def parse_atom(xml_text):
    """Pull filings out of EDGAR's current-filings feed.

    Each filing shows up twice -- once under the subject company and once
    under the filer. Only the subject entry names the issuer whose shares
    these are, so the filer copy is dropped rather than shown as a second
    filing. An entry whose role cannot be read is KEPT: losing a real 13D is
    worse than showing one whose issuer we have to fill in later.
    """
    out = []
    root = ET.fromstring(xml_text)
    for e in root.findall(f"{ATOM_NS}entry"):
        title = (e.findtext(f"{ATOM_NS}title") or "").strip()
        updated = (e.findtext(f"{ATOM_NS}updated") or "").strip()
        href = ""
        for link in e.findall(f"{ATOM_NS}link"):
            href = link.get("href") or href
        form = ""
        for cat in e.findall(f"{ATOM_NS}category"):
            if (cat.get("label") or "").lower().startswith("form"):
                form = (cat.get("term") or "").strip()
        if not form and " - " in title:
            form = title.split(" - ", 1)[0].strip()
        if not form.upper().startswith("SC 13D"):
            continue
        low = title.lower()
        if "(filer)" in low and "(subject)" not in low:
            continue
        cik = _CIK_IN_TITLE.search(title)
        acc = _ACC_IN_URL.search(href or "")
        name = title.split(" - ", 1)[1] if " - " in title else title
        name = re.sub(r"\s*\((?:\d{7,10}|Subject|Filer)\)\s*", " ", name, flags=re.I).strip()
        out.append({
            "accession": acc.group(1) if acc else None,
            "form": form.upper(),
            "issuer_cik": int(cik.group(1)) if cik else None,
            "issuer_name": name or None,
            "filed_at": updated or None,
            "url": href or None,
            "source": "atom",
        })
    return [f for f in out if f["accession"]]


# --- source 2: the end-of-day index -----------------------------------------

def parse_daily_index(text):
    """The published daily index is fixed-width text with a header block. Its
    columns are Form Type | Company Name | CIK | Date Filed | File Name.

    Parsed by splitting on runs of two or more spaces rather than by byte
    offset: the column positions have shifted before, and a parser pinned to
    them fails silently by reading the wrong field rather than loudly.
    """
    out = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("-") or "edgar/data/" not in line:
            continue
        parts = [p.strip() for p in re.split(r"\s{2,}", line.strip()) if p.strip()]
        if len(parts) < 5:
            continue
        form, name, cik, filed, path = parts[0], parts[1], parts[2], parts[3], parts[-1]
        if not form.upper().startswith("SC 13D"):
            continue
        acc = _ACC_IN_URL.search(path)
        out.append({
            "accession": acc.group(1) if acc else None,
            "form": form.upper(),
            "issuer_cik": int(cik) if cik.isdigit() else None,
            "issuer_name": name or None,
            "filed_at": filed or None,
            "url": "https://www.sec.gov/Archives/" + path.lstrip("/"),
            "source": "daily-index",
        })
    return [f for f in out if f["accession"]]


def _index_url_for(day):
    qtr = (day.month - 1) // 3 + 1
    return DAILY_INDEX.format(year=day.year, qtr=qtr, ymd=day.strftime("%Y%m%d"))


# --- enrichment: the structured 13D -----------------------------------------
# Since December 2024 these schedules are filed in a machine-readable format
# rather than as free text. The exact element names are not something this
# build environment can look up, so rather than hard-coding a guess the parser
# walks every element and matches on what the tag NAME contains. A schema that
# renames PercentOfClass to ClassPercent still parses; a hard-coded path would
# return nothing and look like "no data".

_WANTED = (
    ("percent", ("percentofclass", "classpercent", "percentclass", "aggregatepercent")),
    ("shares", ("aggregateamountbeneficiallyowned", "aggregateamount", "sharesbeneficially")),
    ("cusip", ("cusip",)),
    ("issuer_name", ("issuername", "nameofissuer", "subjectcompany")),
    ("reporting_person", ("reportingpersonname", "nameofreportingperson", "filedbyname")),
)


def _flatten(xml_text):
    vals = {}
    root = ET.fromstring(xml_text)
    for el in root.iter():
        tag = el.tag.split("}")[-1].lower()
        txt = (el.text or "").strip()
        if txt and tag not in vals:
            vals[tag] = txt
    return vals


def _number(s):
    try:
        return float(re.sub(r"[^0-9.\-]", "", str(s)))
    except (TypeError, ValueError):
        return None


def enrich(cik, accession, log=print):
    """Best effort. A 13D with no percentage is still a 13D worth showing, so
    every failure here returns {} rather than raising."""
    out = {}
    if not cik or not accession:
        return out
    base = ARCHIVE.format(cik=int(cik), acc=accession.replace("-", ""))
    try:
        listing = _get(base + "/index.json", as_json=True)
        items = (listing.get("directory") or {}).get("item") or []
        names = [i.get("name", "") for i in items]
        xmls = [n for n in names
                if n.lower().endswith(".xml") and "index" not in n.lower()
                and not n.lower().endswith("-index-headers.html")]
        if not xmls:
            return out
        vals = _flatten(_get(f"{base}/{xmls[0]}"))
        for key, needles in _WANTED:
            for tag, txt in vals.items():
                if any(n in tag for n in needles):
                    out[key] = txt
                    break
        if "percent" in out:
            out["percent"] = _number(out["percent"])
        if "shares" in out:
            out["shares"] = _number(out["shares"])
    except Exception as e:
        log(f"filings: could not read {accession} detail ({e})")
    return out


# --- polling ----------------------------------------------------------------

def collect(log=print):
    """Recent 13Ds from whichever source answers. Returns (rows, source)."""
    errors = []
    try:
        rows = parse_atom(_get(ATOM_URL))
        if rows:
            return rows, "atom"
        errors.append("atom returned no 13D entries")
    except Exception as e:
        errors.append(f"atom: {e}")

    today = datetime.now(timezone.utc).date()
    for back in range(0, 5):            # weekends and holidays have no index
        day = today - timedelta(days=back)
        try:
            rows = parse_daily_index(_get(_index_url_for(day)))
            if rows:
                return rows, f"daily-index {day.isoformat()}"
        except Exception as e:
            errors.append(f"index {day}: {e}")
    _status["last_error"] = " | ".join(errors[:3]) or None
    log("filings: no source returned 13D rows -- " + (errors[0] if errors else "unknown"))
    return [], None


def poll_once(store, log=print):
    rows, source = collect(log=log)
    _status.update(last_poll=datetime.now(timezone.utc).isoformat(),
                   last_source=source, last_count=len(rows),
                   polls=_status["polls"] + 1)
    if not rows:
        return 0
    _status["last_error"] = None
    tickers = ticker_map(log=log)
    seen_at = datetime.now(timezone.utc).isoformat()
    stored = 0
    for f in rows:
        if store.filing_exists(f["accession"]):
            continue
        # Only reach for the detail document on filings we are about to store,
        # so a quiet poll costs exactly one request.
        detail = enrich(f.get("issuer_cik"), f["accession"], log=log)
        f = dict(f)
        f["ticker"] = tickers.get(f.get("issuer_cik"))
        f["issuer_name"] = detail.get("issuer_name") or f.get("issuer_name")
        f["percent"] = detail.get("percent")
        f["shares"] = detail.get("shares")
        f["cusip"] = detail.get("cusip")
        f["reporting_person"] = detail.get("reporting_person")
        f["seen_at"] = seen_at
        f["latency_ms"] = _latency_ms(f.get("filed_at"), seen_at)
        if store.insert_filing(f):
            stored += 1
            log(f"filings: {f['form']} {f.get('ticker') or f.get('issuer_name') or '?'}"
                f" {('%.1f%%' % f['percent']) if f.get('percent') else ''}"
                f" by {f.get('reporting_person') or 'undisclosed'}")
    _status["stored"] += stored
    if stored:
        log(f"filings: stored {stored} new 13D filing(s) from {source}")
    return stored


# Anything slower than this is backfill, not pickup. On the first poll after a
# deploy the feed hands back the last hundred filings, some of them days old;
# recording those as latency would let the page advertise a four-hour "pickup
# time" that measures when the service started rather than how fast it is.
MAX_HONEST_LATENCY_MS = 15 * 60 * 1000


def _latency_ms(filed_at, seen_at):
    """How long after EDGAR disseminated it did we have it.

    Returns None rather than a number when the feed only gave a date: a
    date-only stamp would compute as many hours of 'latency' and quietly turn
    an honest measurement into a fabricated one. This project has already been
    bitten once by comparing two timestamps in different zones.
    """
    if not filed_at or len(str(filed_at)) <= 10:
        return None
    try:
        a = datetime.fromisoformat(str(filed_at).replace("Z", "+00:00"))
        if a.tzinfo is None:
            return None
        b = datetime.fromisoformat(seen_at)
        ms = int((b - a).total_seconds() * 1000)
        return ms if 0 <= ms <= MAX_HONEST_LATENCY_MS else None
    except ValueError:
        return None


def status():
    return dict(_status, user_agent_set=bool(os.environ.get("SEC_USER_AGENT", "").strip()))


_thread = None


def start(store, log=print):
    global _thread
    if _thread and _thread.is_alive():
        return
    if not os.environ.get("SEC_USER_AGENT", "").strip():
        log("filings: SEC_USER_AGENT is not set. The SEC asks for a contact "
            "address in the User-Agent and may refuse requests without one -- "
            "set it in Render to 'Your Name your@email.com'.")

    def loop():
        time.sleep(20)                  # let the news stream settle first
        while True:
            try:
                poll_once(store, log=log)
            except Exception as e:
                # The 13D tab is a reporting feature. It must never be able to
                # interrupt headline ingest, which cannot be recovered later.
                _status["last_error"] = str(e)
                log(f"filings poll failed (non-fatal): {e}")
            time.sleep(POLL_SECONDS)

    _thread = threading.Thread(target=loop, daemon=True, name="filings")
    _thread.start()
    log("filings: 13D watcher started")
