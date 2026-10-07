"""Big News rules (2026-10-07): law-firm solicitations are noise, housekeeping is never big, the catalyst is sized
against the company, and a release delivered by a second source is marked dup_of and shown once on the rail.
Every live example comes from the 120 newest Big News rows on the site on 2026-10-07, verbatim."""
import os, sys, tempfile
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["TAPEHAWK_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.pop("FMP_API_KEY", None)                    # no key: the worker never starts; tests drive the queue by hand
import store; store.init()
import classify, companies, wires

I = classify.importance


def routine(h):
    return any(r.startswith("routine:") for r in I(h)["reasons"])


# --- 1. the 19 junk items by shape: noise or routine, never big ----------------------------------------------
LAW = [
    "PRTH Investor Reminder: BFA Law Reminds Priority Technology Shareholders of the Ongoing Investigation into the $8.05 per Share Take-Private Merger",
    "CDNL Stock Notice: Cardinal Infrastructure Stock Plummeted 36% after Acquisition Issues Disclosed - Securities Fraud Investigation Underway",
    "PRTH Deal Notice: Priority Technology's $8.05 per Share Take-Private Merger Under Investigation - Shareholders Encouraged to Contact BFA Law",
    # the shapes the wires carry every day
    "ROSEN, A TOP RANKED LAW FIRM, Encourages Alfa Robotics Investors to Secure Counsel Before Important Deadline",
    "Pomerantz Law Firm Announces the Filing of a Class Action Against Acme Corp and Certain Officers",
    "SHAREHOLDER ALERT: Levi & Korsinsky Reminds Acme Investors of Lead Plaintiff Deadline",
    "Poppins Payroll Company Data Breach Alert Issued By Wolf Haldenstein",
    "Acme Corp Investigated by Johnson Fistel LLP on Behalf of Shareholders",
]
for h in LAW:
    assert classify.is_noise(h), h
    assert classify.noise_reason(h) == "law-firm solicitation", (h, classify.noise_reason(h))
    r = I(h, ["PRTH"])
    assert r["score"] == 0 and not r["big"] and r["reasons"] == ["law-firm solicitation"], (h, r)
# the old noise still says what it said
assert classify.noise_reason("If You Invested $1000 In Nvidia 10 Years Ago, Here's How Much You Would Have Today") == "filtered as routine"
# each sure shape on its own (one pattern catches each of these, so a deleted pattern shows up here)
for h in ("ACME Investor Reminder: Shares Fell 30% After the Company Cut Guidance",
          "Acme Health Data Breach Alert Issued for 2 Million Patients",
          "Acme Corp Investigated by Smith & Jones LLP for Possible Breaches of Duty to Stockholders",
          "Acme Corp: Investors Who Lost Money Have Until Friday to Act",
          # a generic shape WITH a solicitation signal beside it
          "Class Action Filed Against Acme Corp Over Misleading Statements -- Contact the Firm",
          "Securities Fraud Lawsuit Filed Against Acme Corp",
          "Shareholder Notice: Acme Investors With Losses Should Act Now",
          "Acme Corp Shareholders Encouraged to Join Investigation"):
    assert classify.noise_reason(h) == "law-firm solicitation", h
FIRMS = ["Rosen Law", "Pomerantz", "Levi & Korsinsky", "Bronstein", "Schall Law", "Kahn Swick", "Faruqi", "BFA Law", "Johnson Fistel",
         "Halper Sadeh", "Robbins LLP", "Robbins Geller", "Glancy", "Kessler Topaz", "Portnoy Law", "Bragar Eagle", "Ademi", "Monteverde",
         "Block & Leviton", "Hagens Berman", "Bernstein Liebhard", "Kirby McInerney", "Wolf Haldenstein", "Lifshitz", "Weiss Law", "Grabar",
         "Kaskela", "Kuznicki", "Berger Montague", "Scott+Scott", "Labaton", "Gainey McKenna", "Howard G. Smith", "Frank R. Cruz", "Jakubowitz",
         "Ryan & Maniskas", "Moore Law", "Purcell & Lefkowitz", "Edelson Lechtzin", "Zamansky", "Pawar Law", "Holzer & Holzer", "ClaimsFiler",
         "Shareholders Foundation"]
for f in FIRMS:
    assert classify.noise_reason(f"{f} Announces Investigation of Acme Corp") == "law-firm solicitation", f
# ... but a company's own release that shares a shape is NOT noise: no solicitation signal, or a company word
# (a settlement, a rights plan, a meeting, the SEC). Reviewed 2026-10-07: these were all hidden from the feed.
COMPANY_LEGAL = [
    "Acme Announces Settlement of Securities Class Action Lawsuit for $50 Million",
    "Acme Announces Dismissal of Securities Class Action",
    "Acme Resolves Securities Litigation; Will Pay $25 Million",
    "Acme Faces Securities Class Action; Company Says Claims Are Without Merit",
    "Acme Hit by Class Action Lawsuit After Phase 3 Failure",
    "Court Certifies Class Action Against Acme Corp; Trial Set for March",     # no firm, no signal: legal news, not a pitch
    "Acme Adopts Shareholder Rights Plan",
    "Acme Provides Shareholder Update on Phase 3 Trial and Cash Position",
    "Acme Therapeutics Issues Shareholder Update on Phase 3 Program",
    "Acme Receives Subpoena From SEC; Investigation Into Securities Sales",
    "Acme Announces Internal Investigation Into Securities Trading by Former Executive",
    "Acme Confirms It Is Under Investigation by the SEC Following Shareholder Complaint",
    "Acme Reminds Shareholders of Upcoming Special Meeting Deadline",
    "Acme Reminds Shareholders to Vote FOR the Merger Ahead of the Record Date",
    "Acme Monteverde Wins Contract",                                              # a surname, not the firm
    "Acme Appoints Jane Glancy as Chief Financial Officer",
]
for h in COMPANY_LEGAL:
    assert not classify.is_noise(h), (h, classify.noise_reason(h))
for h in COMPANY_LEGAL[:5]:
    assert "legal" in classify.classify_headline(h), h

HOUSEKEEPING = [
    ("Sysco Announces Closing Of $14.65 Billion And €1.0 Billion Notes Offerings", "a debt deal, not an event"),
    ("AMC Entertainment Holdings, Inc. Announces Results of Tender Offer, the Closing of First Lien Notes Offering and New Term Loan Facilities Totaling a $3.97 Billion Refinancing of Existing Debt", None),
    ("Virtus Dividend, Interest & Premium Strategy Fund Announces Preliminary Results of Tender Offer", "tender-offer housekeeping"),
    ("Global Engine Group Announces 1-For-10 Reverse Stock Split, Effective Oct. 8", "a reverse split"),
    ("Granite Point Mortgage Trust Inc. Announces Completion of Reverse Stock Split", "a reverse split"),
    ("Catheter Precision Effects 1-For-10 Reverse Stock Split Effective Oct. 5, 2026", "a reverse split"),
    ("Pine Tree Acquisition Prices $100M Initial Public Offering Of 10M Units At $10 Per Unit", "listing paperwork"),
    ("Pine Tree Acquisition Corp. Announces Pricing of $100,000,000 Initial Public Offering", "listing paperwork"),
    ("Tradeweb Reports September Average Daily Volume Of $3.7T, Up 29.0%; Q3 ADV Of $3.2T, Up 22.4%", "a routine metrics report"),
    ("Tradeweb Reports September 2026 Total Trading Volume of $81.6 Trillion and Average Daily Volume of $3.7 Trillion", "a routine metrics report"),
    ("Bullish Reports September Total Trading Volume $48.6B, Down From $48.8B YoY", "a routine metrics report"),
    ("Franklin Templeton Reports Preliminary Month-End Assets Under Management Of $1.79T At September 30, 2026 Vs $1.83T At August 31, 2026", "a routine metrics report"),
    ("Humana Generates Nearly $20 Billion in Economic Impact Across Kentucky, New Report Finds", "public relations"),
    ("As U.S. Cybercrime Losses Top $20 Billion, Bank of America Expands Fraud Education for Small Business Owners", "public relations"),
    # the other shapes in the list, one each
    ("Acme Therapeutics to Ring the Nasdaq Opening Bell", "a date on the calendar"),
    ("Acme Corp to Present at the H.C. Wainwright 27th Annual Global Investment Conference", "a date on the calendar"),
    ("Acme Corp Declares Quarterly Cash Dividend of $0.12 Per Share", "a regular dividend"),
    ("Acme Corp Named One of America's Best Mid-Size Companies by TIME", "public relations"),
    ("Acme Appoints Jane Roe to Its Board of Directors", "a hire"),
    ("Acme Receives Nasdaq Notice of Non-Compliance with Minimum Bid Price Requirement", "listing paperwork"),
    ("Acme Announces Results of Annual General Meeting", "meeting business"),
    ("Acme Prices Upsized $300 Million Convertible Senior Notes Offering", "a debt deal, not an event"),
    ("Acme shares are trading higher after the company received FDA approval for its monitor", "a price-action note, not a release"),
    ("Shares of software companies are trading higher amid a broader tech rally", "a price-action note, not a release"),
]
for h, words in HOUSEKEEPING:
    r = I(h, ["SYY"])
    assert routine(h) and not r["big"] and r["score"] <= 2, (h, r)
    if words:
        assert f"routine: {words}" in r["reasons"], (h, r["reasons"])
    assert not classify.is_noise(h), h                      # still in the feed, just never on the rail
# dollar figures add nothing on a routine release
assert not any(x.startswith("$") for x in I("Sysco Announces Closing Of $14.65 Billion Notes Offerings")["reasons"])

# --- 2. the real ones stay big with no size, at exactly today's score (pinned from the live rows) -----------
REAL = [
    ("Beam Global Signs Definitive Agreement to Acquire Drone Technology Company ScoutDI, Creating a Vertically Integrated U.S. Drone Platform for Industrial and Defense Markets", ["BEEM"], 9),
    ("Third Coast Bancshares, Inc. and Great Plains Bancshares, Inc. Announce Definitive Merger Agreement", ["TCBX"], 7),
    ("FDA Grants SeaStar Medical Breakthrough Device Designation for its SCD Therapy for the Treatment of Hyperinflammation in Adult Patients with Sepsis or a Septic Condition", ["ICU"], 6),
    ("Desert Control Withdraws Revenue Guidance", ["DSRT"], 5),
    ("SRX Global and CERO 1236, a Wholly-Owned SPV, Enter into Definitive Agreement to Acquire CERo Therapeutics, an Innovative Cellular Immunotherapy Company Developing CER 1236 for Hematologic Malignancies", ["SRXH"], 9),
    ("Constellation Brands Acquires SpikedAde For $75M", ["STZ"], 7),
    ("Advanced Drainage Systems To Acquire Stormwater Solutions Provider StormTrap Investments For ~$530M, Or ~$450M Adjusted For Expected Tax Benefits; Sees Deal Adjusted EPS Accretive From Year One", ["WMS"], 8),
]
for h, syms, score in REAL:
    r = I(h, syms)
    assert r["big"] and r["score"] == score and not routine(h) and not classify.is_noise(h), (h, r)
    assert I(h, syms) == I(h, syms, market_cap=None, exchange=None)          # the defaults are today's answer

# --- 3. the edges: a company's own legal news, a real tender offer, dividends -------------------------------
for h in ("Acme Announces Settlement of Patent Litigation", "Acme Sued by Rival Over Trade Secrets", "SEC Charges Acme"):
    assert not classify.is_noise(h), h
    assert "legal" in classify.classify_headline(h), h
    assert not routine(h), h
to = I("Big Corp Commences Tender Offer to Acquire All Outstanding Shares of Small Corp for $12.00 Per Share in Cash", ["SMAL"])
assert not routine("Big Corp Commences Tender Offer to Acquire All Outstanding Shares of Small Corp") and to["big"] and "M&A" in to["reasons"], to
assert not routine("Jaguar Health Declares Special Dividend Of Preferred Stock Convertible To Five Common Shares")
# the tender-offer results pattern catches this one and the "to acquire" exception rescues it (M&A, not housekeeping)
to2 = I("Big Corp Announces Final Results of Tender Offer to Acquire All Outstanding Shares of Small Corp", ["SMAL"])
assert not routine("Big Corp Announces Final Results of Tender Offer to Acquire All Outstanding Shares of Small Corp") and to2["big"], to2
assert routine("Virtus Dividend, Interest & Premium Strategy Fund Announces Preliminary Results of Tender Offer")   # ... and plain results stay routine
# the dividend patterns catch these and the special-dividend exception rescues them
for h in ("Acme Declares Special Dividend of $1.50 Per Share", "Acme Corp Declares Special Cash Dividend of $2.00 Per Share",
          "Acme Announces Special One-Time Dividend of $3.00 Per Share", "Acme Declares Special Cash Dividend of $1.00 and Regular Quarterly Dividend"):
    assert not routine(h), h
susp = I("Acme Corp Suspends Quarterly Dividend to Preserve Cash", ["ACME"])
assert not routine("Acme Corp Suspends Quarterly Dividend to Preserve Cash") and any(x.startswith("critical event") for x in susp["reasons"]), susp
assert not routine("Acme Names New CEO as CEO Steps Down")                   # a CEO exit is critical and stays
ceo = I("Acme Appoints Jane Roe as Chief Executive Officer as CEO Steps Down", ["ACME"])   # the hire pattern catches it; the exit wins
assert not routine("Acme Appoints Jane Roe as Chief Executive Officer as CEO Steps Down") and ceo["big"], ceo
assert not routine("Acme Awarded $20 Million Contract Award From the U.S. Navy")   # a contract with "award" in it is business
assert not routine("Acme Receives $5 Million NIH Grant to Develop Its Vaccine")    # a received grant is funding
assert routine("Acme Foundation Awards $1 Million Grant to Local Schools")
# contract money phrased as an award is business, with its dollar points (reviewed 2026-10-07: these scored 0 as public relations)
for h, pts in (("Acme Wins $300 Million Award From U.S. Army", 1), ("Acme Receives Up to $500 Million Award From U.S. Department of Commerce Under CHIPS Act", 1),
               ("Acme Wins $300 Million Task Order Award", 1)):
    r = I(h, ["ACME"])
    assert not routine(h) and "$300M" in r["reasons"] or "$500M" in r["reasons"], (h, r)
    assert r["score"] >= pts, (h, r)
assert routine("Acme Named One of America's Best Mid-Size Companies by TIME")
assert not routine("Acme Named to the S&P SmallCap 600 Index") and not routine("Acme Named a Supplier for Boeing 737 Program")
# a real event outranks the housekeeping beside it (reviewed 2026-10-07: all seven were capped at 2 and never reached the rail)
for h, score in (("Acme Receives FDA Approval for Zedox; to Host Conference Call Today", 7),
                 ("Acme and Beta Announce Definitive Merger Agreement; Acme to Redeem Senior Notes", 7),
                 ("Acme Secures $150 Million Term Loan and Announces FDA Approval of Zedox", 7),
                 ("Acme Recognized by FDA with Breakthrough Device Designation", 6),
                 ("Acme Celebrates FDA Approval of Zedox", 7)):
    r = I(h, ["ACME"])
    assert r["big"] and r["score"] == score and not routine(h), (h, r)
for h in ("Acme Announces Positive Topline Phase 3 Results; Company to Host Webcast Today at 8 AM",     # scored 3 before the rules too:
          "Acme Announces Results of Special Meeting: Shareholders Approve Merger With Beta"):         # no cap, the old score
    r = I(h, ["ACME"])
    assert not routine(h) and r["score"] == 3, (h, r)
# ... unless the critical match is itself housekeeping, or the headline is a price-action note about the event
assert routine("Global Engine Group Announces 1-For-10 Reverse Stock Split, Effective Oct. 8")
assert routine("Acme shares are trading higher after the company announced positive topline Phase 3 results")
assert routine("Acme shares are trading higher after the company received FDA approval for its monitor")

# --- 4. sizes --------------------------------------------------------------------------------------------------
FDA = "Acme Therapeutics Receives FDA Approval for Zedox in Adults"
base = I(FDA, ["ACME"])
assert base["big"] and base["score"] == 7, base
small = I(FDA, ["ACME"], market_cap=200e6)
assert small["big"] and small["score"] == 8 and "a small company ($200M company): one release can reprice it" in small["reasons"], small
mega = I(FDA, ["PFE"], market_cap=250e9)
assert not mega["big"] and mega["score"] == 4 and any(x.startswith("a very large company ($250B company)") for x in mega["reasons"]), mega
large = I(FDA, ["ACME"], market_cap=30e9)
assert large["big"] and large["score"] == 5 and any(x.startswith("a large company ($30B company)") for x in large["reasons"]), large
boe = I("Boeing Wins $14.7 Billion PAC-3 Seeker Production Contract From Lockheed", ["BA"], market_cap=150e9)
assert not boe["big"], boe
groq = I("Nvidia Signs $2.0 Billion Chip Supply Deal With Acme", ["NVDA"], market_cap=4000e9)
assert not groq["big"] and any("is small next to a $4.0T company" in x for x in groq["reasons"]), groq    # the dollar points taken back
nav = I("SmallSat Wins $40 Million Navy Contract", ["SSAT"])
assert not nav["big"], nav
nav2 = I("SmallSat Wins $40 Million Navy Contract", ["SSAT"], market_cap=60e6)
assert nav2["big"] and nav2["score"] == nav["score"] + 4 and "worth half the company or more" in nav2["reasons"], nav2
tenth = I("Acme Wins $300 Million Contract", ["ACME"], market_cap=2e9)
assert "a tenth of the company" in tenth["reasons"], tenth
uber = I("Uber Technologies Agrees To Acquire Catering And Workplace Meals Platform ezCater For $2.3B", ["UBER"], market_cap=160e9)
assert not uber["big"], uber
schn = I("Schneider Electric To Acquire PTC For $205 Per Share In All-Cash Deal Valuing Equity At $22.6B", ["PTC"], market_cap=20e9)
assert schn["big"] and "worth half the company or more" in schn["reasons"], schn
for ex, words in (("TSX", "not US-listed (TSX)"), ("SIX", "not US-listed (SIX)"), ("OTC", "over the counter: not tradable here"),
                  ("PNK", "over the counter: not tradable here")):
    r = I(FDA, ["ACME"], market_cap=200e6, exchange=ex)
    assert not r["big"] and r["score"] == 8 and words in r["reasons"], (ex, r)
for ex in ("NASDAQ", "NYSE", "AMEX", "NYSE American", "NYSE Arca", "Cboe", "New York Stock Exchange", "NASDAQ Global Select", None, ""):
    assert I(FDA, ["ACME"], market_cap=200e6, exchange=ex)["big"], ex
assert classify.exchange_verdict("New York Stock Exchange") is None                 # FMP's long name on the profile shape
assert classify.exchange_verdict("NONE") == "not found on a US exchange"            # companies.py's answer when FMP has no US listing
nol = I(FDA, ["NOVN"], exchange="NONE")
assert not nol["big"] and nol["score"] == 7 and "not found on a US exchange" in nol["reasons"], nol
# a routine release with a size is still routine; a noise one is still noise
assert not I("Acme Announces Closing Of $500 Million Notes Offering", ["ACME"], market_cap=100e6)["big"]
assert I(LAW[0], ["PRTH"], market_cap=50e6)["reasons"] == ["law-firm solicitation"]

# size_words
SW = classify.size_words
assert SW(None) is None and SW(0) is None and SW("x") is None
assert SW(420e6) == "$420M company" and SW(3.2e9) == "$3.2B company" and SW(210e9) == "$210B company"
assert SW(1.5e12) == "$1.5T company" and SW(12e9) == "$12B company" and SW(999e6) == "$999M company"
print("classify scores ok")

# --- 5. companies: the cache, the queue, the worker, never the network on the handle path ----------------------
import requests
calls = []
FAKE = {"SSAT": {"name": "SmallSat Inc", "market_cap": 60e6, "price": 4.2, "exchange": "NASDAQ"},
        "PFE": {"name": "Pfizer Inc", "market_cap": 150e9, "price": 25.0, "exchange": "NYSE"},
        "NOPE": {"error": "not on this FMP plan (402)"},
        "NOVN": {"error": companies.NO_LISTING, "exchange": "NONE"},
        "BOOM": {"name": "Boom Co", "market_cap": 90e6, "price": 1.0, "exchange": "NYSE"}}
FAIL_ONCE = {"BOOM": requests.ConnectTimeout("HTTPSConnectionPool(host='financialmodelingprep.com', port=443): Max retries exceeded "
                                              "with url: /stable/quote?symbol=BOOM&apikey=TESTKEY123 (Caused by ...)"),
             "RATE": companies.Transient("FMP 429 for quote"),
             "ODD": RuntimeError("something odd apikey=TESTKEY123&x=1")}
def fake_fetch(sym):
    calls.append(sym)
    if sym in FAIL_ONCE:
        raise FAIL_ONCE.pop(sym)
    return FAKE.get(sym, {"error": "no data"})
companies.fetch_fn = fake_fetch
companies._sleep = lambda s: None
logs = []
# the promises in CLAUDE.md, pinned: the FMP budget share (60 of Starter's 300 a minute) and the three windows
assert companies.RATE_PER_MIN == 60 and companies.status()["rate_per_min"] == 60
assert companies.FRESH_DAYS == 3 and companies.ERROR_RETRY_HOURS == 24 and companies.TRANSIENT_RETRY_HOURS == 1 and companies.RESCORE_HOURS == 48
def aged(sym, hours):
    store._conn().execute("UPDATE companies SET fetched_at = ? WHERE symbol = ?",
                          ((datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(), sym)); store._conn().commit()
assert companies.size_of("SSAT") is None and "SSAT" in companies._queued and companies.status()["queued"] == 1
assert not companies.want("SSAT")                                        # not queued twice
assert companies.size_of("ssat") is None and companies.status()["queued"] == 1
assert not calls                                                           # size_of never fetched
assert companies.process_one(logs.append) == "SSAT" and calls == ["SSAT"]
row = companies.size_of("SSAT")
assert row and row["market_cap"] == 60e6 and row["exchange"] == "NASDAQ" and row["error"] is None and row["fetched_at"]
assert companies.status()["queued"] == 0 and companies.status()["cached"] == 1 and companies.status()["fetched_today"] == 1
# a row under three days old is served and left alone; one over three days is served AND re-queued
aged("SSAT", 48)
assert companies.size_of("SSAT")["market_cap"] == 60e6 and "SSAT" not in companies._queued
aged("SSAT", 96)
row = companies.size_of("SSAT")
assert row and row["market_cap"] == 60e6 and "SSAT" in companies._queued
assert companies.process_one(logs.append) == "SSAT" and calls == ["SSAT", "SSAT"] and "SSAT" not in companies._queued
# a 402 is stored as an error and not asked again for a day: two hours on, still left alone; a day on, asked again
assert companies.size_of("NOPE") is None and companies.process_one(logs.append) == "NOPE"
r = store.company("NOPE")
assert r["error"] == "not on this FMP plan (402)" and r["market_cap"] is None
assert companies.size_of("NOPE")["error"] == r["error"]
assert "NOPE" not in companies._queued and not companies.want("NOPE") and calls.count("NOPE") == 1
assert companies.status()["errors"] == 1 and companies.status()["last_error"].startswith("NOPE")
aged("NOPE", 2)
assert not companies.want("NOPE") and companies.size_of("NOPE")["error"] and "NOPE" not in companies._queued
aged("NOPE", 25)
assert companies.size_of("NOPE")["error"] and "NOPE" in companies._queued                # size_of re-queues it on its own
assert companies.process_one(logs.append) == "NOPE" and calls.count("NOPE") == 2
# FMP could not be asked (a timeout): a transient error, logged once WITHOUT the key, tried again in an hour, then cached
companies.want("BOOM"); assert companies.process_one(logs.append) == "BOOM"
assert store.company("BOOM")["error"] == "fetch failed: ConnectTimeout" and any("BOOM" in l for l in logs)
assert not companies.want("BOOM")                                          # within the hour: left alone
aged("BOOM", 2)
assert companies.size_of("BOOM")["error"] and "BOOM" in companies._queued   # two hours on: asked again
assert companies.process_one(logs.append) == "BOOM" and store.company("BOOM")["market_cap"] == 90e6 and store.company("BOOM")["error"] is None
companies.want("RATE"); companies.process_one(logs.append)                  # a 429 is transient too
assert store.company("RATE")["error"] == "fetch failed: FMP 429 for quote" and companies.retry_hours(store.company("RATE")["error"]) == 1
companies.want("ODD"); companies.process_one(logs.append)                   # any other exception: scrubbed
assert store.company("ODD")["error"].startswith("fetch failed: RuntimeError") and "apikey=***" in store.company("ODD")["error"]
for text in logs + [str(companies.status()), str(store.company("BOOM")), str(store.company("ODD"))]:
    assert "TESTKEY123" not in text, text
assert companies.process_one(logs.append) is None
# the real fetch_fn's reading of FMP's answers, through a faked _get (no network): quote AND profile empty is
# "no US listing" with exchange NONE; the profile's short exchange name wins over its long one
real_get = companies._get
answers = {}
companies._get = lambda path, sym: answers.get(path)
assert companies._fetch_fmp("NOVN") == {"error": companies.NO_LISTING, "exchange": "NONE"}
answers = {"quote": 402}
assert companies._fetch_fmp("X") == {"error": "not on this FMP plan (402)"}
answers = {"quote": {"name": "Acme", "marketCap": 1e9, "price": 5.0}, "profile": {"exchange": "New York Stock Exchange", "exchangeShortName": "NYSE", "country": "US"}}
assert companies._fetch_fmp("ACME") == {"name": "Acme", "market_cap": 1e9, "price": 5.0, "exchange": "NYSE", "country": "US"}
answers = {"profile": {"companyName": "Only Profile", "exchange": "NASDAQ Global Select", "mktCap": 2e9}}
fp = companies._fetch_fmp("ONLY")
assert fp["exchange"] == "NASDAQ Global Select" and fp["market_cap"] == 2e9 and fp["name"] == "Only Profile" and not fp.get("error"), fp
companies._get = real_get
# no key: start() refuses, size_of still answers from the cache
assert companies.start(logs.append) is False and companies.size_of("SSAT")["market_cap"] == 60e6

# rescore_symbol: the $40M contract for a $60M company is promoted, Pfizer's approval demoted
now = datetime.now(timezone.utc)
def put(aid, head, syms, src="prnewswire", when=None):
    when = when or now
    imp = classify.importance(head, syms)
    store.insert({"alpaca_id": aid, "created_at": when.isoformat(), "received_at": when.isoformat(), "headline": head,
                  "source": src, "symbols": syms, "categories": classify.classify_headline(head),
                  "importance": imp["score"], "reasons": imp["reasons"], "url": "http://x/" + aid})
    return store.id_for(aid)
nav_id = put("p:nav", "SmallSat Wins $40 Million Navy Contract", ["SSAT"])
pfe_id = put("p:pfe", "Pfizer's TUKYSA Regimen Receives FDA Approval as Front-Line Maintenance Treatment for HER2+ Metastatic Breast Cancer", ["PFE"])
old_id = put("p:old", "SmallSat Wins $40 Million Navy Contract", ["SSAT"], when=now - timedelta(hours=72))
assert store.get(nav_id)["importance"] < 5 and store.get(pfe_id)["importance"] == 7 and not store.get(nav_id)["big"]
assert store.rescore_symbol("SSAT", 48, companies.score_with_size) == 1
g = store.get(nav_id)
assert g["big"] and g["importance"] >= 5 and g["market_cap"] == 60e6 and g["exchange"] == "NASDAQ" and g["size_words"] == "$60M company", g
assert "worth half the company or more" in g["reasons"]
assert store.get(old_id)["importance"] < 5 and store.get(old_id)["market_cap"] is None    # outside the 48 hours: untouched
# the worker's own re-score reaches back 48 hours: a 36-hour-old row is promoted, the 72-hour one still untouched
yday_id = put("p:yday", "SmallSat Wins $40 Million Navy Contract", ["SSAT"], when=now - timedelta(hours=36))
assert not store.get(yday_id)["big"]
companies.want("SSAT"); companies.process_one(logs.append)
assert store.get(yday_id)["big"] and store.get(yday_id)["market_cap"] == 60e6, store.get(yday_id)
assert not store.get(old_id)["big"] and store.get(old_id)["market_cap"] is None
companies.want("PFE"); companies.process_one(logs.append)                                  # the worker re-scores on its own
p = store.get(pfe_id)
assert not p["big"] and p["importance"] == 4 and p["size_words"] == "$150B company", p
# a ticker FMP has no US listing for (Novartis's NOVN is NOVN.SW there): the answer is stored, the rows re-scored
# with exchange NONE, and they can never be big; not asked again for a day
novn_id = put("p:novn", "Novartis Receives FDA Approval for Zedox in Adults", ["NOVN"])
assert store.get(novn_id)["big"]
companies.want("NOVN"); companies.process_one(logs.append)
nv = store.get(novn_id)
assert not nv["big"] and nv["importance"] == 7 and nv["exchange"] == "NONE" and "not found on a US exchange" in nv["reasons"], nv
assert store.company("NOVN")["error"] == companies.NO_LISTING and not companies.want("NOVN")
assert store.size_from(store.company("NOVN")) == (None, "NONE") and store.size_from(store.company("NOPE")) == (None, None)
assert store.size_from(None) == (None, None) and store.size_from(store.company("SSAT")) == (60e6, "NASDAQ")
assert sorted(h["id"] for h in store.recent(min_importance=5)) == sorted([nav_id, yday_id])
# rescore_recent applies the new rules to the archive (noise included), in batches, and says when it skipped a row
law_id = put("p:law", LAW[0], ["PRTH"])
store._conn().execute("UPDATE headlines SET is_noise = 0, importance = 5 WHERE id = ?", (law_id,)); store._conn().commit()
syy_id = put("p:syy", "Sysco Announces Closing Of $14.65 Billion And €1.0 Billion Notes Offerings", ["SYY"])
store._conn().execute("UPDATE headlines SET importance = 5, reasons = '[]' WHERE id = ?", (syy_id,)); store._conn().commit()
res = store.rescore_recent(30, companies.score_with_size, log=logs.append, noise_fn=classify.is_noise, batch=2, pause_s=0)
assert res["skipped"] == 0 and res["last_error"] is None, res
assert res["rows"] == 7 and res["noise"] == 1 and res["changed"] >= 2, res
assert store.get(law_id)["is_noise"] and store.get(law_id)["importance"] == 0
assert store.get(syy_id)["importance"] == 0 and "routine: a debt deal, not an event" in store.get(syy_id)["reasons"]
assert store.get(nav_id)["big"] and not store.get(pfe_id)["big"] and not store.get(novn_id)["big"]
def bad_score(*a, **k): raise ValueError("scorer broke")
res = store.rescore_recent(30, bad_score, log=logs.append, noise_fn=classify.is_noise, batch=200, pause_s=0)
assert res["skipped"] == 7 and "scorer broke" in res["last_error"] and any("7 skipped" in l for l in logs), res
assert store.get(nav_id)["big"]                                                            # a skipped row is left as it was
assert store.symbols_since(14)[:1] == ["SYY"] and set(store.symbols_since(14)) == {"SYY", "PRTH", "PFE", "SSAT", "NOVN"}
print("companies ok")

# --- 6. the handle path: the size from the cache, never a fetch; a second source's copy marked dup_of ----------
calls.clear()
published, wlogs = [], []
feed = wires.Feed("prnewswire", "http://x/prn", wlogs.append, publish=published.append); feed.primed = True
PRN = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>
<item><title>SmallSat Wins $40 Million Navy Contract</title><link>https://www.prnewswire.com/a/ss-1.html</link>
<guid>https://www.prnewswire.com/a/ss-1.html</guid><pubDate>Fri, 2 Oct 2026 18:33:00 +0000</pubDate>
<description><![CDATA[<p>DENVER /PRNewswire/ -- SmallSat Inc. (NASDAQ: SSAT) and Big Co (NYSE: BIGC) today announced...</p>]]></description></item>
</channel></rss>"""
t0 = datetime.now(timezone.utc)
assert feed.handle(wires.parse_feed(PRN), t0) == 1 and len(published) == 1
a = published[0]
assert a["symbols"] == ["SSAT", "BIGC"] and a["market_cap"] == 60e6 and a["exchange"] == "NASDAQ" and a["size_words"] == "$60M company", a
assert a["big"] and "worth half the company or more" in a["reasons"] and a["dup_of"] is None
assert not calls and "BIGC" in companies._queued                        # the handle path fetched nothing; the unknown ticker is queued
row = store.get(a["id"])
assert row["market_cap"] == 60e6 and row["size_words"] == "$60M company" and row["dup_of"] is None

# dedupe: an rtpr row, then the GlobeNewswire copy 60 s later with the ticker and half the words shared
class FakeRtpr(wires.RtprSocket):
    def __init__(self, log, publish=None):
        wires.Source.__init__(self, "rtpr", wires.RTPR_WS, log, publish, None)
        self.primed = True; self.sample_logged = True; self.pairs = set(); self.pair_order = wires.deque(maxlen=5000)
        self._st(connected=True, alerts=0, fetched=0, fetch_errors=0, reconnects=0, merged=0)
    def fetch_article(self, url):
        calls.append(("rtpr", url))
        return ("# Zedox Pharma Receives FDA Approval for Zedox in Adults\n\nBOSTON--(BUSINESS WIRE)--Zedox Pharma (NASDAQ: ZEDX) "
                "today announced that the FDA has approved Zedox...\n")
pub2 = []
r = FakeRtpr(wlogs.append, publish=pub2.append)
t1 = t0 + timedelta(minutes=5)
assert r.on_frame({"type": "alert", "ticker": "ZEDX", "article_published_at": "2026-10-05T11:30:00Z",
                   "article_url": "https://rtpr.io/a/zedx_1?sig=a"}, t1) == 1
first = pub2[0]
assert first["dup_of"] is None and first["big"] and first["source"] == "rtpr"
calls.clear()
g = wires.Feed("globenewswire", "http://x/gnw", wlogs.append, publish=published.append); g.primed = True
GNW = """<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>
<item><guid>https://www.globenewswire.com/n/1/zedox.html</guid><link>https://www.globenewswire.com/n/1/zedox.html</link>
<category domain="https://www.globenewswire.com/rss/stock">Nasdaq:ZEDX</category>
<title>Zedox Pharma Announces FDA Approval of Zedox for Adult Patients</title>
<description><![CDATA[<p>Zedox Pharma Receives FDA Approval</p>]]></description><pubDate>Mon, 05 Oct 2026 11:30 GMT</pubDate></item>
<item><guid>https://www.globenewswire.com/n/2/zedox-cmo.html</guid><link>https://www.globenewswire.com/n/2/zedox-cmo.html</link>
<category domain="https://www.globenewswire.com/rss/stock">Nasdaq:ZEDX</category>
<title>Zedox Pharma to Acquire Beta Biologics for $400 Million in Cash</title>
<description><![CDATA[<p>...</p>]]></description><pubDate>Mon, 05 Oct 2026 11:30 GMT</pubDate></item>
</channel></rss>"""
assert g.handle(wires.parse_feed(GNW), t1 + timedelta(seconds=60)) == 2
copy, other = published[-2], published[-1]
assert copy["headline"].startswith("Zedox Pharma Announces FDA Approval") and copy["dup_of"] == first["id"], copy      # the SSE payload carries it
assert copy["copy_of"] is None                                                                                        # copy_of is the same source's own second delivery
assert store.get(copy["id"])["dup_of"] == first["id"]                                                                 # and so does the row
assert other["headline"].startswith("Zedox Pharma to Acquire") and other["dup_of"] is None and other["big"]            # a different release in the same minute: its own story
assert any("(also on rtpr, 60s later)" in l for l in wlogs), [l for l in wlogs if "ZEDX" in l]
rail = [h["id"] for h in store.recent(min_importance=5)]
assert first["id"] in rail and other["id"] in rail and copy["id"] not in rail, rail                                    # the rail shows the release once
feed_ids = [h["id"] for h in store.recent()]
assert copy["id"] in feed_ids and first["id"] in feed_ids                                                             # the plain feed shows every row
assert [h["id"] for h in store.recent(min_importance=5, symbol="ZEDX")] == [other["id"], first["id"]]
assert not calls                                                                                                       # nothing on the handle path fetched a size
# Halthawk's reader keeps the keys it knows and ignores the rest: every row still has what it reads
for h in store.recent(limit=3):
    for k in ("id", "headline", "symbols", "url", "created_at", "source", "importance", "reasons", "tone", "author", "wire_pub", "rtpr_id", "paragraph", "copy_of"):
        assert k in h, k
print("classify ok")
