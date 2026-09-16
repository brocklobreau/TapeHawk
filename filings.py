"""
EDGAR watcher: activist stakes (Schedule 13D) and the 8-Ks that matter.

Most SEC filings are routine paperwork, and the most reliably market-moving
one for a small cap is bearish -- a share offering -- so "filings are bullish"
is not a thing this module believes. Two things are worth surfacing:

  * Schedule 13D. A holder went past 5% AND is not claiming to be passive:
    they may push for board seats, a sale, a strategy change. Since the SEC's
    2023 amendments took effect the initial deadline is 5 business days rather
    than 10, with amendments at 2, so it now describes something current.

  * A small subset of Form 8-K. The form is only a container; the ITEM NUMBER
    says what happened, and the items divide cleanly into bad news, good news,
    and three that need the document read because the number is ambiguous.
    Direction here is what the filing IS, structurally -- not a prediction.
    That distinction matters: this project measured headline SENTIMENT against
    subsequent price and found no directional edge (48.6% up, a coin flip). A
    non-reliance filing being bad news is not a sentiment guess.

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
from zoneinfo import ZoneInfo

import requests

ATOM_URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
            "&type={form}&company=&dateb=&owner=include&count=100&output=atom")
DAILY_INDEX = "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{qtr}/form.{ymd}.idx"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}"
# EDGAR stamps acceptance times in Eastern time with no offset in the string.
# Attached explicitly so a stamp never gets read as UTC (four hours out, and
# after 8pm ET, a different DAY).
EDGAR_TZ = ZoneInfo("America/New_York")
# How many date-only rows already in the database get their exact time filled
# in per poll. Small on purpose: a burst of Archives requests is what drew
# 403s in production, and the backlog only ever has to drain once.
MAX_TIME_BACKFILL = 12

TIMEOUT = 20
POLL_SECONDS = 90
MIN_REQUEST_INTERVAL = 0.15      # ~7/s, comfortably under the SEC's cap of 10
ATOM_NS = "{http://www.w3.org/2005/Atom}"

# Nothing older than this is ingested, from ANY source.
#
# This exists because of a real failure: the full-text-search endpoint ignores
# every date parameter it is given -- every documented spelling returns zero
# hits -- so it answers with an arbitrary slice of its index. For 8-K that
# slice happens to be today's filings. For SC 13D it was a hundred filings
# from DECEMBER, which the page then showed as "recent 13D filings", sorted
# newest-first so they looked entirely current.
#
# A stale row presented as current is worse than a missing row: one is a gap
# you can see, the other is a lie you act on. So the age check sits in
# collect(), where every source passes through, rather than in any one parser
# -- and a source that can only offer stale rows now yields NOTHING and lets
# the chain fall through to the next one.
MAX_FEED_AGE_DAYS = 7

# --- which 8-Ks are worth anyone's attention --------------------------------
# Hundreds of 8-Ks are filed every day and almost all of them are procedural.
# An 8-K feed carrying all of them is a feed nobody reads, and the genuinely
# bad news is exactly what gets buried when a company files at 4:05pm.
#
# These four are bad news from the item number alone -- no reading required.
# 5.02 is NOT here despite being bad news roughly half the time: it is in
# BODY_ITEMS below, because the same number covers a CFO resigning and a
# company hiring one, and deciding from the number would be wrong constantly.
RED_FLAG_ITEMS = {"1.03", "3.01", "4.01", "4.02"}

# Good news has its own items. These two are rare and material whichever way
# you read them, and for the company being bought they are the most bullish
# filing there is -- so they go in without needing the document read.
TAILWIND_ITEMS = {"2.01", "5.01"}

# These three cannot be judged from the item number alone and the document has
# to be read:
#   5.02 covers a CFO resigning AND a company hiring a well-regarded new one.
#        Labelling every 5.02 a departure is wrong about half the time.
#   8.01 is the catch-all. An FDA approval and a change of registered agent
#        arrive under the same number, so an unfiltered 8.01 feed is noise.
#   1.01 is a material agreement -- a large customer contract, or an
#        at-the-market offering that dilutes holders. Opposite directions,
#        same item.
BODY_ITEMS = {"5.02", "8.01", "1.01"}

# The complete Form 8-K item list, used to tell an item code apart from any
# other number-dot-number in the same line. Without it "Size: 45 KB" and a
# share price both parse as items.
ALL_8K_ITEMS = {
    "1.01", "1.02", "1.03", "1.04", "1.05",
    "2.01", "2.02", "2.03", "2.04", "2.05", "2.06",
    "3.01", "3.02", "3.03",
    "4.01", "4.02",
    "5.01", "5.02", "5.03", "5.04", "5.05", "5.06", "5.07", "5.08",
    "6.01", "6.02", "6.03", "6.04", "6.05", "6.06",
    "7.01", "8.01", "9.01",
}
ITEM_LABELS = {
    "1.01": "Material agreement",
    "1.03": "Bankruptcy or receivership",
    "2.01": "Acquisition or disposition completed",
    "3.01": "Delisting notice or listing-rule failure",
    "4.01": "Auditor changed",
    "4.02": "Financials can no longer be relied on",
    "5.01": "Change of control",
    "5.02": "Executive or director change",
    "8.01": "Other material event",
}
# A burst of 8-Ks lands right after the close. The per-filing header fetch is
# capped so one busy minute cannot eat the request budget; anything skipped is
# still in the feed on the next pass ninety seconds later.
MAX_HEADER_FETCH = 150
# Reading a filing's body is heavier than reading its header, so it gets its
# own smaller budget. Anything deferred is picked up on the next pass.
MAX_BODY_FETCH = 60
# Only the front of the document is read. The judgement is made on the opening
# narrative; the rest is exhibits, and an 8-K with fifty pages of appendices
# should not cost fifty pages of transfer.
MAX_BODY_BYTES = 220_000

_rate_lock = threading.Lock()
_last_request = [0.0]
_ticker_cache = {"at": 0.0, "map": {}}
# The first poll after a restart sees the whole recent feed as "new". Tracked
# so the log can say which pass was the priming one, and so that anything
# reacting to new filings later (a notifier, a webhook) has the hook it needs
# to stay quiet on the burst a restart produces.
_primed = [False]
# A filing that has been sitting in the feed for a day is not news, whatever
# the database thinks. Used by _fresh_enough below.
ALERT_MAX_AGE_HOURS = 8
_status = {"last_poll": None, "last_source": None, "last_count": 0,
           "last_error": None, "stored": 0, "polls": 0,
           "last_8k_poll": None, "last_8k_source": None, "last_8k_count": 0,
           "stored_8k": 0}


def user_agent():
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    return ua or "Tapehawk/1.0 (SEC_USER_AGENT not set)"


def _throttle():
    with _rate_lock:
        wait = MIN_REQUEST_INTERVAL - (time.time() - _last_request[0])
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.time()


class SecError(RuntimeError):
    """A refusal or failure from sec.gov, carrying the reason it gave.

    Defined here because it is raised here. An earlier edit renamed a class
    that lives in a DIFFERENT module, so this name was referenced and never
    created -- every non-200 from sec.gov raised NameError instead of the
    intended error, and the fallback chain reported 'NameError' where it
    should have reported what the SEC actually said. The tests never caught it
    because none of them exercise an HTTP failure path; the one below now does.
    """


def _describe(resp):
    """A refusal with its reason attached.

    sec.gov answers a blocked request with a 403 whose BODY says why -- an
    undeclared user agent, or a rate threshold. Without the body all three
    look identical in the logs, which is exactly the hole that made the first
    live failure unreadable.
    """
    try:
        body = " ".join((resp.text or "")[:400].split())
    except Exception:
        body = ""
    return f"{resp.status_code} for {resp.url}" + (f" -- said: {body}" if body else "")


def _get(url, as_json=False, max_bytes=None, _retry=True):
    _throttle()
    if max_bytes:
        # Streamed and truncated: an 8-K with a fifty-page exhibit would
        # otherwise be pulled in full to read its first two paragraphs.
        with requests.get(url, timeout=TIMEOUT, stream=True, headers={
                "User-Agent": user_agent()}) as r:
            if r.status_code >= 400:
                raise SecError(_describe(r))
            buf = b""
            for chunk in r.iter_content(16384):
                buf += chunk
                if len(buf) >= max_bytes:
                    break
        return buf.decode("utf-8", "replace")
    r = requests.get(url, timeout=TIMEOUT, headers={
        "User-Agent": user_agent(),
        "Accept-Encoding": "gzip, deflate",
        # sec.gov is stricter about Archives paths than about /files. Sending
        # a browser-shaped Accept costs nothing and removes one variable.
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    if r.status_code in (403, 429) and _retry:
        # Observed in production: a burst of Archives requests draws a 403
        # while the same URL succeeds moments later. One backed-off retry
        # turns an intermittent refusal into a non-event.
        time.sleep(2.0)
        return _get(url, as_json=as_json, max_bytes=max_bytes, _retry=False)
    if r.status_code >= 400:
        raise SecError(_describe(r))
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


def parse_atom(xml_text, want="SC 13D", subject_only=True):
    """Pull filings of one form type out of EDGAR's current-filings feed.

    A 13D concerns two parties: the investor who filed it and the company whose
    shares they bought. EDGAR may list the filing under either or both, and it
    is the SUBJECT entry that names the company whose stock moves -- so when
    both copies are present the subject wins and the filer copy is dropped as a
    duplicate.

    What it must NOT do is discard a filing that appears only under the filer.
    An earlier version did, and in production that filtered out every single
    row: the feed reported entries, the parser returned none, and the tab
    looked broken for a reason no log could explain. A filer-only row is kept,
    marked, and its issuer filled in later from the filing's own document.

    `subject_only` now only controls PREFERENCE, never exclusion. It stays a
    parameter because an 8-K has no subject/filer split at all.
    """
    by_accession, order = {}, []
    root = ET.fromstring(xml_text)
    seen = 0
    for e in root.findall(f"{ATOM_NS}entry"):
        seen += 1
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
        if not _is_form(form, want):
            continue
        low = title.lower()
        role = ("subject" if "(subject)" in low
                else "filer" if "(filer)" in low else "unknown")
        cik = _CIK_IN_TITLE.search(title)
        acc = _ACC_IN_URL.search(href or "")
        if not acc:
            continue
        name = title.split(" - ", 1)[1] if " - " in title else title
        name = re.sub(r"\s*\((?:\d{7,10}|Subject|Filer)\)\s*", " ", name, flags=re.I).strip()
        row = {
            "accession": acc.group(1),
            "form": form.upper(),
            "issuer_cik": int(cik.group(1)) if cik else None,
            "issuer_name": name or None,
            "issuer_confirmed": role == "subject" or not subject_only,
            "role": role,
            "filed_at": updated or None,
            "url": href or None,
            "source": "atom",
            # Some feeds put the 8-K items in the entry text. When they do it
            # saves a request per filing; when they don't the header fetch
            # fills it in.
            "items": parse_items(title + " " + (e.findtext(f"{ATOM_NS}summary") or "")),
        }
        prev = by_accession.get(row["accession"])
        if prev is None:
            by_accession[row["accession"]] = row
            order.append(row["accession"])
        elif subject_only and prev["role"] != "subject" and role == "subject":
            by_accession[row["accession"]] = row       # the better copy wins
    rows = [by_accession[a] for a in order]
    _status["last_atom_entries"] = seen
    _status["last_atom_kept"] = len(rows)
    return rows


def _is_form(form, want):
    """8-K must not swallow 8-K/A's sibling forms like 8-K12B, and 'SC 13D'
    must still match 'SC 13D/A'. Exact match or an amendment suffix only."""
    f, w = (form or "").upper().strip(), want.upper()
    return f == w or f.startswith(w + "/")


_ITEM_RE = re.compile(r"\b(\d{1,2}\.\d{2})\b")

# The SGML header names items by TITLE, not number:
#     ITEM INFORMATION:        Other Events
#     ITEM INFORMATION:        Financial Statements and Exhibits
# A parser that only knows the numbers reads that header as "no items", and
# the caller then dismisses the filing as routine -- permanently, since
# dismissed accessions are never re-read. Titles are matched on a distinctive
# phrase each, after punctuation is stripped, and ONLY on ITEM INFORMATION
# lines so that the words "other events" in a document body never become an
# item code.
_TITLE_TO_ITEM = [
    ("entry into a material definitive agreement", "1.01"),
    ("termination of a material definitive agreement", "1.02"),
    ("bankruptcy or receivership", "1.03"),
    ("mine safety", "1.04"),
    ("material cybersecurity incident", "1.05"),
    ("completion of acquisition or disposition of assets", "2.01"),
    ("results of operations and financial condition", "2.02"),
    ("creation of a direct financial obligation", "2.03"),
    ("triggering events that accelerate", "2.04"),
    ("costs associated with exit or disposal activities", "2.05"),
    ("material impairments", "2.06"),
    ("notice of delisting", "3.01"),
    ("unregistered sales of equity securities", "3.02"),
    ("material modification to rights of security holders", "3.03"),
    ("changes in registrants certifying accountant", "4.01"),
    ("non reliance on previously issued financial statements", "4.02"),
    ("nonreliance on previously issued financial statements", "4.02"),
    ("changes in control of registrant", "5.01"),
    ("departure of directors or certain officers", "5.02"),
    ("departure of directors or principal officers", "5.02"),
    ("election of directors", "5.02"),
    ("appointment of certain officers", "5.02"),
    ("compensatory arrangements of certain officers", "5.02"),
    ("amendments to articles of incorporation or bylaws", "5.03"),
    ("temporary suspension of trading under registrants employee benefit plans", "5.04"),
    ("code of ethics", "5.05"),
    ("change in shell company status", "5.06"),
    ("submission of matters to a vote of security holders", "5.07"),
    ("shareholder director nominations", "5.08"),
    ("abs informational and computational material", "6.01"),
    ("change of servicer or trustee", "6.02"),
    ("change in credit enhancement", "6.03"),
    ("failure to make a required distribution", "6.04"),
    ("securities act updating disclosure", "6.05"),
    ("static pool", "6.06"),
    ("regulation fd disclosure", "7.01"),
    ("other events", "8.01"),
    ("financial statements and exhibits", "9.01"),
]
_ITEM_LINE = re.compile(r"ITEM\s+INFORMATION\s*:?\s*(.*)", re.I)
_PUNCT = re.compile(r"[^a-z0-9 ]+")


def _norm(t):
    # Apostrophes are REMOVED, not replaced: "Registrant's" must become
    # "registrants", not "registrant s", or 4.01 and 5.04 never match.
    t = str(t or "").lower().replace("'", "").replace("\u2019", "").replace("-", " ")
    return _PUNCT.sub(" ", t).split()


def parse_items(text):
    """Item codes out of whatever text EDGAR gives us.

    Deliberately conservative: it returns only codes that exist in the 8-K
    item list, because a bare number-dot-number pattern also matches a share
    price, a percentage and a file size. A feed line reading "Size: 45 KB" must
    not turn into an item code.
    """
    if not text:
        return []
    found = []
    text = str(text)
    for m in _ITEM_RE.findall(text):
        if m in ALL_8K_ITEMS and m not in found:
            found.append(m)
    for line in text.splitlines():
        m = _ITEM_LINE.search(line)
        if not m:
            continue
        words = " ".join(_norm(m.group(1)))
        for phrase, code in _TITLE_TO_ITEM:
            if phrase in words and code not in found:
                found.append(code)
    return found


# --- source 2: EDGAR full-text search ---------------------------------------
# Added after the Atom feed returned an empty document on every poll in
# production for an hour straight. This endpoint was confirmed by hand to
# return real SC 13D rows, and it carries something the other sources do not:
# the TICKER, inline, in display_names -- so no CIK lookup is needed, and the
# second name in the list is the reporting person.
#
# Its date parameters could not be made to work (every documented spelling
# returned zero hits), so nothing is passed and the rows are filtered and
# sorted here instead. Slightly wasteful, entirely reliable.

EFTS_URL = "https://efts.sec.gov/LATEST/search-index?q=&forms={form}"
_NAME_TICKER = re.compile(r"^(.*?)\s*\(([A-Z][A-Z0-9.\-]{0,6})\)\s*\(CIK\s*(\d+)\)\s*$")
_NAME_ONLY = re.compile(r"^(.*?)\s*\(CIK\s*(\d+)\)\s*$")


def parse_efts(payload, want="SC 13D"):
    """Rows out of the full-text search response.

    display_names holds every party to the filing. The entry carrying a ticker
    is the issuer -- the company whose stock moves -- and any other is a
    reporting person. That split is exactly the subject/filer distinction the
    Atom feed makes with a "(Subject)" suffix, available here for free.
    """
    hits = (((payload or {}).get("hits") or {}).get("hits")) or []
    out = []
    for h in hits:
        src = h.get("_source") or {}
        acc = src.get("adsh")
        form = (src.get("form") or "").strip()
        if not acc or not _is_form(form, want):
            continue
        issuer_name = issuer_cik = ticker = reporting = None
        for name in src.get("display_names") or []:
            m = _NAME_TICKER.match(str(name).strip())
            if m and not ticker:
                issuer_name, ticker, issuer_cik = m.group(1), m.group(2), int(m.group(3))
                continue
            m2 = _NAME_ONLY.match(str(name).strip())
            if m2 and not reporting:
                reporting = m2.group(1)
        # With no ticker anywhere, the first name is the best guess at the
        # issuer -- but it is NOT confirmed, so downstream will not print a
        # symbol next to it.
        if not issuer_name and (src.get("display_names") or []):
            first = str(src["display_names"][0]).strip()
            m2 = _NAME_ONLY.match(first)
            issuer_name = m2.group(1) if m2 else first
            reporting = None if reporting == issuer_name else reporting
        out.append({
            "accession": acc,
            "form": form.upper(),
            "issuer_cik": issuer_cik,
            "issuer_name": issuer_name,
            "issuer_confirmed": bool(ticker),
            "ticker": ticker,
            "reporting_person": reporting,
            "role": "subject" if ticker else "unknown",
            "filed_at": src.get("file_date") or None,
            "url": _filing_url(src.get("ciks"), acc),
            "source": "full-text-search",
            "items": parse_items(" ".join(src.get("items") or [])),
        })
    # Newest first, because the endpoint's own ordering is not by date and a
    # poller that reads the oldest hundred filings forever never sees today.
    out.sort(key=lambda r: str(r.get("filed_at") or ""), reverse=True)
    return out


def _filing_url(ciks, accession):
    cik = None
    for c in (ciks or []):
        try:
            cik = int(c)
            break
        except (TypeError, ValueError):
            continue
    if not cik or not accession:
        return None
    return (ARCHIVE.format(cik=cik, acc=accession.replace("-", ""))
            + f"/{accession}-index.htm")


# --- source 3: the end-of-day index -----------------------------------------

# The SGML header is read for two different things -- the item codes and the
# acceptance time -- sometimes for the same filing in the same poll. Cached so
# the second question never costs a second request. Bounded because the
# process runs for weeks.
_HEADER_CACHE = {}
_HEADER_CACHE_MAX = 400


def _read_header(cik, accession, log=print):
    """The filing's -index-headers.html, or '' on any failure."""
    if not cik or not accession:
        return ""
    if accession in _HEADER_CACHE:
        return _HEADER_CACHE[accession]
    url = (ARCHIVE.format(cik=int(cik), acc=accession.replace("-", ""))
           + f"/{accession}-index-headers.html")
    try:
        text = _get(url)
    except Exception as e:
        log(f"filings: could not read header for {accession} ({e})")
        return ""
    if len(_HEADER_CACHE) >= _HEADER_CACHE_MAX:
        _HEADER_CACHE.pop(next(iter(_HEADER_CACHE)))
    _HEADER_CACHE[accession] = text
    return text


def fetch_items(cik, accession, log=print):
    """Item codes from the filing's own SGML header.

    The header is the authoritative place: EDGAR writes an ITEM INFORMATION
    line per item. Costs one request per 8-K, which is why the caller caps how
    many it does per poll. Returns [] on any failure, and the caller treats an
    8-K with no readable items as 'not a red flag' rather than guessing -- a
    guess here would put a routine filing in a list whose whole promise is
    that everything in it matters.
    """
    text = _read_header(cik, accession, log=log)
    return parse_items(text) if text else []


# EDGAR renders the SGML tag either raw or HTML-escaped depending on the page.
_ACCEPTED = re.compile(r"ACCEPTANCE-DATETIME(?:&gt;|>)\s*(\d{14})")


def parse_accepted(header_text):
    """The exact second EDGAR accepted the filing, as ISO-8601 in Eastern time.

    The feeds that list filings carry only a calendar date -- the full-text
    index and the daily index both stamp '2026-09-15' and nothing more -- so
    most rows arrived with a day and no time. The header has the real stamp,
    to the second. Returns None when it cannot be read: a missing time is
    shown as a missing time, never invented as midnight.
    """
    m = _ACCEPTED.search(header_text or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(
            tzinfo=EDGAR_TZ).isoformat()
    except ValueError:
        return None


def _has_time(stamp):
    return bool(stamp) and "T" in str(stamp)


def fetch_accepted(cik, accession, log=print):
    return parse_accepted(_read_header(cik, accession, log=log))


def with_exact_time(f, log=print):
    """Upgrade a date-only filed_at to the acceptance stamp, in place.

    Only fetches when the stamp is missing its time, so rows from the Atom
    feed (which already carries one) cost nothing extra.
    """
    if _has_time(f.get("filed_at")):
        return False
    stamp = fetch_accepted(f.get("issuer_cik"), f.get("accession"), log=log)
    if not stamp:
        return False
    f["filed_at"] = stamp
    return True


def backfill_times(store, log=print):
    """Fill in exact times for rows stored before this existed, a few per poll.

    The live table held weeks of 8-Ks stamped with a bare date. Rather than
    hammer sec.gov on boot to fix them all at once, each poll fixes a handful,
    newest first -- the rows a reader is actually looking at heal first.
    """
    rows = store.filings_missing_time(limit=MAX_TIME_BACKFILL)
    fixed = 0
    for r in rows:
        stamp = fetch_accepted(r.get("issuer_cik"), r.get("accession"), log=log)
        if stamp:
            store.set_filed_at(r["accession"], stamp,
                               _latency_ms(stamp, r.get("seen_at")))
            fixed += 1
        else:
            # Do not ask again next poll for a header that has no stamp.
            store.set_filed_at(r["accession"], None, None, give_up=True)
    if rows:
        log(f"filings: exact times filled in for {fixed} of {len(rows)} older row(s)")
    return fixed


# --- reading the document ---------------------------------------------------

_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

# The single biggest trap in judging a 5.02. Filings quote the item's official
# title verbatim -- "Departure of Directors or Certain Officers; Election of
# Directors; Appointment of Certain Officers" -- which contains BOTH the
# departure vocabulary and the appointment vocabulary. Match against the raw
# text and every 5.02 comes back "both", every time, regardless of what
# actually happened. These phrases are removed before anything is matched.
_BOILERPLATE = [
    "departure of directors or certain officers",
    "election of directors",
    "appointment of certain officers",
    "compensatory arrangements of certain officers",
    "departure of directors or principal officers",
    "other events",
    "entry into a material definitive agreement",
]


def to_text(html_or_text):
    t = _TAGS.sub(" ", str(html_or_text or ""))
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&#160;", " ").replace("&rsquo;", "'").replace("&#8217;", "'"))
    return _WS.sub(" ", t).strip().lower()


def strip_boilerplate(text):
    t = text or ""
    for phrase in _BOILERPLATE:
        t = t.replace(phrase, " ")
    return _WS.sub(" ", t)


def fetch_body(cik, accession, log=print):
    """The opening text of the filing's primary document, lowercased.

    Returns "" on any failure, and every caller treats an unreadable body as
    'cannot tell' rather than defaulting to a direction -- a guessed label on
    a page that exists to flag red flags is worse than no label.
    """
    if not cik or not accession:
        return ""
    base = ARCHIVE.format(cik=int(cik), acc=accession.replace("-", ""))
    try:
        listing = _get(base + "/index.json", as_json=True)
        items = (listing.get("directory") or {}).get("item") or []
        docs = [i.get("name", "") for i in items
                if i.get("name", "").lower().endswith((".htm", ".html", ".txt"))
                and "index" not in i.get("name", "").lower()]
        if not docs:
            return ""
        return to_text(_get(f"{base}/{docs[0]}", max_bytes=MAX_BODY_BYTES))
    except Exception as e:
        log(f"filings: could not read body of {accession} ({e})")
        return ""


# --- what the document actually says ----------------------------------------

_DEPARTURE = re.compile(
    r"\b(resign(?:ed|s|ation)?|retir(?:e|ed|es|ement)|stepp?(?:ed|ing|s)?\s+down|"
    r"depart(?:ed|s|ing|ure)|terminat(?:ed|ion)|"
    r"no\s+longer\s+(?:serve|be\s+employed|an?\s+(?:officer|director|employee))|"
    r"separation\s+(?:from|agreement)|removed\s+from\s+the\s+board|"
    r"relieved\s+of\s+(?:his|her|their)\s+duties)\b")
_APPOINTMENT = re.compile(
    r"\b(appoint(?:ed|s|ment|ing)|was\s+elected|has\s+been\s+elected|"
    r"nam(?:ed|es)\s+(?:as|to)|promot(?:ed|ion)|"
    r"join(?:ed|s|ing)\s+the\s+(?:company|board)|hired|will\s+serve\s+as|"
    r"will\s+become\s+(?:the\s+)?(?:new\s+)?(?:chief|president|ceo))\b")

# Bridging gap inside a pattern. Plain [^.] stops at a full stop -- which also
# stops at the decimal point in "$1.2 billion contract", so every award with a
# dollar figure in it was being missed. This allows a period only when a digit
# follows it, keeping the sentence boundary while letting numbers through.
_GAP = r"(?:[^.]|\.(?=\d))"

# Good news specific enough to be worth a row of its own. Deliberately narrow:
# item 8.01 carries everything from an FDA approval to a change of registered
# agent, so anything that does not match one of these is dismissed rather than
# listed. A permissive gate here turns the section back into a firehose.
_GOOD_NEWS = [
    (re.compile(r"\bfda\b" + _GAP + r"{0,80}\bapprov|"
                r"approval\s+of\s+(?:our|its|the)\s+" + _GAP +
                r"{0,40}\b(?:nda|bla|pma|510\(k\))"), "FDA approval"),
    (re.compile(r"(?:met|achiev\w+)\s+(?:its\s+)?primary\s+endpoint|"
                r"positive\s+top-?line\s+result"), "Trial met its endpoint"),
    (re.compile(r"(?:share|stock|common\s+stock)\s+repurchase|buy-?back\s+program"),
     "Buyback authorised"),
    (re.compile(r"(?:initiat\w+|declar\w+|increas\w+)\s+(?:a\s+|its\s+)?"
                r"(?:quarterly\s+|annual\s+|special\s+)?dividend"), "Dividend action"),
    (re.compile(r"award(?:ed)?\s+(?:a\s+|an\s+)?" + _GAP + r"{0,40}\bcontract|"
                r"contract\s+award|received\s+(?:a\s+|an\s+)?" + _GAP +
                r"{0,30}\border\s+(?:valued|worth)"), "Contract award"),
    (re.compile(r"agreement\s+to\s+(?:be\s+)?acquir|definitive\s+merger\s+agreement|"
                r"agreed\s+to\s+acquire"), "Merger agreement"),
    (re.compile(r"(?:favou?rable|favorable)\s+(?:ruling|verdict|judgment)|"
                r"dismissed\s+the\s+(?:complaint|lawsuit|action)|"
                r"granted\s+summary\s+judgment\s+in\s+(?:our|its)\s+favou?r"),
     "Legal win"),
    (re.compile(r"patent" + _GAP + r"{0,40}(?:granted|issued|upheld)"),
     "Patent granted"),
]

# A material agreement that is really a financing. This is the single most
# reliably bearish thing a small cap files: shares sold at a discount, usually
# announced after the close, usually gapping down the next morning.
_DILUTION = re.compile(
    r"at-the-market\s+(?:offering|program|sales\s+agreement)|\batm\s+program\b|"
    r"securities\s+purchase\s+agreement|registered\s+direct\s+offering|"
    r"convertible\s+(?:note|debenture)s?\s+(?:purchase|offering)|"
    r"equity\s+(?:line|purchase)\s+(?:of\s+credit|agreement)|"
    r"standby\s+equity\s+(?:purchase|distribution)")


def judge_5_02(body):
    """Departure, appointment, or can't tell.

    A departure 8-K usually names a successor too, so the two vocabularies
    coexist often. The question that matters is whether somebody LEFT, so
    departure language wins when both are present -- and the label says both
    happened rather than hiding the appointment.
    """
    t = strip_boilerplate(body or "")
    if not t:
        return "unclear", "Executive or director change"
    out, appointed = bool(_DEPARTURE.search(t)), bool(_APPOINTMENT.search(t))
    if out and appointed:
        return "negative", "Executive departure, successor named"
    if out:
        return "negative", "Executive or director departure"
    if appointed:
        return "positive", "Executive or director appointed"
    return "unclear", "Executive or director change"


def judge_body_item(item, body):
    """Direction and label for an item that cannot be read from its number.

    Returns (direction, label) or None, where None means 'not worth listing'.
    """
    if item == "5.02":
        return judge_5_02(body)
    t = strip_boilerplate(body or "")
    if not t:
        return None                  # unreadable 8.01/1.01 is not worth a row
    if _DILUTION.search(t):
        return "negative", ("Dilutive financing agreement" if item == "1.01"
                            else "Dilutive financing")
    for pattern, label in _GOOD_NEWS:
        if pattern.search(t):
            return "positive", label
    return None


_last_body = [""]


def _shape(text):
    """A one-line description of what a response actually was.

    'fetched, 0 rows' is ambiguous: it covers a real empty index and an HTML
    error page returned with status 200, which are completely different
    problems. Naming the shape separates them in the log.
    """
    t = (text or "").lstrip()
    if not t:
        return "an empty body"
    if t[:400].lower().find("<html") >= 0 or t.startswith("<!"):
        return f"an HTML page ({len(text)} bytes) -- not an index file"
    return f"{len(text)} bytes, {text.count(chr(10)) + 1} lines"


def parse_daily_index(text, want="SC 13D"):
    _last_body[0] = text or ""
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
        if not _is_form(form, want):
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
            "items": [],            # the index carries no item codes
            # The index names a party to the filing without saying which side
            # it is on. Treated as usable because enrich() overrides it from
            # the filing itself whenever the document can be read, and a name
            # is more useful than a blank on the fallback path.
            "issuer_confirmed": True,
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
    ("issuer_cik", ("issuercik", "subjectcik", "issuercentralindexkey")),
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

def _age_days(stamp):
    """Age of a filing in days, or None when the stamp cannot be read.

    Unknown age counts as fresh: every source in use stamps its rows, so this
    is rare, and dropping a filing because its date was unparseable would lose
    real data to guard against stale data.
    """
    s = str(stamp or "").strip()
    if not s:
        return None
    try:
        if len(s) <= 10:
            d = datetime.fromisoformat(s[:10]).replace(tzinfo=timezone.utc)
        else:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - d).total_seconds() / 86400.0


def drop_stale(rows, max_age_days=MAX_FEED_AGE_DAYS):
    """(fresh rows, how many were dropped)."""
    fresh, dropped = [], 0
    for r in rows:
        age = _age_days(r.get("filed_at"))
        if age is not None and age > max_age_days:
            dropped += 1
        else:
            fresh.append(r)
    return fresh, dropped


def collect(want="SC 13D", subject_only=True, log=print):
    """Recent filings of one form type, from whichever source answers.

    Returns (rows, source). The source is reported rather than assumed because
    this code cannot be exercised against sec.gov from where it was written --
    the live logs are what tell us which path actually works.
    """
    errors = []
    try:
        rows = parse_atom(_get(ATOM_URL.format(form=want.replace(" ", "+"))),
                          want=want, subject_only=subject_only)
        rows, stale = drop_stale(rows)
        if rows:
            return rows, "atom"
        errors.append(
            f"atom: {_status.get('last_atom_entries', 0)} entries in the feed, "
            f"0 matched {want}" + (f", {stale} too old" if stale else ""))
    except Exception as e:
        errors.append(f"atom: {e}")

    try:
        rows = parse_efts(_get(EFTS_URL.format(form=want.replace(" ", "+")),
                               as_json=True), want=want)
        rows, stale = drop_stale(rows)
        if rows:
            return rows, "full-text-search"
        # The common case for this source, and the one that caused the bug:
        # it answered, with rows, and every one of them was months old.
        errors.append(f"full-text-search: 0 usable rows"
                      + (f" ({stale} older than {MAX_FEED_AGE_DAYS} days)" if stale else ""))
    except Exception as e:
        errors.append(f"full-text-search: {e}")

    today = datetime.now(timezone.utc).date()
    # Weekdays only, and at most three of them. EDGAR publishes no daily index
    # for a Saturday or Sunday, so requesting one is a guaranteed miss that
    # still counts against the burst -- and a burst of Archives requests is
    # what drew 403s in production while the same URLs worked moments later.
    days, probe = [], 0
    while len(days) < 3 and probe < 7:
        d = today - timedelta(days=probe)
        if d.weekday() < 5:
            days.append(d)
        probe += 1
    for day in days:
        try:
            rows = parse_daily_index(_get(_index_url_for(day)), want=want)
            rows, stale = drop_stale(rows)
            if rows:
                return rows, f"daily-index {day.isoformat()}"
        except Exception as e:
            errors.append(f"index {day}: {type(e).__name__} {e}")
        else:
            # Say what came back. sec.gov answers a missing index file with a
            # 200 and an HTML page, which is indistinguishable from a real but
            # empty index unless the content is described.
            errors.append(f"index {day}: fetched {_shape(_last_body[0])}, "
                          f"0 {want} rows")
    _status["last_error"] = " | ".join(errors[:4]) or None
    # Every attempt, not just the first. The first version logged errors[0]
    # only, so a live failure showed one line about the Atom feed and said
    # nothing at all about whether the fallback had even been reached.
    log(f"filings: no source returned {want} rows. Tried: " + " | ".join(errors))
    return [], None


def poll_13d(store, log=print):
    rows, source = collect("SC 13D", subject_only=True, log=log)
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
        f["kind"] = "13d"
        # When the feed gave only the FILER's entry, the CIK in the title is
        # the investor's, not the company's -- a ticker looked up with it would
        # print the wrong symbol beside the stake, which is worse than none.
        # The filing's own document names the issuer, so prefer that and fall
        # back to nothing rather than to the investor.
        issuer_cik = _number(detail.get("issuer_cik"))
        if issuer_cik:
            f["issuer_cik"] = int(issuer_cik)
        elif not f.get("issuer_confirmed", True):
            f["issuer_cik"] = None
        f["ticker"] = tickers.get(f.get("issuer_cik"))
        f["issuer_name"] = detail.get("issuer_name") or (
            f.get("issuer_name") if f.get("issuer_confirmed", True) else None)
        f["percent"] = detail.get("percent")
        f["shares"] = detail.get("shares")
        f["cusip"] = detail.get("cusip")
        f["reporting_person"] = detail.get("reporting_person")
        f["seen_at"] = seen_at
        # The header for the 13D's issuer CIK may differ from the filer's; the
        # accession is the same either way, and that is what the URL is built
        # from once the CIK resolves.
        with_exact_time(f, log=log)
        f["latency_ms"] = _latency_ms(f.get("filed_at"), seen_at)
        if store.insert_filing(f):
            stored += 1
            log(f"filings: {f['form']} {f.get('ticker') or f.get('issuer_name') or '?'}"
                f" {('%.1f%%' % f['percent']) if f.get('percent') else ''}"
                f" by {f.get('reporting_person') or 'undisclosed'}")
    _status["stored"] += stored
    if stored:
        log(f"filings: stored {stored} new 13D filing(s) from {source}"
            + ("" if _primed[0] else " -- first pass"))
    _primed[0] = True
    return stored


def _fresh_enough(filed_at):
    """Is this filing new enough to be treated as breaking?

    A date-only stamp counts as fresh only if it is today: EDGAR dates a
    filing without a time of day often enough that treating every such row as
    stale would silence the daily-index path entirely.
    """
    s = str(filed_at or "").strip()
    if not s:
        return False
    now = datetime.now(timezone.utc)
    if len(s) <= 10:
        return s[:10] == now.date().isoformat()
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return False
    if t.tzinfo is None:
        return False
    return 0 <= (now - t).total_seconds() <= ALERT_MAX_AGE_HOURS * 3600


def assess(items, body_fn):
    """Should this 8-K be listed, and which way does it cut?

    Returns (direction, labels) or None. `body_fn` is called at most once and
    only when an item genuinely needs the document read, so a filing whose
    items already settle the question costs nothing extra.
    """
    direction, labels, body, read_body = None, [], None, False

    for item in items:
        if item in BODY_ITEMS:
            if not read_body:
                body, read_body = body_fn(), True
            verdict = judge_body_item(item, body)
            if verdict:
                d, label = verdict
                labels.append(label)
                # A red flag outranks good news in the same filing. A company
                # announcing a buyback in the same 8-K that discloses a
                # delisting notice is not a buyback story.
                if d == "negative" or direction is None:
                    direction = d
                elif direction == "positive" and d == "unclear":
                    direction = "unclear"
        elif item in RED_FLAG_ITEMS:
            labels.append(ITEM_LABELS.get(item, "Item " + item))
            direction = "negative"
        elif item in TAILWIND_ITEMS:
            labels.append(ITEM_LABELS.get(item, "Item " + item))
            if direction != "negative":
                direction = "positive"
    if not labels:
        return None
    return direction or "unclear", labels


def poll_8k(store, log=print):
    """8-Ks worth a row, and which way they cut.

    Staged on purpose, cheapest first. The feed is one request. Item codes are
    one request per filing, capped. The document body is one more, capped
    separately and only for the three items whose number does not settle the
    question. Filings judged not worth listing are recorded as seen, so the
    next poll does not pay to look at them again.
    """
    rows, source = collect("8-K", subject_only=False, log=log)
    _status.update(last_8k_poll=datetime.now(timezone.utc).isoformat(),
                   last_8k_source=source, last_8k_count=len(rows))
    if not rows:
        return 0
    tickers = ticker_map(log=log)
    seen_at = datetime.now(timezone.utc).isoformat()
    stored = looked = bodies = skipped = 0
    for f in rows:
        acc = f["accession"]
        if store.filing_exists(acc) or store.filing_dismissed(acc):
            continue
        items = f.get("items") or []
        if not items:
            if looked >= MAX_HEADER_FETCH:
                skipped += 1
                continue
            looked += 1
            items = fetch_items(f.get("issuer_cik"), acc, log=log)
        keep = [i for i in items
                if i in RED_FLAG_ITEMS or i in TAILWIND_ITEMS or i in BODY_ITEMS]
        if not keep:
            # Remember the verdict, not the filing. Routine 8-Ks are the bulk
            # of the feed and re-reading their headers every ninety minutes
            # would be the single most wasteful thing this service does.
            store.dismiss_filing(acc)
            continue
        if any(i in BODY_ITEMS for i in keep) and bodies >= MAX_BODY_FETCH:
            skipped += 1              # deferred, NOT dismissed
            continue

        state = {"n": 0}

        def read_body(cik=f.get("issuer_cik"), acc=acc, state=state):
            state["n"] += 1
            return fetch_body(cik, acc, log=log)

        verdict = assess(keep, read_body)
        bodies += state["n"]
        if not verdict:
            store.dismiss_filing(acc)
            continue
        direction, labels = verdict
        f = dict(f)
        f["kind"] = "8k"
        f["items"] = keep
        f["labels"] = labels
        f["direction"] = direction
        f["ticker"] = tickers.get(f.get("issuer_cik"))
        f["seen_at"] = seen_at
        # The exact time comes from the same header the item codes came from,
        # so for a filing whose header was already read this is free; for the
        # rest it is one request per filing actually being stored.
        with_exact_time(f, log=log)
        f["latency_ms"] = _latency_ms(f.get("filed_at"), seen_at)
        if store.insert_filing(f):
            stored += 1
            log(f"filings: 8-K [{direction}] "
                f"{f.get('ticker') or f.get('issuer_name') or '?'} — "
                + "; ".join(labels))
    _status["stored_8k"] += stored
    try:
        backfill_times(store, log=log)
    except Exception as e:
        log(f"filings: time backfill skipped ({e})")
    if stored or skipped:
        log(f"filings: {stored} notable 8-K(s) from {len(rows)} filings "
            f"({looked} headers, {bodies} documents read"
            + (f", {skipped} deferred to next poll" if skipped else "") + ")")
    return stored


# --- filed, but did anybody report it? --------------------------------------
# The differentiating claim of this section. An 8-K that moves a stock is
# usually written up by a wire within minutes; the ones that are NOT are
# either genuinely unnoticed or were filed at a time chosen so they would be.
# Either way "nobody covered this" is the interesting label, and we can only
# make it because this service already keeps a timestamped headline archive.

COVERAGE_GRACE_MINUTES = 25      # give the wire a fair chance before judging
COVERAGE_BEFORE_MINUTES = 30     # a wire that broke the story first still counts
COVERAGE_AFTER_MINUTES = 45


def check_coverage(store, log=print):
    cutoff = (datetime.now(timezone.utc)
              - timedelta(minutes=COVERAGE_GRACE_MINUTES)).isoformat()
    pending = store.filings_needing_coverage(cutoff, limit=50)
    done = 0
    for row in pending:
        stamp = row.get("filed_at") or row.get("seen_at")
        # A date-only stamp has no time of day, so a 30-minute window around it
        # is meaningless. Mark it checked and leave the verdict unknown rather
        # than compare against an invented midnight.
        if not stamp or len(str(stamp)) <= 10 or not row.get("ticker"):
            store.set_coverage(row["accession"], None)
            done += 1
            continue
        try:
            t = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
        except ValueError:
            store.set_coverage(row["accession"], None)
            done += 1
            continue
        n = store.headlines_mentioning(
            row["ticker"],
            (t - timedelta(minutes=COVERAGE_BEFORE_MINUTES)).astimezone(timezone.utc).isoformat(),
            (t + timedelta(minutes=COVERAGE_AFTER_MINUTES)).astimezone(timezone.utc).isoformat())
        store.set_coverage(row["accession"], 0 if n else 1)
        done += 1
    if done:
        log(f"filings: coverage checked on {done} filing(s)")
    return done


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
            # Each stage is guarded separately: a broken 8-K header format
            # must not stop 13Ds arriving, and neither can be allowed to
            # interrupt headline ingest, which cannot be recovered later.
            for name, fn in (("13D", poll_13d), ("8-K", poll_8k),
                             ("coverage", check_coverage)):
                try:
                    fn(store, log=log)
                except Exception as e:
                    _status["last_error"] = f"{name}: {e}"
                    log(f"filings {name} pass failed (non-fatal): {e}")
            time.sleep(POLL_SECONDS)

    _thread = threading.Thread(target=loop, daemon=True, name="filings")
    _thread.start()
    log("filings: EDGAR watcher started (13D + notable 8-K)")
