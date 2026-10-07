"""The wires: RSS parsing for both feed shapes, ticker extraction, priming, storing, the first-arrival scoreboard;
v2 step 9: the wire fields on every row and SSE payload, a two-ticker release in one row, the same-source copy mark,
and the stream with the Benzinga socket off."""
import os, sys, tempfile, time
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["TAPEHAWK_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
import store; store.init()
os.environ.pop("FMP_API_KEY", None)                    # no key: the size worker never starts
import companies
def _no_net(sym): raise AssertionError(f"the wire path must never fetch a size ({sym})")
companies.fetch_fn = _no_net                           # a regression that fetches on the handle path fails here, with no request sent
import bus, wires, snipe

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
# v2 step 9: the SSE payload carries the wire fields (an RSS release has no rtpr id or impact block, and NO paragraph:
# the RSS description is not the release's first paragraph, and Halthawk's reader calls a story with a paragraph a
# 'release' read and one with only a summary an 'rss' read -- the two must never pool)
assert art["wire_pub"] == "2026-10-02T18:06:00+00:00" and art["paragraph"] is None and art["summary"] == "Acme Therapeutics Receives FDA Approval", art
assert art["author"] is None and art["rtpr_id"] is None and art["impact"] is None and art["copy_of"] is None
# Big News rules (2026-10-07): the first delivery of a release is nobody's dup; the size keys ride along (unknown here)
assert art["dup_of"] is None and art["market_cap"] is None and art["exchange"] is None and art["size_words"] is None, art
gnw_acme_id = art["id"]
row = store.recent(limit=5)[0]
assert row["dup_of"] is None and row["market_cap"] is None and row["size_words"] is None
assert row["source"] == "globenewswire" and row["headline"] == "Acme Therapeutics Announces FDA Approval of Zedox" and row["symbols"] == ["ACME"]
# ... and so does the stored row (/api/feed's shape)
assert row["wire_pub"] == art["wire_pub"] and row["paragraph"] is None and row["summary"] == art["summary"] and row["rtpr_id"] is None and row["impact"] is None and row["copy_of"] is None, row
# a Benzinga row (the old socket's) has a summary but no paragraph either; Benzinga's summary was its own rewrite
assert store.insert({"alpaca_id": "bz:1", "created_at": now.isoformat(), "received_at": now.isoformat(), "headline": "Acme Shares Jump After FDA Nod",
                     "summary": "Benzinga's own rewrite of the release", "source": "benzinga", "symbols": ["ACME"]})
bz = store.get(store.id_for("bz:1"))
assert bz["paragraph"] is None and bz["summary"] == "Benzinga's own rewrite of the release" and bz["source"] == "benzinga", bz
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
# the same Acme release as rtpr's second article id might carry it: the header-line fallback gives it another headline
md_hdr = """Title: Acme Therapeutics: FDA Approves Zedox (Press Release)
Wire: Business Wire
Ticker: ACME

BOSTON--(BUSINESS WIRE)--Acme Therapeutics, Inc. (NASDAQ: ACME) today announced that the U.S. Food and Drug Administration has approved Zedox...
"""
# a different Acme release two minutes later: its own story, never a copy
md_other = """# Acme Therapeutics Appoints Jane Roe as Chief Medical Officer

BOSTON--(BUSINESS WIRE)--Acme Therapeutics, Inc. (NASDAQ: ACME) today announced the appointment of Jane Roe...
"""
# the same Acme release again with a longer headline: six of ten words shared (0.6), just over the half the rule asks
md_near = """# Acme Therapeutics Receives FDA Approval for Zedox in Adult Patients With Hypertension

BOSTON--(BUSINESS WIRE)--Acme Therapeutics, Inc. (NASDAQ: ACME) today announced that the U.S. Food and Drug Administration has approved Zedox...
"""
# a law firm's template: the same headline but for the company name, the same first paragraph, two tickers in one minute
ROSEN = """# ROSEN, A TOP RANKED LAW FIRM, Encourages {name} Investors to Secure Counsel Before Important Deadline

NEW YORK--(BUSINESS WIRE)--Rosen Law Firm, a global investor rights law firm, reminds purchasers of the securities of the company named above of the deadline...
"""
# one company's results release, delivered twice in one minute with the wire named only on a header line: once as
# Business Wire, once as PR Newswire (so a different wire), and once with no wire named at all
FOO_PARA = "AUSTIN, Oct. 5, 2026 -- Foo Corp (NASDAQ: FOO) today reported results for the quarter ended September 30, 2026."
foo_bw = "Title: Foo Corp Reports Third Quarter 2026 Financial Results\nWire: Business Wire\nTicker: FOO\n\n" + FOO_PARA + "\n"
foo_prn = "Title: Foo Corp: Q3 Numbers Are In (Press Release)\nWire: PR Newswire\nTicker: FOO\n\n" + FOO_PARA + "\n"
# ... and Foo's second release of the same minute on the same wire: its own paragraph, so its own story
foo_div = """# Foo Corp Declares Quarterly Dividend

AUSTIN--(BUSINESS WIRE)--Foo Corp (NASDAQ: FOO) today announced that its board of directors declared a quarterly cash dividend...
"""
QUX_PARA = "PARIS, Oct. 5, 2026 -- Qux Inc (NASDAQ: QUX) today announced that its heart monitor has received CE Mark certification..."
qux_bw = "Title: Qux Inc Receives CE Mark for Its Heart Monitor\nWire: Business Wire\nTicker: QUX\n\n" + QUX_PARA + "\n"
qux_bare = "Title: Qux Inc: CE Mark Granted (Press Release)\nTicker: QUX\n\n" + QUX_PARA + "\n"
# a release whose text names no ticker: dropped when the frame names none too
orph = """# Orphan Co Signs Supply Agreement With Big Retailer

NEW YORK--(BUSINESS WIRE)--Orphan Co today announced a multi-year supply agreement with a national retailer...
"""
BODIES = [("boom", None), ("acme_hdr", md_hdr), ("acme_other", md_other), ("acme_near", md_near), ("acme", md),
          ("rosen_alfa", ROSEN.format(name="Alfa Robotics")), ("rosen_brvo", ROSEN.format(name="Bravo Metals")),
          ("foo_bw", foo_bw), ("foo_prn", foo_prn), ("foo_div", foo_div), ("qux_bw", qux_bw), ("qux_bare", qux_bare), ("orph", orph)]
class FakeRtpr(wires.RtprSocket):
    def __init__(self, log, publish=None):
        wires.Source.__init__(self, "rtpr", wires.RTPR_WS, log, publish, None)
        self.primed = True; self.sample_logged = False; self.pairs = set(); self.pair_order = wires.deque(maxlen=5000)
        self._st(connected=True, alerts=0, fetched=0, fetch_errors=0, reconnects=0, merged=0)
    def fetch_article(self, url):
        fetched.append(url)
        for mark, body in BODIES:
            if mark in url:
                if body is None: raise RuntimeError("HTTP 403")
                return body
        return txt
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
# v2 step 9: the SSE payload carries author / wire_pub / rtpr_id / paragraph; the plain frame has no impact block
assert art["wire_pub"] == "2026-10-05T11:30:00Z" and art["rtpr_id"] == "acme_n123" and art["paragraph"].startswith("BOSTON--(BUSINESS WIRE)--Acme"), art
assert art["impact"] is None and art["copy_of"] is None
# Big News rules (2026-10-07): the same Acme release reached GlobeNewswire's feed moments earlier (same stock, five of
# seven words shared), so this rtpr row is marked dup_of that row -- on the SSE payload and the stored row -- and the
# rail (recent with min_importance) shows the release once while the plain feed keeps both
assert art["dup_of"] == gnw_acme_id and store.get(art["id"])["dup_of"] == gnw_acme_id, art
assert any("(also on globenewswire," in l for l in logs), [l for l in logs if "rtpr" in l]
assert art["id"] not in [h["id"] for h in store.recent(min_importance=5)] and art["id"] in [h["id"] for h in store.recent()]
acme_id = art["id"]
# a two-ticker release: the second ticker's frame has the same link; no second fetch, no second row, no second SSE
# message -- the ticker is merged into the stored row, so the one row keeps both tickers
assert r.on_frame(dict(frame, ticker="ZEDX"), t1) == 0 and len(fetched) == 1 and len(pub2) == 1
assert store.get(acme_id)["symbols"] == ["ACME", "ZEDX"], store.get(acme_id)["symbols"]
assert store.recent(limit=1, symbol="ZEDX")[0]["id"] == acme_id                       # /api/feed finds it under either ticker
assert r.on_frame(dict(frame, ticker="ZEDX"), t1) == 0 and store.get(acme_id)["symbols"] == ["ACME", "ZEDX"]   # the very same frame again: nothing
assert store.add_symbol("rtpr:https://rtpr.io/a/acme_n123", "ZEDX") == ["ACME", "ZEDX"] and store.get(acme_id)["symbols"] == ["ACME", "ZEDX"]   # called straight, twice: no growth
assert store.add_symbol("rtpr:no-such-row", "ZEDX") is None
assert wires.status()["merged"] == 1 and wires.status()["feeds"]["rtpr"]["merged"] == 1
hi = {"type": "alert", "alert_kind": "high_impact", "ticker": "SSAT", "impact_score": 92, "impact_tier": "high", "event_type": "contract",
      "impact_direction": "bullish", "article_published_at": "2026-10-05T11:31:00Z", "article_url": "https://rtpr.io/a/ssat_n9?exp=1&sig=x",
      "article_id": "n9"}
assert r.on_frame(hi, t1) == 1 and pub2[-1]["symbols"] == ["SSAT"] and pub2[-1]["author"] == "prnewswire"
# the impact block rides along, over SSE and in the stored row; the frame's own id wins over the link's last part
assert pub2[-1]["impact"] == {"alert_kind": "high_impact", "impact_score": 92, "impact_tier": "high", "event_type": "contract", "impact_direction": "bullish"}
assert pub2[-1]["rtpr_id"] == "n9" and store.get(pub2[-1]["id"])["impact"] == pub2[-1]["impact"] and store.get(pub2[-1]["id"])["rtpr_id"] == "n9"
assert r.on_frame(dict(frame, article_url="https://rtpr.io/a/boom?x=1"), t1) == 0
st = wires.status()["feeds"]["rtpr"]
assert st["alerts"] == 5 and st["fetched"] == 2 and st["fetch_errors"] == 1 and st["stored"] == 0   # stored is counted by poll_once; handle() counts _state["stored"]
assert any(l.startswith("wire rtpr: first article sample >>>") for l in logs)
assert any(l.startswith("wire [rtpr] [ACME]") for l in logs), [l for l in logs if "rtpr" in l]
# and ACME via rtpr matched the earlier globenewswire/benzinga ACME story on the scoreboard? no -- more than 15 minutes apart in the test clock; it is its own first arrival
print("rtpr ok")

# --- the same-source copy mark: rtpr delivers one release under two article ids ---
rtpr_n = wires.scoreboard()["rtpr"]["headlines"]
# (a) a second URL, the same headline, 10 s later -> stored, sent, and marked a copy of the first row; not a new arrival
assert r.on_frame(dict(frame, article_url="https://rtpr.io/a/acme_n124?exp=1&sig=cd"), t1 + timedelta(seconds=10)) == 1
cp = pub2[-1]
assert cp["copy_of"] == acme_id and cp["id"] != acme_id and cp["rtpr_id"] == "acme_n124", cp
assert store.get(cp["id"])["copy_of"] == acme_id and wires.status()["copies"] == 1
assert wires.scoreboard()["rtpr"]["headlines"] == rtpr_n                              # a copy does not count as a headline for the source
assert any("(copy of #%d)" % acme_id in l for l in logs), [l for l in logs if "copy" in l]
# (a2) the headline need not be byte-identical: six of the ten words shared (0.6) is over the half the rule asks
#      (the frame says another publish minute, so only the headline rule can catch this one)
assert r.on_frame(dict(frame, article_url="https://rtpr.io/a/acme_near_n129?e=1", article_published_at="2026-10-05T11:31:00Z"), t1 + timedelta(seconds=20)) == 1
assert pub2[-1]["copy_of"] == acme_id and pub2[-1]["headline"].endswith("Adult Patients With Hypertension"), pub2[-1]
assert wires.status()["copies"] == 2 and wires.scoreboard()["rtpr"]["headlines"] == rtpr_n
# (b) the header-line fallback gives the copy another headline: same ticker, same wire, same publish minute still makes it a copy
assert r.on_frame(dict(frame, article_url="https://rtpr.io/a/acme_hdr_n125?e=1"), t1 + timedelta(seconds=40)) == 1
cp2 = pub2[-1]
assert cp2["headline"].startswith("Acme Therapeutics: FDA Approves Zedox") and cp2["copy_of"] == acme_id, cp2
# (c) a different Acme release in another publish minute, with its own headline: its own story
assert r.on_frame(dict(frame, article_url="https://rtpr.io/a/acme_other_n126?e=1", article_published_at="2026-10-05T11:32:00Z"),
                  t1 + timedelta(seconds=50)) == 1
assert pub2[-1]["copy_of"] is None and pub2[-1]["headline"].startswith("Acme Therapeutics Appoints")
# (d) past the minute, the same headline again is not caught here (Halthawk's first-knowledge rule files it)
assert r.on_frame(dict(frame, article_url="https://rtpr.io/a/acme_n127?e=1"), t1 + timedelta(seconds=130)) == 1 and pub2[-1]["copy_of"] is None
assert wires.status()["copies"] == 3
# --- a copy always names the same stock, and the minute rule needs the same first paragraph and wire ---
t3 = t1 + timedelta(seconds=400)
rtpr_n = wires.scoreboard()["rtpr"]["headlines"]
def rtpr_row(mark):
    return store.get(store.id_for(f"rtpr:https://rtpr.io/a/{mark}"))
# (e) a law firm's template run: the same headline but for the company name, the SAME first paragraph, one wire, one
#     minute, 5 s apart -- two companies, so two stories, both on the scoreboard (not a copy by headline or by minute)
alfa = dict(frame, ticker="ALFA", article_url="https://rtpr.io/a/rosen_alfa?e=1", article_published_at="2026-10-05T12:42:00Z")
brvo = dict(frame, ticker="BRVO", article_url="https://rtpr.io/a/rosen_brvo?e=1", article_published_at="2026-10-05T12:42:00Z")
assert r.on_frame(alfa, t3) == 1 and r.on_frame(brvo, t3 + timedelta(seconds=5)) == 1
assert rtpr_row("rosen_alfa")["copy_of"] is None and rtpr_row("rosen_brvo")["copy_of"] is None
assert rtpr_row("rosen_alfa")["symbols"] == ["ALFA"] and rtpr_row("rosen_brvo")["symbols"] == ["BRVO"]
assert wires.scoreboard()["rtpr"]["headlines"] == rtpr_n + 2 and wires.status()["copies"] == 3
# (f) the same company's results, same minute, same first paragraph, the wire named on a header line: Business Wire
#     then PR Newswire is a different wire, so not a copy; the same minute on the same wire with a different paragraph
#     (Foo's dividend) is Foo's own second release, not a copy either
foo = dict(frame, ticker="FOO", article_published_at="2026-10-05T12:40:00Z")
assert r.on_frame(dict(foo, article_url="https://rtpr.io/a/foo_bw?e=1"), t3 + timedelta(seconds=10)) == 1
assert r.on_frame(dict(foo, article_url="https://rtpr.io/a/foo_prn?e=1"), t3 + timedelta(seconds=20)) == 1
assert rtpr_row("foo_bw")["author"] == "businesswire" and rtpr_row("foo_prn")["author"] == "prnewswire"
assert rtpr_row("foo_prn")["copy_of"] is None and rtpr_row("foo_prn")["paragraph"] == rtpr_row("foo_bw")["paragraph"] == FOO_PARA
assert r.on_frame(dict(foo, article_url="https://rtpr.io/a/foo_div?e=1"), t3 + timedelta(seconds=30)) == 1
assert rtpr_row("foo_div")["copy_of"] is None and rtpr_row("foo_div")["author"] == "businesswire", rtpr_row("foo_div")
assert wires.scoreboard()["rtpr"]["headlines"] == rtpr_n + 5 and wires.status()["copies"] == 3
# (g) ... but when one delivery names no wire at all (no header line, no mark in the text) the wire is not held against
#     the pair: same ticker, same minute, same paragraph -> a copy, under the header-line fallback's other headline
qux = dict(frame, ticker="QUX", article_published_at="2026-10-05T12:41:00Z")
assert r.on_frame(dict(qux, article_url="https://rtpr.io/a/qux_bw?e=1"), t3 + timedelta(seconds=40)) == 1
assert r.on_frame(dict(qux, article_url="https://rtpr.io/a/qux_bare?e=1"), t3 + timedelta(seconds=50)) == 1
assert rtpr_row("qux_bare")["author"] is None and rtpr_row("qux_bare")["copy_of"] == rtpr_row("qux_bw")["id"], rtpr_row("qux_bare")
assert wires.scoreboard()["rtpr"]["headlines"] == rtpr_n + 6 and wires.status()["copies"] == 4
# --- a frame with no ticker for a release whose text names none is dropped; a later frame that names one brings it back ---
before = (wires.status()["no_ticker"], len(fetched))
assert r.on_frame({"type": "alert", "article_url": "https://rtpr.io/a/orph?e=1", "article_published_at": "2026-10-05T12:45:00Z"}, t3) == 0
assert wires.status()["no_ticker"] == before[0] + 1 and len(fetched) == before[1] + 1 and store.id_for("rtpr:https://rtpr.io/a/orph") is None
assert r.on_frame({"type": "alert", "ticker": "ORPH", "article_url": "https://rtpr.io/a/orph?e=1", "article_published_at": "2026-10-05T12:45:00Z"}, t3) == 1
assert len(fetched) == before[1] + 2 and rtpr_row("orph")["symbols"] == ["ORPH"] and rtpr_row("orph")["copy_of"] is None
assert any("ORPH frame for a release that was not stored" in l for l in logs), [l for l in logs if "ORPH" in l]
assert r.on_frame({"type": "alert", "ticker": "ORPH", "article_url": "https://rtpr.io/a/orph?e=1"}, t3) == 0 and len(fetched) == before[1] + 2   # and now it is a repeat
# an RSS feed: the same headline under two guids within a minute is a copy, but two DIFFERENT releases from one company
# in the same publish minute are two stories (the minute rule is rtpr's alone: RSS headlines are the real ones)
published.clear()
g2 = wires.Feed("globenewswire", "http://x/gnw2", logs.append, publish=published.append); g2.primed = True
A = GNW.replace("3373999", "3380001")
B = GNW.replace("3373999", "3380002")
C = GNW.replace("3373999", "3380003").replace("Acme Therapeutics Receives FDA Approval for Zedox in Adults", "Acme Therapeutics to Ring the Nasdaq Opening Bell")
t2 = datetime.now(timezone.utc)
assert g2.handle(wires.parse_feed(A), t2) == 2 and [p["copy_of"] for p in published] == [None, None]   # Acme and the Widget buyout (new to this source)
assert g2.handle(wires.parse_feed(B), t2 + timedelta(seconds=20)) == 1 and published[-1]["copy_of"] == published[0]["id"]
assert g2.handle(wires.parse_feed(C), t2 + timedelta(seconds=30)) == 1 and published[-1]["copy_of"] is None, published[-1]
# two companies' template headlines ("X Announces Pricing of Public Offering": three of four words shared) 5 s apart
# are two stories: a copy names the same stock
gnw_n = wires.scoreboard()["globenewswire"]["headlines"]
D = GNW.replace("3373999", "3380004").replace("Acme Therapeutics Receives FDA Approval for Zedox in Adults", "Acme Announces Pricing of Public Offering")
E = GNW.replace("3373999", "3380005").replace("Nasdaq:ACME", "Nasdaq:BETA").replace("Acme Therapeutics Receives FDA Approval for Zedox in Adults", "Beta Announces Pricing of Public Offering")
assert g2.handle(wires.parse_feed(D), t2 + timedelta(seconds=35)) == 1 and published[-1]["symbols"] == ["ACME"] and published[-1]["copy_of"] is None
assert g2.handle(wires.parse_feed(E), t2 + timedelta(seconds=40)) == 1 and published[-1]["symbols"] == ["BETA"] and published[-1]["copy_of"] is None, published[-1]
assert wires.scoreboard()["globenewswire"]["headlines"] == gnw_n + 2
print("copies ok")

# --- the Benzinga socket is off: a GNW release still reaches an SSE listener, and the stream reports the wires ---
import app, news_stream
assert not news_stream.status()["enabled"] and not news_stream.status()["connected"] and "off" in news_stream.status()["note"]
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app.py"), encoding="utf-8").read()
assert "news_stream.start(" not in src and "bus.subscribe()" in src and "publish=bus.publish" in src
assert news_stream.subscribe is bus.subscribe and news_stream._publish is bus.publish       # the old names still point at the one stream
class FakeFeed(wires.Feed):
    """A GlobeNewswire poll that answers from a string instead of the network."""
    xml = GNW
    def fetch(self):
        return self.xml
listener = bus.subscribe()
f3 = FakeFeed("globenewswire", "http://x/gnw3", logs.append, publish=bus.publish)
assert f3.poll_once() == 0 and f3.primed                                                   # primed: history, nothing sent
assert wires.stream_status()["connected"] and "globenewswire" in wires.stream_status()["sources_up"]
f3.xml = GNW.replace("3373999", "3390000").replace("Acme Therapeutics Receives FDA Approval for Zedox in Adults", "Acme Therapeutics Gets FDA Nod for Zedox in Children")
assert f3.poll_once() == 1
got = listener.get_nowait()
assert got["headline"] == "Acme Therapeutics Gets FDA Nod for Zedox in Children" and got["source"] == "globenewswire" and got["wire_pub"], got
assert listener.empty()
bus.unsubscribe(listener)
app._started_pid = os.getpid()                 # the request hook would otherwise start every background thread (and the network) here
c = app.app.test_client()
d = c.get("/api/feed?limit=3").get_json()
s = d["stream"]
assert s["source"] == "wires" and s["benzinga"] == "off" and s["connected"] and "globenewswire" in s["sources"] and "rtpr" in s["sources"], s
assert s["last_message_at"] and s["stored"] == wires.status()["stored"]
assert d["headlines"][0]["headline"] == got["headline"] and d["headlines"][0]["wire_pub"] == got["wire_pub"]
assert d["headlines"][0]["paragraph"] is None and d["headlines"][0]["summary"]        # an RSS row over /api/feed: summary, no paragraph
st = c.get("/api/status").get_json()
assert st["stream"]["source"] == "wires" and st["benzinga"]["enabled"] is False and "listeners" in st["bus"]
sn = c.get("/api/snipe").get_json()
assert sn["stream"]["source"] == "wires" and sn["wires"]["status"]["running"] is False
print("stream ok")
