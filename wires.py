"""
The wires: press releases straight from the pipe.

Every headline Benzinga (and so Alpaca, and so Tapehawk) delivers about an
FDA approval or a buyout started life as a press release on one of four
wire services -- GlobeNewswire, PR Newswire, Business Wire, Accesswire. The
company posts it there at 7:00:00; Benzinga reads it, rewrites the
headline, and publishes its version some seconds to a minute later. For a
page whose whole point is the first ninety seconds after a story, that
rewrite is the slowest link in the chain.

This module reads the wires directly. Each feed is polled every few seconds
with a conditional request (the wire answers "nothing new" in a few bytes
when nothing is new), each release is run through the same classifier as
the socket's headlines, and the ones that carry a US ticker go into the
same table, out the same server-sent stream, and into the same Snipe
setups -- with the source named, so the page shows "globenewswire" where it
would have shown "benzinga".

And it keeps score. When the same story arrives from two places -- the wire
and then Benzinga, or Benzinga and then the wire -- the pair is matched (same
ticker, within fifteen minutes) and the lag is recorded against the source
that came second. After a week the table says, per source, how often it was
first and how far behind it was when it was not. That number is what
decides whether a paid feed is worth buying: it has to beat the free one.

What a wire's RSS does NOT give you: a precise timestamp (GlobeNewswire's
is to the minute; the arrival time here is what the clock runs on), a
ticker on every release (law firms and private companies post too; those
are dropped), or the whole text (the first paragraph, which is where the
ticker lives). Business Wire and Accesswire do not publish an open
all-releases feed; add one with WIRE_FEEDS if you have a URL that works.

Since v2 step 9 (October 2026) the wires ARE the stream: the Benzinga
socket is off, and every headline the site and Halthawk see came off a
wire. Each one carries the wire's own publish time (wire_pub), the first
paragraph, the rtpr article id and rtpr's impact block when it has one.
Two things the same source can do twice are caught here, at ingest: rtpr
sends a two-ticker release as two frames with one link (the second ticker
is merged into the stored row, the article is fetched once), and it can
deliver one release under two article ids (the second is stored and marked
a copy of the first: the same stock with the same headline, or the same
stock, wire, publish minute and first paragraph, within a minute). A copy
always names the same stock: two companies' template headlines in one
minute are two stories. The scoreboard skips same-source pairs by design,
so it could not be used for that.
"""
import html
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

import bus
import classify
import store

USER_AGENT = "Tapehawk/1.0 (+https://tapehawk.onrender.com)"
TIMEOUT = 8
POLL_S = float(os.environ.get("WIRE_POLL_S") or 3.0)
BACKOFF_MAX = 60.0
MATCH_WINDOW_S = 15 * 60               # same ticker within this long = the same story
RECENT_KEEP = 2000
COPY_WINDOW_S = 60                     # one source, two deliveries of one release this close = a copy
COPY_OVERLAP = 0.5                     # ... when the headlines share half their words (and the stock)
PARA_KEY_CHARS = 120                   # how much of the first paragraph the minute rule compares

# name=url;name=url in WIRE_FEEDS replaces this list.
DEFAULT_FEEDS = [
    ("globenewswire", "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies"),
    ("prnewswire", "https://www.prnewswire.com/rss/news-releases-list.rss"),
]

US_EXCHANGES = {"nasdaq", "nyse", "nyse american", "nyse mkt", "nyse arca", "amex", "nyseamerican", "cboe", "cboe bzx", "nasdaq gs", "nasdaq gm", "nasdaq cm"}
# "(NASDAQ: ABCD)", "(NYSE American: XYZ)", "(Nasdaq: ABCD, ABCDW)"
_TICKER_RE = re.compile(r"\b(NASDAQ|Nasdaq|NYSE(?:\s+(?:American|MKT|Arca))?|AMEX|NYSEAMERICAN|Cboe(?:\s+BZX)?)\s*:\s*([A-Z]{1,5})(?:\.[A-Z])?\b")
_TAG_RE = re.compile(r"<[^>]+>")
_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "and", "of", "for", "to", "in", "on", "with", "its", "inc", "corp", "corporation", "ltd", "co", "announces", "announced", "today"}


def feeds():
    raw = os.environ.get("WIRE_FEEDS")
    if not raw:
        return list(DEFAULT_FEEDS)
    out = []
    for part in raw.split(";"):
        if "=" in part:
            name, url = part.split("=", 1)
            if name.strip() and url.strip():
                out.append((name.strip().lower(), url.strip()))
    return out or list(DEFAULT_FEEDS)


# ---- parsing ------------------------------------------------------------------------

def _text(el, tag):
    x = el.find(tag)
    return (x.text or "").strip() if x is not None and x.text else ""


def _clean(s):
    s = html.unescape(_TAG_RE.sub(" ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def tickers_in(text):
    """US-listed tickers named in the text, in order, deduplicated."""
    out = []
    for ex, sym in _TICKER_RE.findall(text or ""):
        if ex.lower() in US_EXCHANGES and sym not in out:
            out.append(sym)
    return out


def parse_feed(xml_text):
    """RSS 2.0 items as dicts: guid, title, link, pub (iso or None),
    description (first paragraph, cleaned), tickers (US-listed only),
    exchanges_seen (every exchange:ticker tag, for the record)."""
    root = ET.fromstring(xml_text)
    items = []
    for it in root.iter("item"):
        title = _clean(_text(it, "title"))
        if not title:
            continue
        guid = _text(it, "guid") or _text(it, "link") or title
        desc = _clean(_text(it, "description"))
        pub = None
        raw = _text(it, "pubDate")
        if raw:
            try:
                d = parsedate_to_datetime(raw)
                pub = (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()
            except (TypeError, ValueError):
                pub = None
        tagged, seen = [], []
        for cat in it.findall("category"):
            dom = (cat.get("domain") or "").lower()
            val = (cat.text or "").strip()
            if dom.endswith("/rss/stock") and ":" in val:
                ex, sym = val.rsplit(":", 1)
                seen.append(val)
                if ex.strip().lower() in US_EXCHANGES and re.fullmatch(r"[A-Z]{1,5}", sym.strip()):
                    if sym.strip() not in tagged:
                        tagged.append(sym.strip())
        syms = tagged or tickers_in(title + " " + desc)
        items.append({"guid": guid, "title": title, "link": _text(it, "link") or guid, "pub": pub,
                      "description": desc[:800], "tickers": syms, "exchanges_seen": seen})
    return items


# ---- the scoreboard: who was first --------------------------------------------------

_lock = threading.Lock()
_recent = deque(maxlen=RECENT_KEEP)        # {"ts", "source", "symbols", "tokens", "headline", "id"}
_score = {}                                # source -> {"n", "first", "lags": deque, "examples": deque}
_state = {"running": False, "feeds": {}, "stored": 0, "no_ticker": 0, "seen": 0, "matched": 0,
          "copies": 0, "merged": 0, "last_stored_at": None}


def _pub_minute(iso):
    """The wire's publish minute as epoch seconds, or None."""
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00")) if iso else None
    except ValueError:
        return None
    if d is None:
        return None
    d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return int(d.timestamp() // 60) * 60


def _tokens(title):
    return {w for w in _WORD_RE.findall((title or "").lower()) if w not in _STOP and len(w) > 2}


def _para_key(text):
    """The start of a first paragraph, flattened, so two deliveries of one
    release compare equal whatever the headline parse made of them."""
    return re.sub(r"\s+", " ", (text or "").lower()).strip()[:PARA_KEY_CHARS]


def _bucket(source):
    return _score.setdefault(source, {"n": 0, "first": 0, "lags": deque(maxlen=500), "examples": deque(maxlen=12)})


def note_arrival(source, symbols, headline, ts=None, article_id=None):
    """Record a headline's arrival from a source and match it against the
    others. Returns the match (first source, lag) or None. Called for every
    stored headline, socket and wire alike."""
    ts = ts or time.time()
    syms = {str(s).upper() for s in (symbols or []) if s}
    toks = _tokens(headline)
    me = {"ts": ts, "source": source, "symbols": syms, "tokens": toks, "headline": headline, "id": article_id}
    match = None
    with _lock:
        _bucket(source)["n"] += 1
        _state["seen"] += 1
        best = None
        for other in reversed(_recent):
            if ts - other["ts"] > MATCH_WINDOW_S:
                break
            if other["source"] == source or not (other["symbols"] & syms):
                continue
            j = len(other["tokens"] & toks) / max(1, len(other["tokens"] | toks))
            if best is None or j > best[1]:
                best = (other, j)
        _recent.append(me)
        if best is not None:
            other, j = best
            lag = round(ts - other["ts"], 1)
            _bucket(source)["lags"].append(lag)
            _bucket(other["source"])["first"] += 1
            _bucket(source)["examples"].appendleft({"symbol": sorted(other["symbols"] & syms)[0], "headline": headline[:90],
                                                     "first": other["source"], "lag_s": lag, "overlap": round(j, 2),
                                                     "at": datetime.fromtimestamp(ts, timezone.utc).isoformat()})
            _state["matched"] += 1
            match = {"first": other["source"], "lag_s": lag, "overlap": round(j, 2), "first_id": other.get("id")}
    return match


def scoreboard():
    with _lock:
        out = {}
        for src, b in _score.items():
            lags = sorted(b["lags"])
            med = lags[len(lags) // 2] if lags else None
            out[src] = {"headlines": b["n"], "first": b["first"], "second": len(lags),
                        "median_lag_s": med, "worst_lag_s": lags[-1] if lags else None,
                        "examples": list(b["examples"])}
        return out


def status():
    with _lock:
        s = dict(_state)
        s["feeds"] = {k: dict(v) for k, v in _state["feeds"].items()}
    return s


def stream_status():
    """What the Live tab's dot and /api/feed['stream'] read now that the
    wires are the stream: connected when any wire source is up (a poll
    that answered, or the rtpr socket open), which sources, and when the
    last release was stored. The Benzinga socket is off and says so."""
    s = status()
    up = sorted(k for k, f in s["feeds"].items() if f.get("ok"))
    return {"source": "wires", "connected": bool(up), "running": s["running"],
            "sources": sorted(s["feeds"]), "sources_up": up,
            "stored": s["stored"], "copies": s["copies"], "merged": s["merged"],
            "last_message_at": s.get("last_stored_at"),
            "listeners": bus.listeners(), "benzinga": "off"}


# ---- the pollers ------------------------------------------------------------------------

class Source(threading.Thread):
    """What every wire source shares: a seen-set, the article shape, the
    store, the stream, the scoreboard. Subclasses get the releases."""
    # Whether "same ticker, same wire, same publish minute, same first
    # paragraph" makes a copy. True only for rtpr, whose header-line
    # fallback can give one release two headlines; an RSS feed's headlines
    # are the real ones, and its description is not the release's text.
    COPY_BY_MINUTE = False

    def __init__(self, name, url, log, publish=None, on_article=None):
        super().__init__(name=f"wire-{name}", daemon=True)
        self.src, self.url, self.log = name, url, log
        self.publish, self.on_article = publish, on_article
        self.seen = set()
        self.order = deque(maxlen=5000)
        self.primed = False
        self.arrivals = deque(maxlen=200)          # this source's last releases, for the copy check
        with _lock:
            _state["feeds"][name] = {"url": url, "polls": 0, "not_modified": 0, "items": 0, "stored": 0, "errors": 0,
                                     "last_poll_at": None, "last_error": None, "last_status": None, "ok": False}

    def _st(self, **kw):
        with _lock:
            _state["feeds"][self.src].update(kw)

    def _bump(self, key, n=1):
        with _lock:
            _state["feeds"][self.src][key] = _state["feeds"][self.src].get(key, 0) + n

    def _mark(self, guid):
        if guid in self.seen:
            return False
        self.seen.add(guid)
        self.order.append(guid)
        if len(self.seen) > 5000:
            self.seen.discard(self.order[0])
        return True

    def _print(self, art):
        """What the copy check compares: the headline's words, the tickers,
        the wire, the publish minute and the start of the first paragraph."""
        return {"tokens": _tokens(art["headline"]),
                "symbols": {str(s).upper() for s in (art.get("symbols") or []) if s},
                "wire": art.get("author") or self.src, "pub_min": _pub_minute(art.get("wire_pub")),
                "para": _para_key(art.get("paragraph") or art.get("summary"))}

    def copy_of(self, art, now_s):
        """The id of this source's own earlier delivery of the same release
        within COPY_WINDOW_S, or None. A copy always names the same stock
        (two companies' template headlines -- "X to Present at the H.C.
        Wainwright Conference", a law firm's "Encourages X Investors" run --
        are two stories however alike they read). On a shared stock it is
        the same headline (half the words shared) or, when rtpr's header-line
        fallback gave one release two headlines, the same wire, publish
        minute and first paragraph. A company's second release in the same
        minute has its own paragraph, so it stays its own story. When only
        one of the two deliveries carried a wire mark the wire is not held
        against the pair."""
        me = self._print(art)
        for other in reversed(self.arrivals):
            if now_s - other["ts"] > COPY_WINDOW_S:
                break
            if not (other["symbols"] & me["symbols"]):
                continue
            j = len(other["tokens"] & me["tokens"]) / max(1, len(other["tokens"] | me["tokens"]))
            if j >= COPY_OVERLAP:
                return other["id"]
            if self.COPY_BY_MINUTE and me["pub_min"] is not None and other["pub_min"] == me["pub_min"] \
                    and (other["wire"] == me["wire"] or self.src in (other["wire"], me["wire"])) \
                    and me["para"] and other["para"] == me["para"]:
                return other["id"]
        return None

    def handle(self, items, now=None):
        """New items -> articles. Returns how many were stored."""
        now = now or datetime.now(timezone.utc)
        stored = 0
        for it in items:
            if not self._mark(it["guid"]):
                continue
            if not self.primed:
                continue                                   # everything on the feed at boot is history
            if not it["tickers"]:
                with _lock:
                    _state["no_ticker"] += 1
                continue
            art = self.article(it, now)
            first_id = self.copy_of(art, now.timestamp())
            if first_id is not None:
                art["copy_of"] = first_id                  # stored and sent with the mark; never a second story
            try:
                fresh = store.insert(art)
            except Exception as e:
                self.log(f"wire {self.src}: store failed -- {e}")
                continue
            if not fresh:
                continue
            stored += 1
            with _lock:
                _state["stored"] += 1
                _state["last_stored_at"] = now.isoformat()
                if first_id is not None:
                    _state["copies"] += 1
            art["id"] = store.id_for(art["alpaca_id"])
            self.arrivals.append(dict(self._print(art), ts=now.timestamp(), id=first_id if first_id is not None else art["id"]))
            # a copy is not an arrival: the scoreboard would otherwise count the same release twice for this source
            m = None if first_id is not None else note_arrival(self.src, art["symbols"], art["headline"], now.timestamp(), art.get("id"))
            self.log(f"wire [{self.src}] [{','.join(art['symbols'][:3])}]"
                     + (f" ** BIG {art['importance']} {str(art['tone']).upper()} **" if art["big"] else "")
                     + (f" (copy of #{first_id})" if first_id is not None else (f" ({m['lag_s']:.0f}s after {m['first']})" if m else " (first)"))
                     + f" {art['headline'][:96]}")
            if not art["is_noise"]:
                if self.publish:
                    self.publish(art)
                if self.on_article:
                    try:
                        self.on_article(art)
                    except Exception as e:
                        self.log(f"wire {self.src}: handler failed -- {e}")
        return stored

    def article(self, it, now):
        headline = classify.normalize(it["title"]).strip()
        cats = classify.classify_headline(headline)
        noise = classify.is_noise(headline)
        imp = classify.importance(headline, it["tickers"], cats)
        tn = classify.tone(headline)
        ip = classify.impact(headline, it["tickers"])
        # The arrival is the clock. A wire's pubDate is to the minute (or to a
        # scheduled :00), so it is kept for the record and the latency column,
        # never used as the story time.
        pub_dt = None
        try:
            pub_dt = datetime.fromisoformat(it["pub"]) if it["pub"] else None
        except ValueError:
            pub_dt = None
        latency_ms = None
        if pub_dt:
            delta = (now - pub_dt).total_seconds() * 1000
            if 0 <= delta <= 3600_000:
                latency_ms = int(delta)
        return {
            "alpaca_id": f"{self.src}:{it['guid']}", "created_at": now.isoformat(), "received_at": now.isoformat(),
            "latency_ms": latency_ms, "headline": headline, "summary": it["description"] or None, "content": None,
            "author": None, "source": self.src, "url": it["link"], "symbols": it["tickers"],
            "categories": cats, "category_labels": [classify.CATEGORY_LABEL[c] for c in cats],
            "is_noise": noise, "importance": imp["score"], "reasons": imp["reasons"], "big": imp["big"],
            "tone": tn["direction"], "tone_reasons": tn["reasons"], "impact_level": ip["level"], "impact_note": ip["note"],
            # v2 step 9: what Halthawk reads off every release. wire_pub is
            # the wire's own publish time; paragraph, rtpr_id and impact
            # come only off the rtpr socket (an RSS description is not the
            # release's first paragraph, and Halthawk's reader tells the two
            # apart by whether paragraph is there); copy_of is set by handle().
            "wire_pub": it["pub"], "paragraph": None,
            "rtpr_id": it.get("rtpr_id"), "impact": it.get("impact") or None, "copy_of": None,
        }


class Feed(Source):
    """An RSS feed, polled."""
    def __init__(self, name, url, log, publish=None, on_article=None):
        super().__init__(name, url, log, publish, on_article)
        self.etag = self.modified = None
        self.backoff = POLL_S

    def fetch(self):
        headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.5"}
        if self.etag:
            headers["If-None-Match"] = self.etag
        if self.modified:
            headers["If-Modified-Since"] = self.modified
        r = requests.get(self.url, headers=headers, timeout=TIMEOUT)
        self._st(last_status=r.status_code)
        if r.status_code == 304:
            return None
        r.raise_for_status()
        self.etag = r.headers.get("ETag") or self.etag
        self.modified = r.headers.get("Last-Modified") or self.modified
        return r.text

    def poll_once(self):
        text = self.fetch()
        # A poll that answered, with news or with "nothing new", is this
        # source being up; the stream dot on the Live tab reads it.
        self._st(ok=True, last_poll_at=datetime.now(timezone.utc).isoformat())
        if text is None:
            self._st(not_modified=_state["feeds"][self.src]["not_modified"] + 1)
            return 0
        items = parse_feed(text)
        self._st(items=_state["feeds"][self.src]["items"] + len(items))
        n = self.handle(items)
        if not self.primed:
            self.primed = True
            self.log(f"wire {self.src}: primed with {len(items)} releases on the feed; new ones from here")
        self._st(stored=_state["feeds"][self.src]["stored"] + n)
        return n

    def run(self):
        self.log(f"wire {self.src}: polling {self.url} every {POLL_S:.0f}s")
        while True:
            try:
                self.poll_once()
                self._st(polls=_state["feeds"][self.src]["polls"] + 1, last_error=None)
                self.backoff = POLL_S
            except Exception as e:
                msg = str(e)[:140]
                self._st(errors=_state["feeds"][self.src]["errors"] + 1, last_error=msg, ok=False)
                if self.backoff == POLL_S:
                    self.log(f"wire {self.src}: {msg}")
                self.backoff = min(BACKOFF_MAX, self.backoff * 2)
            time.sleep(self.backoff)


# ---- RTPR: the four wires over one socket ----------------------------------------------------
# rtpr.io relays Business Wire, PR Newswire, GlobeNewswire and Accesswire.
# Its socket is deliberately thin: an alert frame carries the ticker, the
# wire's publish time and a signed link -- no headline. So every alert costs
# one more request, for the article text, before it can be judged. The link
# is hot for two minutes after publish and works after that with the key.
# Alerts come from RULES made in their dashboard; the "All articles" rule is
# what turns the socket into a firehose. The key is RTPR_KEY in the
# environment -- the dashboard, never the repo.

RTPR_WS = "wss://ws.rtpr.io/ws-alerts"
RTPR_FORMAT = "md"
_WIRE_MARKS = [
    ("businesswire", re.compile(r"\(BUSINESS WIRE\)|business ?wire", re.I)),
    ("prnewswire", re.compile(r"/PRNewswire/|PR ?Newswire", re.I)),
    ("globenewswire", re.compile(r"\(GLOBE NEWSWIRE\)|globe ?newswire", re.I)),
    ("accesswire", re.compile(r"/\s*ACCESS ?Newswire\s*/|ACCESS ?WIRE|accessnewswire", re.I)),
]
_HDR_RE = re.compile(r"^\s*\**\s*(title|headline|wire|source|distributor|published|ticker|tickers|symbols?|company|url)\s*\**\s*:\s*\**\s*(.+?)\s*$", re.I)


def rtpr_key():
    return os.environ.get("RTPR_KEY") or ""


def parse_rtpr_article(text):
    """The headline, the wire it came from, and the first paragraph, out of
    an rtpr.io article in markdown or text form. Tolerant: a markdown H1,
    else a Title/Headline header line, else the first real line."""
    lines = [l.rstrip() for l in (text or "").splitlines()]
    hdr = {}
    headline = None
    body_start = 0
    for n, l in enumerate(lines[:40]):
        t = l.strip()
        if not t:
            continue
        m = _HDR_RE.match(t)
        if m:
            hdr[m.group(1).lower()] = m.group(2).strip()
            body_start = n + 1
            continue
        if t.startswith("#"):
            if headline is None:
                headline = t.lstrip("#").strip()
                body_start = n + 1
            continue
        if headline is None and not hdr.get("title") and not hdr.get("headline"):
            headline = t
            body_start = n + 1
        break
    headline = hdr.get("title") or hdr.get("headline") or headline or ""
    para = ""
    for l in lines[body_start:body_start + 40]:
        t = l.strip()
        if t and not t.startswith("#") and not _HDR_RE.match(t):
            para = t
            break
    wire = None
    named = (hdr.get("wire") or hdr.get("source") or hdr.get("distributor") or "")
    probe = named + " " + " ".join(lines[:60])
    for name, rx in _WIRE_MARKS:
        if rx.search(probe):
            wire = name
            break
    return {"headline": _clean(headline), "wire": wire, "paragraph": _clean(para)[:800], "header": hdr}


class RtprSocket(Source):
    COPY_BY_MINUTE = True

    def __init__(self, log, publish=None, on_article=None):
        super().__init__("rtpr", RTPR_WS, log, publish, on_article)
        self.primed = True                              # a socket has no backlog: everything it sends is new
        self.sample_logged = False
        self.pairs = set()                              # link|ticker frames already handled
        self.pair_order = deque(maxlen=5000)
        self._st(connected=False, alerts=0, fetched=0, fetch_errors=0, reconnects=0, merged=0)

    def fetch_article(self, url):
        sep = "&" if "?" in url else "?"
        r = requests.get(f"{url}{sep}format={RTPR_FORMAT}", headers={"User-Agent": USER_AGENT, "X-API-Key": rtpr_key(),
                                                                 "Accept": "text/markdown, text/plain;q=0.9, */*;q=0.5"}, timeout=6)
        self._st(last_status=r.status_code)
        r.raise_for_status()
        return r.text

    def on_frame(self, msg, now=None):
        """One alert frame -> one release. Both tickers of a two-ticker
        release arrive as two frames with the same link: the article is
        fetched once, and the second frame's ticker is merged into the
        stored row (the seen-set is keyed on link + ticker, so a frame is
        a repeat only when both match). Returns the stored count."""
        if msg.get("type") == "ping":
            return None
        if msg.get("type") != "alert" or not msg.get("article_url"):
            return 0
        now = now or datetime.now(timezone.utc)
        self._bump("alerts")
        url = msg["article_url"]
        key = url.split("?", 1)[0]
        tick = str(msg.get("ticker") or "").upper()
        if not re.fullmatch(r"[A-Z]{1,5}", tick):
            tick = ""
        pair = f"{key}|{tick}"
        if pair in self.pairs:
            return 0                                                    # this very frame again
        if key in self.seen:
            # the release is on file under its first ticker: merge this one in, no second fetch
            self.pairs.add(pair)
            self.pair_order.append(pair)
            if len(self.pairs) > 5000:
                self.pairs.discard(self.pair_order[0])
            if not tick:
                return 0
            syms = None
            try:
                syms = store.add_symbol(f"rtpr:{key}", tick)
            except Exception as e:
                self.log(f"wire rtpr: could not add {tick} to {key}: {str(e)[:100]}")
            if syms is not None:
                self._bump("merged")
                with _lock:
                    _state["merged"] += 1
                self.log(f"wire rtpr: {tick} added to the same release ({','.join(syms)})")
                return 0
            # The link was seen but no row holds it: the first frame named no
            # ticker and the text named none, so the release was dropped. This
            # frame names one, so the release gets its chance under it.
            self.log(f"wire rtpr: {tick} frame for a release that was not stored; fetching it again under {tick}")
            self.seen.discard(key)
        try:
            text = self.fetch_article(url)
            self._bump("fetched")
        except Exception as e:
            self._bump("fetch_errors")
            self._st(last_error=f"article fetch: {str(e)[:100]}")
            self.log(f"wire rtpr: could not fetch {key}: {str(e)[:100]}")
            return 0
        if not self.sample_logged:
            self.sample_logged = True
            self.log("wire rtpr: first article sample >>> " + " | ".join(l.strip() for l in text.splitlines()[:8] if l.strip())[:400])
        art = parse_rtpr_article(text)
        if not art["headline"]:
            self.log(f"wire rtpr: no headline found in {key}")
            return 0
        syms = [tick] if tick else tickers_in(art["headline"] + " " + art["paragraph"])
        # the article id the way Halthawk's archive fetcher names it: the frame's id, else the link's last part
        rtpr_id = str(msg.get("article_id") or msg.get("id") or key.rstrip("/").rsplit("/", 1)[-1] or "") or None
        item = {"guid": key, "title": art["headline"], "link": key, "pub": msg.get("article_published_at"),
                "description": art["paragraph"], "tickers": syms, "exchanges_seen": [], "wire": art["wire"], "rtpr_id": rtpr_id,
                "impact": {k: msg.get(k) for k in ("alert_kind", "impact_score", "impact_tier", "event_type", "impact_direction") if msg.get(k) is not None}}
        n = self.handle([item], now)
        self.pairs.add(pair)
        self.pair_order.append(pair)
        if len(self.pairs) > 5000:
            self.pairs.discard(self.pair_order[0])
        return n

    def article(self, it, now):
        a = super().article(it, now)
        if it.get("wire"):
            a["author"] = it["wire"]                   # the page shows "rtpr · businesswire": which pipe it came off
        a["paragraph"] = it["description"] or None     # the release's own first paragraph, as rtpr delivered it
        return a

    def run(self):
        from websocket import create_connection
        import json
        import ssl
        backoff = 2.0
        if not rtpr_key():
            self.log("wire rtpr: RTPR_KEY not set -- not connecting")
            self._st(last_error="RTPR_KEY not set")
            return
        self.log("wire rtpr: connecting to the alert socket (needs the 'All articles' rule in the rtpr dashboard)")
        while True:
            ws = None
            try:
                ws = create_connection(f"{RTPR_WS}?apiKey={rtpr_key()}", timeout=20, sslopt={"cert_reqs": ssl.CERT_REQUIRED},
                                       header=[f"User-Agent: {USER_AGENT}"])
                self._st(connected=True, ok=True, last_error=None, last_poll_at=datetime.now(timezone.utc).isoformat())
                backoff = 2.0
                ws.settimeout(100)
                while True:
                    try:
                        raw = ws.recv()
                    except Exception as e:
                        if "timed out" in str(e).lower() or "timeout" in type(e).__name__.lower():
                            raise RuntimeError("no ping for 100s")
                        raise
                    if not raw:
                        continue
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if msg.get("type") == "ping":
                        ws.send(json.dumps({"type": "pong"}))
                        self._st(last_poll_at=datetime.now(timezone.utc).isoformat())
                        self._bump("polls")
                        continue
                    try:
                        self.on_frame(msg)
                    except Exception as e:
                        self.log(f"wire rtpr: frame failed: {str(e)[:120]}")
            except Exception as e:
                msg = str(e)[:140]
                self._st(connected=False, ok=False, last_error=msg)
                self._bump("errors"); self._bump("reconnects")
                self.log(f"wire rtpr: {msg} -- reconnecting in {backoff:.0f}s")
            finally:
                try:
                    if ws:
                        ws.close()
                except Exception:
                    pass
            time.sleep(backoff)
            backoff = min(BACKOFF_MAX, backoff * 2)


_threads = []


def start(log=print, publish=None, on_article=None):
    if _state["running"]:
        return
    _state["running"] = True
    for name, url in feeds():
        t = Feed(name, url, log, publish=publish, on_article=on_article)
        _threads.append(t)
        t.start()
    if rtpr_key():
        t = RtprSocket(log, publish=publish, on_article=on_article)
        _threads.append(t)
        t.start()
    log(f"wires: {len(_threads)} sources; the scoreboard is on the Snipe tab")
