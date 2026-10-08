"""Size guards (2026-10-07, evening): a company size is trusted only when FMP's company is the release's company.
Live facts from the first hour: FMP answers NOVN = "Novan, Inc." $2.6M NASDAQ (Novartis's SIX: NOVN release was sized
as that), PARA = "Banzai International, Inc. Class A" $2.0M (the release was Parabolic's), TOPS = "Top Ships Inc." $2.4M
AMEX (right), ICU = "SeaStar Medical Holding Corporation" $10.6M (right), WOLF = "Wolfspeed Inc." $1.63B NYSE (right)."""
import os, sys, tempfile
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["TAPEHAWK_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.pop("FMP_API_KEY", None)                    # no key: the worker never starts; tests drive the queue by hand
import store; store.init()
import classify, companies, wires

def _no_net(sym): raise AssertionError(f"the wire path must never fetch a size ({sym})")
companies.fetch_fn = _no_net
companies._sleep = lambda s: None
logs = []

# --- 1. exchange_tags / non_us_tag ----------------------------------------------------------------------------
ET, NT = classify.exchange_tags, classify.non_us_tag
assert ET("Basel, October 7, 2026 -- Novartis (SIX: NOVN) today announced") == {"NOVN": ["SIX"]}
assert ET("SIX:NOVN") == {"NOVN": ["SIX"]}
FFH = "Fairfax Financial Holdings Limited (TSX: FFH) and (NYSE: FRFHF) announces"
assert ET(FFH) == {"FFH": ["TSX"], "FRFHF": ["NYSE"]}, ET(FFH)
assert NT(FFH, "FFH") == "TSX" and NT(FFH, "FRFHF") is None
assert ET("Acme Corp (Nasdaq: ACME) today") == {"ACME": ["NASDAQ"]} and NT("Acme Corp (Nasdaq: ACME)", "ACME") is None
assert ET("no tag in this text at all") == {} and NT("no tag", "ACME") is None and NT("(SIX: NOVN)", "") is None
many = ET("(Euronext Paris: AIR) (Nasdaq Stockholm: VOLV) (OTCQB: ABCD) (Pink: XYZQ) (HKEX: 0700) (LSE: BP.) (NYSE American: AMX) (Oslo Børs: EQNR)")
assert many == {"AIR": ["Euronext Paris"], "VOLV": ["Nasdaq Stockholm"], "ABCD": ["OTCQB"], "XYZQ": ["OTC Pink"], "0700": ["HKEX"],
                "BP": ["LSE"], "AMX": ["NYSE American"], "EQNR": ["Oslo Børs"]}, many
assert NT("Acme (NASDAQ: ACME) (TSX: ACME)", "ACME") is None                 # tagged under a US exchange too: FMP is consulted
assert NT("Acme (TSX: ACME) (NASDAQ: ACME)", "acme") is None
assert NT("Acme (TSX: ACME, ACMW)", "ACME") == "TSX"
for label in ("SIX", "TSX", "Euronext Paris", "OTCQB", "OTC Pink", "Nasdaq Stockholm"):
    assert classify.exchange_verdict(label), label                             # every non-US label is a verdict
assert classify.exchange_verdict("SIX") == "not US-listed (SIX)" and classify.exchange_verdict("OTC Pink") == "over the counter: not tradable here"
assert classify.exchange_verdict("NYSE American") is None and classify.exchange_verdict("NASDAQ") is None
print("exchange tags ok")

# --- 2. name_matches: the suffix list, the first word left, whole words, punctuation -----------------------------
NM, NK = classify.name_matches, classify.name_key
NOVARTIS = "Novartis receives FDA approval for Zedox in adults. Basel -- Novartis (SIX: NOVN) today announced"
PARA_H = "Parabolic Announces Preliminary Q3 2026 Revenue of $4.7 Million"
assert not NM("Novan, Inc.", NOVARTIS) and NK("Novan, Inc.") == "novan"
assert not NM("Banzai International, Inc. Class A", PARA_H) and NK("Banzai International, Inc. Class A") == "banzai"
assert NM("Top Ships Inc.", "Top Ships Inc. Announces Acquisition of Two Vessels")
assert NM("SeaStar Medical Holding Corporation", "FDA Grants SeaStar Medical Breakthrough Device Designation for Its Device")
assert NM("Wolfspeed Inc.", "Wolfspeed Announces Fiscal 2027 First Quarter Results")
assert NM("SRX Global Inc.", "SRX Global Inc. Announces a Contract") and NM("SeaStar Medical Holding Corporation", "seastar wins")
assert NM("The Boeing Company", "Boeing Wins $300 Million Award From U.S. Army") and NK("The Boeing Company") == "boeing"
assert NM("Alphabet Inc. Class A", "Alphabet Announces Third Quarter Results") and NK("Alphabet Inc. Class A") == "alphabet"
assert NM("Hims & Hers Health, Inc.", "Hims & Hers Expands Weight Loss Offering") and NK("Hims & Hers Health, Inc.") == "hims"
assert not NM("Top Ships Inc.", "Laptop Maker Announces Results")             # a whole word, not a part of one
assert not NM("Novan, Inc.", "") and not NM(None, "Novan announces") and not NM("Inc. Corp", "Inc") and NK("Inc. Corp") is None
assert NK("") is None and NK(None) is None and NK("A10 Networks, Inc.") == "a10"
SPEC = {"inc", "corp", "corporation", "co", "company", "holdings", "holding", "group", "ltd", "limited", "plc", "sa", "ag", "nv",
        "se", "llc", "lp", "class", "a", "b", "the", "trust", "fund", "international", "technologies", "therapeutics", "pharmaceuticals"}
assert SPEC <= classify.NAME_SUFFIXES, SPEC - classify.NAME_SUFFIXES          # the spec's whole list, so a deleted word shows up
for w in SPEC:
    assert NK(f"{w.title()} Zedox Inc.") == "zedox", w                         # a leading suffix word is skipped
assert NK("The Company Store, Inc.") == "store"
assert NM("Company Zedox Holdings", "Zedox wins a contract")
print("name match ok")

# --- 3. size_from: the guards at the one door -----------------------------------------------------------------
NOVAN = {"symbol": "NOVN", "name": "Novan, Inc.", "market_cap": 2.6e6, "price": 0.0941, "exchange": "NASDAQ", "error": None}
NVS = {"symbol": "NVS", "name": "Novartis AG", "market_cap": 200e9, "price": 110.0, "exchange": "NYSE", "error": None}
BANZAI = {"symbol": "PARA", "name": "Banzai International, Inc. Class A", "market_cap": 2.0e6, "price": 1.0, "exchange": "NASDAQ", "error": None}
TOPS = {"symbol": "TOPS", "name": "Top Ships Inc.", "market_cap": 2.4e6, "price": 1.0, "exchange": "AMEX", "error": None}
ICU = {"symbol": "ICU", "name": "SeaStar Medical Holding Corporation", "market_cap": 10.6e6, "price": 1.0, "exchange": "NASDAQ", "error": None}
WOLF = {"symbol": "WOLF", "name": "Wolfspeed Inc.", "market_cap": 1.63e9, "price": 20.0, "exchange": "NYSE", "error": None}
head = "Novartis receives FDA approval for Zedox in adults"
para = "Basel, October 7, 2026 -- Novartis (SIX: NOVN) today announced that the FDA has approved Zedox."
# guard a: the SIX tag wins over FMP's Novan; the scorer makes it not big and names SIX
rs = []
assert store.size_from(NOVAN, store.row_text(head, para), "NOVN", rs, log=logs.append) == (None, "SIX") and rs == []
imp = classify.importance(head, ["NOVN"], None, market_cap=None, exchange="SIX")
assert not imp["big"] and imp["score"] == 7 and "not US-listed (SIX)" in imp["reasons"], imp
# the same release naming (NYSE: NVS) for ticker NVS: FMP consulted as before
rs = []
assert store.size_from(NVS, store.row_text(head, "Basel -- Novartis (NYSE: NVS) today announced"), "NVS", rs, log=logs.append) == (200e9, "NYSE") and rs == []
# guard b: FMP's company is not named in the release -> no adjustment, the reason says so, logged once
rs = []
assert store.size_from(BANZAI, store.row_text(PARA_H, None), "PARA", rs, log=logs.append) == (None, None)
assert rs == ["size unknown: FMP's PARA is Banzai International, Inc. Class A, not this company"], rs
assert store.size_from(BANZAI, store.row_text(PARA_H, None), "PARA", rs, log=logs.append) == (None, None) and len(rs) == 1
assert sum("FMP's PARA is Banzai International" in l for l in logs) == 1, logs
rs = []
assert store.size_from(NOVAN, store.row_text(head, "Basel -- Novartis today announced"), "NOVN", rs, log=logs.append) == (None, None)
assert rs == ["size unknown: FMP's NOVN is Novan, Inc., not this company"], rs
assert store.size_from(TOPS, "Top Ships Inc. Announces Acquisition of Two Vessels", "TOPS", rs) == (2.4e6, "AMEX")
assert store.size_from(ICU, "FDA Grants SeaStar Medical Breakthrough Device Designation", "ICU", rs) == (10.6e6, "NASDAQ")
assert store.size_from(WOLF, "Wolfspeed Announces Results", "WOLF", rs) == (1.63e9, "NYSE") and len(rs) == 1
rs = []
assert store.size_from({"name": None, "market_cap": 5e6, "exchange": "NASDAQ", "error": None}, "Acme wins", "ACME", rs) == (None, None)
assert rs == ["size unknown: FMP gave no company name for ACME"]
# without the text the answers are what they were; an error row and the no-listing answer as before
assert store.size_from(NOVAN) == (2.6e6, "NASDAQ") and store.size_from(None) == (None, None) and store.size_from(None, "x", "X") == (None, None)
assert store.size_from({"error": companies.NO_LISTING, "exchange": "NONE"}, "Novartis (SIX: NOVN)", "NOVN") == (None, "SIX")
assert store.size_from({"error": companies.NO_LISTING, "exchange": "NONE"}, "Novartis", "NOVN") == (None, "NONE")
assert store.size_from({"error": "stale quote", "name": "Novan, Inc.", "exchange": "NASDAQ"}, "Novan announces", "NOVN") == (None, None)
print("size_from ok")

# --- 4. the handle path: the guards on a live release, no fetch, no wait --------------------------------------
store.upsert_company("NOVN", "Novan, Inc.", 2.6e6, 0.0941, "NASDAQ", "US", None)
store.upsert_company("PARA", "Banzai International, Inc. Class A", 2.0e6, 1.0, "NASDAQ", "US", None)
store.upsert_company("TOPS", "Top Ships Inc.", 2.4e6, 1.0, "AMEX", "US", None)
published, wlogs = [], []
store._mismatch_said.clear()                           # section 3 logged the PARA pair already; the handle path logs it once more here
feed = wires.Feed("prnewswire", "http://x/prn", wlogs.append, publish=published.append); feed.primed = True
t0 = datetime.now(timezone.utc)
def item(guid, title, desc, tickers):
    return {"guid": guid, "title": title, "link": "https://x/" + guid, "pub": t0.isoformat(), "description": desc,
            "tickers": tickers, "exchanges_seen": []}
TOPS_H = "Top Ships Inc. Announces Acquisition of Two Vessels for $40 Million"
assert feed.handle([item("novn-1", head, para, ["NOVN"]),
                    item("para-1", PARA_H, "NEW YORK -- Parabolic (NASDAQ: PARA) today reported...", ["PARA"]),
                    item("tops-1", TOPS_H, "ATHENS -- Top Ships Inc. (NYSE American: TOPS) today announced...", ["TOPS"])], t0) == 3
nv, pb, tp = published
assert nv["symbols"] == ["NOVN"] and nv["exchange"] == "SIX" and nv["market_cap"] is None and nv["size_words"] is None, nv
assert not nv["big"] and nv["importance"] == 7 and "not US-listed (SIX)" in nv["reasons"], nv
assert pb["market_cap"] is None and pb["exchange"] is None and pb["size_words"] is None and pb["importance"] == classify.importance(PARA_H, ["PARA"])["score"], pb
assert "size unknown: FMP's PARA is Banzai International, Inc. Class A, not this company" in pb["reasons"], pb
assert tp["market_cap"] == 2.4e6 and tp["exchange"] == "AMEX" and tp["size_words"] == "$2M company" and tp["big"], tp
assert "worth half the company or more" in tp["reasons"] and any("small company" in r for r in tp["reasons"]), tp
rows = {r["headline"]: r for r in store.recent(limit=10)}
assert rows[head]["exchange"] == "SIX" and rows[head]["market_cap"] is None and not rows[head]["big"]
assert rows[PARA_H]["market_cap"] is None and rows[PARA_H]["exchange"] is None and "not this company" in " ".join(rows[PARA_H]["reasons"])
assert rows[TOPS_H]["market_cap"] == 2.4e6 and rows[TOPS_H]["big"]
rail = [h["id"] for h in store.recent(min_importance=5)]
assert tp["id"] in rail and nv["id"] not in rail, rail
assert sum("FMP's PARA is Banzai International" in l for l in wlogs) == 1, wlogs      # logged once (the module remembers)
feed.handle([item("para-2", "Parabolic Announces a Second Thing", "Parabolic (NASDAQ: PARA)", ["PARA"])], t0)
assert sum("FMP's PARA is Banzai International" in l for l in wlogs) == 1
assert "NOVN" not in companies._queued and "PARA" not in companies._queued      # fresh cache rows: nothing queued, nothing fetched
print("handle path ok")

# --- 5. guard c: a dead quote is not a live listing -----------------------------------------------------------
assert companies.STALE_QUOTE_DAYS == 7 and companies.retry_hours(companies.STALE_QUOTE) == 24
now_s = datetime.now(timezone.utc).timestamp()
real_get = companies._get
answers = {}
companies._get = lambda path, sym: answers.get(path)
answers = {"quote": {"name": "Novan, Inc.", "marketCap": 2.6e6, "price": 0.0941, "exchange": "NASDAQ", "timestamp": int(now_s - 30 * 86400)}}
r = companies._fetch_fmp("NOVN")
assert r["error"] == companies.STALE_QUOTE and r["name"] == "Novan, Inc." and r["exchange"] == "NASDAQ", r
answers = {"quote": {"name": "Acme", "marketCap": 1e9, "price": 5.0, "exchange": "NYSE", "timestamp": int(now_s - 3600)}}
assert companies._fetch_fmp("ACME") == {"name": "Acme", "market_cap": 1e9, "price": 5.0, "exchange": "NYSE", "country": None}   # fresh: sized
answers = {"quote": {"name": "Acme", "marketCap": 1e9, "price": 5.0, "exchange": "NYSE"}}
assert not companies._fetch_fmp("ACME").get("error")                                                                   # no timestamp field: not stale
answers = {"quote": {"name": "Acme", "marketCap": 1e9, "price": 5.0, "exchange": "NYSE", "timestamp": "2026-10-07T10:00:00Z"}}
assert not companies._fetch_fmp("ACME").get("error")                                                                   # a timestamp that is not a number counts as absent: sized
answers = {"quote": {"name": "Acme", "marketCap": 1e9, "price": 0, "exchange": "NYSE", "timestamp": "2026-10-07T10:00:00Z"}}
assert companies._fetch_fmp("ACME")["error"] == companies.STALE_QUOTE                                                  # ... but no price is still dead
answers = {"quote": {"name": "Acme", "marketCap": 1e9, "price": 0, "exchange": "NYSE", "timestamp": int(now_s - 60)}}
assert companies._fetch_fmp("ACME")["error"] == companies.STALE_QUOTE                                                  # no price: dead
answers = {"quote": {"name": "Acme", "marketCap": 1e9, "price": None, "exchange": "NYSE", "timestamp": int(now_s - 60)}}
assert companies._fetch_fmp("ACME")["error"] == companies.STALE_QUOTE
companies._get = real_get
# through the worker: stored as the error, unsized, left alone for a day, asked again after; a size on file is taken back
def put(aid, head_, syms, src="prnewswire", when=None, summary=None, market_cap=None, exchange=None, importance=None):
    when = when or t0
    imp = classify.importance(head_, syms, market_cap=market_cap, exchange=exchange)
    store.insert({"alpaca_id": aid, "created_at": when.isoformat(), "received_at": when.isoformat(), "headline": head_,
                  "summary": summary, "source": src, "symbols": syms, "categories": classify.classify_headline(head_),
                  "importance": imp["score"] if importance is None else importance, "reasons": imp["reasons"],
                  "url": "http://x/" + aid, "market_cap": market_cap, "exchange": exchange})
    return store.id_for(aid)
dead_id = put("p:dead", "Dead Co Wins $40 Million Navy Contract", ["DEAD"], market_cap=60e6, exchange="NASDAQ")
assert store.get(dead_id)["big"] and store.get(dead_id)["market_cap"] == 60e6
def aged(sym, hours):
    store._conn().execute("UPDATE companies SET fetched_at = ? WHERE symbol = ?",
                          ((datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(), sym)); store._conn().commit()
calls = []
def fake_fetch(sym):
    calls.append(sym)
    return {"error": companies.STALE_QUOTE, "name": "Dead Co", "price": 0.0, "exchange": "NASDAQ"}
companies.fetch_fn = fake_fetch
companies.want("DEAD"); assert companies.process_one(logs.append) == "DEAD" and calls == ["DEAD"]
assert store.company("DEAD")["error"] == companies.STALE_QUOTE and store.company("DEAD")["market_cap"] is None
d = store.get(dead_id)
assert not d["big"] and d["market_cap"] is None and d["exchange"] is None and d["size_words"] is None, d                 # the size on file is taken back
assert not companies.want("DEAD") and companies.size_of("DEAD")["error"] == companies.STALE_QUOTE and "DEAD" not in companies._queued
aged("DEAD", 2)
assert not companies.want("DEAD")                                                                                      # two hours on: left alone
aged("DEAD", 25)
assert companies.size_of("DEAD")["error"] and "DEAD" in companies._queued                                               # a day on: asked again
companies.fetch_fn = _no_net
companies._queue.clear(); companies._queued.clear()
print("stale quote ok")

# --- 6. resize_recent: the sizes already stored, re-checked with the guards -----------------------------------
# before the deploy: Novartis sized as Novan (8), Top Ships sized right, Parabolic sized as Banzai, one unsized row
novn_old = put("p:novn-old", head, ["NOVN"], src="rtpr", when=t0 - timedelta(days=1), summary=para, market_cap=2.6e6, exchange="NASDAQ")
tops_old = put("p:tops-old", TOPS_H, ["TOPS"], when=t0 - timedelta(days=1), summary="Top Ships Inc. (NYSE American: TOPS)", market_cap=2.4e6, exchange="AMEX")
para_old = put("p:para-old", PARA_H, ["PARA"], when=t0 - timedelta(days=1), summary="Parabolic (NASDAQ: PARA)", market_cap=2.0e6, exchange="NASDAQ")
plain_old = put("p:plain-old", "Acme Wins $300 Million Award From U.S. Army", ["ACME"], when=t0 - timedelta(days=1))
ancient = put("p:ancient", head, ["NOVN"], when=t0 - timedelta(days=20), summary=para, market_cap=2.6e6, exchange="NASDAQ")
assert store.get(novn_old)["importance"] == 8 and store.get(novn_old)["big"] and store.get(tops_old)["big"]
before_tops, before_plain = store.get(tops_old), store.get(plain_old)
res = store.resize_recent(14, companies.score_with_size, log=logs.append, batch=2, pause_s=0)
# sized rows in the window: the three above plus the handle path's NOVN (exchange SIX), TOPS and DEAD (unsized now) -> 5
assert res == {"rows": 5, "resized": 2, "changed": 2, "skipped": 0, "last_error": None}, res   # Novartis 8->7; Parabolic loses the "worth half the company" points
nv2 = store.get(novn_old)
assert nv2["importance"] == 7 and not nv2["big"] and nv2["exchange"] == "SIX" and nv2["market_cap"] is None and "not US-listed (SIX)" in nv2["reasons"], nv2
pa2 = store.get(para_old)
assert pa2["market_cap"] is None and pa2["exchange"] is None and "size unknown: FMP's PARA is Banzai International, Inc. Class A, not this company" in pa2["reasons"], pa2
tp2 = store.get(tops_old)
assert tp2["importance"] == before_tops["importance"] and tp2["market_cap"] == 2.4e6 and tp2["exchange"] == "AMEX" and tp2["big"]
assert store.get(plain_old) == before_plain                                                                            # never sized: untouched
assert store.get(ancient)["importance"] == 8 and store.get(ancient)["market_cap"] == 2.6e6                             # outside the 14 days: untouched
assert any("size guards checked 5 sized row(s) from the last 14 days; 2 lost or changed their size, 2 changed importance" in l for l in logs), logs
assert novn_old not in [h["id"] for h in store.recent(min_importance=5)] and tops_old in [h["id"] for h in store.recent(min_importance=5)]
def bad_score(*a, **k): raise ValueError("scorer broke")
store._conn().execute("UPDATE headlines SET market_cap = 2.6e6, exchange = 'NASDAQ' WHERE id = ?", (novn_old,)); store._conn().commit()
res = store.resize_recent(14, bad_score, log=logs.append, pause_s=0)
assert res["skipped"] == 1 and "scorer broke" in res["last_error"] and res["resized"] == 0, res
assert store.get(novn_old)["market_cap"] == 2.6e6                                                                      # a skipped row is left as it was
store.resize_recent(14, companies.score_with_size, log=logs.append, pause_s=0)
assert store.get(novn_old)["exchange"] == "SIX"
print("resize ok")

# --- 7. backfill_dups: the duplicates already on file ---------------------------------------------------------
t1 = t0 - timedelta(hours=6)
FDA1 = "Zedox Pharma Receives FDA Approval for Zedox in Adults"
FDA2 = "Zedox Pharma Announces FDA Approval of Zedox for Adult Patients"
a_id = put("d:a", FDA1, ["ZEDX"], src="rtpr", when=t1)
b_id = put("d:b", FDA2, ["ZEDX"], src="globenewswire", when=t1 + timedelta(seconds=60))               # another source, 60 s later, half the words
c_id = put("d:c", FDA2, ["ZEDX"], src="globenewswire", when=t1 + timedelta(seconds=65))               # the same source's own copy
store._conn().execute("UPDATE headlines SET copy_of = ? WHERE id = ?", (b_id, c_id)); store._conn().commit()
d_id = put("d:d", "Zedox Pharma to Acquire Beta Biologics for $400 Million in Cash", ["ZEDX"], src="globenewswire", when=t1 + timedelta(seconds=60))
e_id = put("d:e", FDA1, ["ZEDX"], src="prnewswire", when=t1 + timedelta(minutes=20))                 # outside the 15-minute window
f_id = put("d:f", FDA1, ["OTHR"], src="prnewswire", when=t1 + timedelta(seconds=30))                  # another company's identical headline
g_id = put("d:g", FDA2, ["ZEDX"], src="businesswire", when=t1 + timedelta(seconds=90))                 # worded like b (a dup), a third source: still points at the FIRST arrival
h_id = put("d:h", FDA1, ["ZEDX"], src="rtpr", when=t1 + timedelta(seconds=120))                       # the first source again, past its 60 s copy window: no copy_of, and not a dup either
assert store.get(h_id)["copy_of"] is None
assert all(store.get(i)["big"] for i in (a_id, b_id, d_id, e_id, f_id, g_id, h_id))
assert store.get(b_id)["dup_of"] is None and len([h for h in store.recent(min_importance=5, symbol="ZEDX")]) == 7   # a, b, c (a copy, not a dup), d, e, g, h
n = store.backfill_dups(14, log=logs.append)
assert n == 2, n
assert store.get(b_id)["dup_of"] == a_id and store.get(g_id)["dup_of"] == a_id, (store.get(g_id)["dup_of"], a_id, b_id)
assert store.get(c_id)["dup_of"] is None and store.get(c_id)["copy_of"] == b_id                                        # same-source copy: left alone
assert store.get(h_id)["dup_of"] is None, store.get(h_id)                                                              # a dup needs a DIFFERENT source; the same source is not one
assert store.get(d_id)["dup_of"] is None and store.get(e_id)["dup_of"] is None and store.get(f_id)["dup_of"] is None and store.get(a_id)["dup_of"] is None
rail = [h["id"] for h in store.recent(min_importance=5, symbol="ZEDX")]
assert a_id in rail and d_id in rail and e_id in rail and h_id in rail and b_id not in rail and g_id not in rail, rail
assert any("2 earlier row(s) from the last 14 days marked as another source's copy" in l for l in logs), logs
assert store.backfill_dups(14, log=logs.append) == 0                                                                   # a second pass finds nothing new
store._conn().execute("INSERT INTO headlines (alpaca_id, created_at, received_at, headline, source, symbols, categories, importance, reasons, url) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)", ("d:bad", t1.isoformat(), t1.isoformat(), "Broken row", "rtpr", "not json", "[]", 5, "[]", "http://x/bad"))
store._conn().commit()
j_id = put("d:j", FDA1, ["ZEDX"], src="globenewswire", when=t1 + timedelta(minutes=21))                                # another source, a minute after e (in its window; 21 min after a: out)
assert store.backfill_dups(14, log=logs.append) == 1                                                                   # the unreadable row is skipped, the pass goes on
assert store.get(j_id)["dup_of"] == e_id, (store.get(j_id)["dup_of"], e_id)
# the live path's own marks match the backfill's rule (same constants)
assert wires.MATCH_WINDOW_S == 15 * 60 and wires.COPY_OVERLAP == 0.5
print("backfill dups ok")

for text in logs + wlogs:
    assert "apikey" not in text.lower(), text
print("size guards ok")
