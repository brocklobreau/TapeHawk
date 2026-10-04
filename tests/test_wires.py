"""The wires: RSS parsing for both feed shapes, ticker extraction, priming, storing, the first-arrival scoreboard."""
import os, sys, tempfile, time
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["TAPEHAWK_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
import store; store.init()
import wires, snipe

GNW = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:dc="http://dublincore.org/documents/dcmi-namespace/" xmlns:media="http://search.yahoo.com/mrss/">
<channel><title>GlobeNewswire - News about Public Companies</title>
<item>
  <guid isPermaLink="true">https://www.globenewswire.com/news-release/2026/10/02/3373999/0/en/acme.html</guid>
  <link>https://www.globenewswire.com/news-release/2026/10/02/3373999/0/en/acme.html</link>
  <category domain="https://www.globenewswire.com/rss/stock">Nasdaq:ACME</category>
  <category domain="https://www.globenewswire.com/rss/ISIN">US0000000001</category>
  <title>Acme Therapeutics Receives FDA Approval for Zedox in Adults</title>
  <description><![CDATA[<p><b>Acme Therapeutics Receives FDA Approval</b></p>]]></description>
  <pubDate>Fri, 02 Oct 2026 18:06 GMT</pubDate>
  <dc:identifier>3373999</dc:identifier>
</item>
<item>
  <guid>https://www.globenewswire.com/news-release/2026/10/02/3373972/0/en/bcp.html</guid>
  <category domain="https://www.globenewswire.com/rss/stock">Euronext Lisbon:BCP</category>
  <title>Banco Comercial Portugu&#234;s informs about share buy-back</title>
  <description><![CDATA[<p>...</p>]]></description>
  <pubDate>Fri, 02 Oct 2026 18:06 GMT</pubDate>
</item>
<item>
  <guid>https://www.globenewswire.com/news-release/2026/10/02/3374000/0/en/widget.html</guid>
  <category domain="https://www.globenewswire.com/rss/stock">NYSE American:WDGT</category>
  <category domain="https://www.globenewswire.com/rss/stock">TSX:WDG</category>
  <title>Widget Corp to Be Acquired by Giant Inc for $12.50 Per Share in Cash</title>
  <description><![CDATA[<p>Widget Corp (NYSE American: WDGT) ...</p>]]></description>
  <pubDate>Fri, 02 Oct 2026 18:07 GMT</pubDate>
</item>
</channel></rss>"""

PRN = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:prn="http://www.prnewswire.com/ns/0.1/#" xmlns:media="http://search.yahoo.com/mrss/">
<channel><title>PR Newswire: News Releases</title>
<item>
  <title>SmallSat Wins $40 Million Navy Contract</title>
  <link>https://www.prnewswire.com/news-releases/smallsat-302897300.html</link>
  <guid>https://www.prnewswire.com/news-releases/smallsat-302897300.html</guid>
  <pubDate>Fri, 2 Oct 2026 18:33:00 +0000</pubDate>
  <description><![CDATA[<p>DENVER, Oct. 2, 2026 /PRNewswire/ -- SmallSat Inc. (NASDAQ: SSAT), a maker of small satellites, today announced it has been awarded a $40 million contract by the U.S. Navy...</p>]]></description>
  <dc:publisher>PR Newswire Association LLC.</dc:publisher>
</item>
<item>
  <title>Poppins Payroll Company Data Breach Alert Issued By Wolf Haldenstein</title>
  <link>https://www.prnewswire.com/news-releases/poppins-302897299.html</link>
  <guid>https://www.prnewswire.com/news-releases/poppins-302897299.html</guid>
  <pubDate>Fri, 2 Oct 2026 18:33:00 +0000</pubDate>
  <description><![CDATA[<p>NEW YORK, Oct. 2, 2026 /PRNewswire/ -- Wolf Haldenstein is investigating claims...</p>]]></description>
</item>
<item>
  <title>Two Tickers Inc. and Other Co. Announce Merger</title>
  <link>https://www.prnewswire.com/news-releases/two-302897301.html</link>
  <guid>https://www.prnewswire.com/news-releases/two-302897301.html</guid>
  <pubDate>Fri, 2 Oct 2026 18:34:00 +0000</pubDate>
  <description><![CDATA[<p>Two Tickers Inc. (NYSE: TWO) and Other Co. (OTCQB: OTHR) and Pink Ltd (TSXV: PNK) ...</p>]]></description>
</item>
</channel></rss>"""

# --- parsing ---
g = wires.parse_feed(GNW)
assert [i["tickers"] for i in g] == [["ACME"], [], ["WDGT"]], [i["tickers"] for i in g]
assert g[0]["pub"].startswith("2026-10-02T18:06:00+00:00") and g[0]["description"] == "Acme Therapeutics Receives FDA Approval"
assert g[1]["title"] == "Banco Comercial Português informs about share buy-back" and g[1]["exchanges_seen"] == ["Euronext Lisbon:BCP"]
p = wires.parse_feed(PRN)
assert [i["tickers"] for i in p] == [["SSAT"], [], ["TWO"]], [i["tickers"] for i in p]
assert p[0]["pub"] == "2026-10-02T18:33:00+00:00" and "SmallSat Inc. (NASDAQ: SSAT)" in p[0]["description"]
assert wires.tickers_in("Foo (Nasdaq: FOO) and Bar (NYSE Arca: BAR) and Baz (OTC: BAZ) and (NYSE: FOO)") == ["FOO", "BAR"]

# --- a feed thread, driven by hand: primed first, then stores the new ones with a ticker ---
published = []
logs = []
f = wires.Feed("globenewswire", "http://x/gnw", logs.append, publish=published.append)
now = datetime.now(timezone.utc)
assert f.handle(wires.parse_feed(GNW), now) == 0 and not f.primed
f.primed = True
assert f.handle(wires.parse_feed(GNW), now) == 0                        # nothing new: all three were on the feed at boot
NEW = GNW.replace("3373999", "3375000").replace("Acme Therapeutics Receives FDA Approval for Zedox in Adults", "Acme Therapeutics Announces FDA Approval of Zedox")
n = f.handle(wires.parse_feed(NEW), now)
assert n == 1 and len(published) == 1, (n, published)
art = published[0]
assert art["source"] == "globenewswire" and art["symbols"] == ["ACME"] and art["alpaca_id"].startswith("globenewswire:") and art["id"]
assert art["created_at"] == now.isoformat() and "importance" in art and "tone" in art and art["categories"], art
row = store.recent(limit=5)[0]
assert row["source"] == "globenewswire" and row["headline"] == "Acme Therapeutics Announces FDA Approval of Zedox" and row["symbols"] == ["ACME"]
assert f.handle(wires.parse_feed(NEW), now) == 0                        # seen guids never repeat
assert wires.status()["stored"] == 1 and wires.status()["no_ticker"] == 0
# a PR Newswire item with no ticker is dropped and counted
f2 = wires.Feed("prnewswire", "http://x/prn", logs.append, publish=published.append); f2.primed = True
assert f2.handle(wires.parse_feed(PRN), now) == 2 and wires.status()["no_ticker"] == 1
assert any(l.startswith("wire [globenewswire] [ACME]") and "(first)" in l for l in logs), logs

# --- the scoreboard: Benzinga arrives 23s after the wire with a rewritten headline on the same ticker ---
t0 = now.timestamp()
m = wires.note_arrival("benzinga", ["ACME"], "Acme Therapeutics Shares Jump After FDA Approves Zedox", t0 + 23, 99)
assert m and m["first"] == "globenewswire" and m["lag_s"] == 23.0, m
sb = wires.scoreboard()
assert sb["globenewswire"]["first"] == 1 and sb["benzinga"]["second"] == 1 and sb["benzinga"]["median_lag_s"] == 23.0
assert sb["benzinga"]["examples"][0]["symbol"] == "ACME" and sb["benzinga"]["examples"][0]["first"] == "globenewswire"
# the other way round: Benzinga first on a different stock, the wire 40s later
wires.note_arrival("benzinga", ["ZETA"], "Zeta Announces Positive Phase 3 Results", t0 + 100, 100)
m2 = wires.note_arrival("prnewswire", ["ZETA"], "Zeta Therapeutics Announces Positive Topline Results From Phase 3 Trial", t0 + 140, 101)
assert m2["first"] == "benzinga" and m2["lag_s"] == 40.0
sb = wires.scoreboard()
assert sb["benzinga"]["first"] == 1 and sb["prnewswire"]["second"] == 1 and sb["prnewswire"]["median_lag_s"] == 40.0
# no match: different ticker, or too old
assert wires.note_arrival("benzinga", ["QQQQ"], "Unrelated", t0 + 150) is None
assert wires.note_arrival("benzinga", ["ACME"], "Acme again, an hour later", t0 + 3700) is None
assert wires.status()["matched"] == 2

# --- the Snipe tab takes a wire article like any other ---
s = snipe.on_headline(art, now_s=t0 + 1)
assert s and s["symbol"] == "ACME" and s["category"] == "fda"
print("wires ok")

# --- RTPR: the article parser in the shapes it might come in, and the socket frames ---
P = wires.parse_rtpr_article
md = """# Acme Therapeutics Receives FDA Approval for Zedox in Adults

**Ticker:** ACME · **Published:** 2026-10-05T11:30:00Z

BOSTON--(BUSINESS WIRE)--Acme Therapeutics, Inc. (NASDAQ: ACME) today announced that the U.S. Food and Drug Administration has approved Zedox...

| Q3 | Q2 |
|----|----|
"""
a = P(md)
assert a["headline"] == "Acme Therapeutics Receives FDA Approval for Zedox in Adults" and a["wire"] == "businesswire", a
assert a["paragraph"].startswith("BOSTON--(BUSINESS WIRE)--Acme") and a["header"].get("ticker", "").startswith("ACME")
txt = """Title: SmallSat Wins $40 Million Navy Contract
Wire: PR Newswire
Published: 2026-10-05 11:31:00Z
Ticker: SSAT

DENVER, Oct. 5, 2026 /PRNewswire/ -- SmallSat Inc. (NASDAQ: SSAT) today announced...
"""
b = P(txt)
assert b["headline"] == "SmallSat Wins $40 Million Navy Contract" and b["wire"] == "prnewswire" and b["paragraph"].startswith("DENVER"), b
plain = "Widget Corp to Be Acquired for $12.50 Per Share in Cash\n\nTORONTO, Oct. 5, 2026 (GLOBE NEWSWIRE) -- Widget Corp (NYSE American: WDGT)...\n"
c = P(plain)
assert c["headline"].startswith("Widget Corp to Be Acquired") and c["wire"] == "globenewswire"
assert P("")["headline"] == "" and P("NEW YORK / ACCESSWIRE / Oct 5, 2026 / Foo (NASDAQ: FOO)")["wire"] == "accesswire"

# frames through the socket source with a fake article fetch
fetched = []
class FakeRtpr(wires.RtprSocket):
    def __init__(self, log, publish=None):
        wires.Source.__init__(self, "rtpr", wires.RTPR_WS, log, publish, None)
        self.primed = True; self.sample_logged = False; self._st(connected=True, alerts=0, fetched=0, fetch_errors=0, reconnects=0)
    def fetch_article(self, url):
        fetched.append(url)
        if "boom" in url: raise RuntimeError("HTTP 403")
        return md if "acme" in url else txt
pub2 = []
r = FakeRtpr(logs.append, publish=pub2.append)
t1 = datetime.now(timezone.utc)
frame = {"type": "alert", "ticker": "ACME", "rules": [{"rule_name": "All articles"}], "article_published_at": "2026-10-05T11:30:00Z",
         "article_url": "https://rtpr.io/a/acme_n123?exp=1&sig=ab"}
assert r.on_frame({"type": "ping"}) is None
assert r.on_frame(frame, t1) == 1 and fetched == ["https://rtpr.io/a/acme_n123?exp=1&sig=ab"] and len(pub2) == 1
art = pub2[0]
assert art["source"] == "rtpr" and art["author"] == "businesswire" and art["symbols"] == ["ACME"] and art["alpaca_id"] == "rtpr:https://rtpr.io/a/acme_n123"
assert art["headline"] == "Acme Therapeutics Receives FDA Approval for Zedox in Adults" and art["summary"].startswith("BOSTON")
assert r.on_frame(dict(frame, ticker="ACMEW"), t1) == 0 and len(fetched) == 1          # same link, second ticker: one release
hi = {"type": "alert", "alert_kind": "high_impact", "ticker": "SSAT", "impact_score": 92, "impact_tier": "high", "event_type": "contract",
      "impact_direction": "bullish", "article_published_at": "2026-10-05T11:31:00Z", "article_url": "https://rtpr.io/a/ssat_n9?exp=1&sig=x"}
assert r.on_frame(hi, t1) == 1 and pub2[-1]["symbols"] == ["SSAT"] and pub2[-1]["author"] == "prnewswire"
assert r.on_frame(dict(frame, article_url="https://rtpr.io/a/boom?x=1"), t1) == 0
st = wires.status()["feeds"]["rtpr"]
assert st["alerts"] == 4 and st["fetched"] == 2 and st["fetch_errors"] == 1 and st["stored"] == 0   # stored is counted by poll_once; handle() counts _state["stored"]
assert any(l.startswith("wire rtpr: first article sample >>>") for l in logs)
assert any(l.startswith("wire [rtpr] [ACME]") for l in logs), [l for l in logs if "rtpr" in l]
# and ACME via rtpr matched the earlier globenewswire/benzinga ACME story on the scoreboard? no -- more than 15 minutes apart in the test clock; it is its own first arrival
print("rtpr ok")
