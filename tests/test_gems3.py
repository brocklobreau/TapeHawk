"""The ported Bellwether rules: vetoes, throttle, fair value, mocked pass, store."""
import os, sys, tempfile
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["FMP_API_KEY"] = "test"
os.environ["TAPEHAWK_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
import store; store.init()
import gems

good = {"pe": 10.0, "ev_ebitda": 7.0, "pb": 1.2, "fcf_yield": 8.0, "fcf_margin": 16.0, "rev_growth": 22.0,
        "eps_growth": 25.0, "net_margin": 18.0, "roe": 21.0, "debt_to_equity": 0.25, "market_cap": 900e6,
        "price": 20.0, "year_low": 18.0, "year_high": 40.0, "upside": 35.0, "run_10d": 2.0,
        "insider_buys": 2, "insider_buyers": 2, "insider_buy_value": 400000, "insider_sells": 0,
        "beats_4q": 4, "last_surprise": 12.0, "upgrades": 2, "downgrades": 0, "grades_seen": 3,
        "ret_6m": 14.0, "avg50": 19.0, "activist_13d": 1, "activist_who": "Starboard Value", "activist_pct": 7.5,
        "shares_growth": -3.5}
assert gems.gates(good) is None
t, p, n = gems.score(good)
assert t >= 90, (t, p)
assert p["cheap"] >= 85 and p["quality"] >= 90 and p["growth"] >= 90 and p["timing"] >= 90 and p["catalyst"] >= 85, p
assert gems.verdict(t) == "Deep value + quality"
assert any("insider purchase" in x for x in n) and any("Beat earnings" in x for x in n) and any("13D" in x for x in n)
assert any("buying itself back" in x for x in n) and any("starting to work" in x for x in n)
# no catalyst at all: the part drops out, the rest carries the score
tq, pq, nq = gems.score(dict(good, beats_4q=None, upgrades=None, downgrades=None, ret_6m=None, insider_buys=None, activist_13d=None, shares_growth=None))
assert pq["catalyst"] is None and tq > 80, (tq, pq)
# a dead cheap stock: keeps missing, still falling, diluting, nobody buying
dead = dict(good, beats_4q=0, last_surprise=-20, upgrades=0, downgrades=2, grades_seen=2, ret_6m=-25, avg50=25, insider_buys=0, insider_sells=3, activist_13d=0, shares_growth=6)
td, pd_, nd = gems.score(dead)
assert pd_["catalyst"] < 30 and td < t - 15, (pd_, td, t)
assert any("keep being cheap" in x for x in nd) and any("dilution" in x for x in nd)
fv = gems.fair_value(10, 22, 18, 35, 20)
assert fv["fair_pe"] == 21.7 and fv["upside"] > 15 and fv["fair_price"] > 20, fv

# vetoes, each on its own
assert "already at" in gems.gates(dict(good, price=38))                       # 91% of range
assert "10 sessions" in gems.gates(dict(good, run_10d=40))
assert "unprofitable" in gems.gates(dict(good, pe=-3))
assert "not undervalued" in gems.gates(dict(good, pe=50))
assert "too thin" in gems.gates(dict(good, net_margin=1))
assert "negative free cash flow" in gems.gates(dict(good, fcf_yield=-1))
assert "debt/equity" in gems.gates(dict(good, debt_to_equity=2.5))
assert "shrinking" in gems.gates(dict(good, rev_growth=-4))
assert "priced about right" in gems.gates(dict(good, pe=24, rev_growth=3, net_margin=8, upside=2))
assert gems.gates(dict(good, run_10d=None, upside=None, insider_buys=None)) is None   # missing is not failure
# two vetoes are both reported
both = gems.gates(dict(good, price=38, run_10d=40))
assert "already at" in both and "10 sessions" in both

# quality throttles cheapness: same discount, bad business scores far lower
junk = dict(good, net_margin=2.5, roe=2, fcf_margin=0.5, debt_to_equity=1.9)
tj, pj, nj = gems.score(junk)
assert pj["cheap"] < p["cheap"] * 0.7, (pj, p)   # 0.35 + 0.65 x 0.41 = 62% of the discount survives
assert any("throttles" in x for x in nj)
assert tj < t - 18, (tj, t)

# mediocre but passing: mid-range, modest growth, lands in the 58-75 zone or below the bar
mid = dict(good, pe=17, ev_ebitda=12, pb=3, fcf_yield=4, fcf_margin=6, rev_growth=8, eps_growth=6, net_margin=9,
           roe=11, debt_to_equity=0.9, price=29, upside=18, run_10d=9, insider_buys=0, insider_buyers=0, insider_sells=2)
tm, pm, _ = gems.score(mid)
assert 45 <= tm < 75, (tm, pm)

# mocked FMP pass, two-stage: vetoed names never get the deep calls
CALLS = []
SPYPX = [500.0]; QPX = {}
today = datetime.now(timezone.utc).date()
# a 13D on file from the site's own watcher
store._conn().execute("INSERT INTO filings (accession, kind, form, ticker, reporting_person, percent, filed_at, seen_at) VALUES ('0001-x','13d','SC 13D','GOOD','Starboard Value LP',7.5,?,?)",
                      ((today - timedelta(days=5)).isoformat(), datetime.now(timezone.utc).isoformat()))
store._conn().commit()
def hist(sym, start, end, n=140):
    step = (end - start) / n
    return [{"symbol": sym, "date": (today - timedelta(days=(n - i))).isoformat(), "price": round(start + step * i, 4)} for i in range(n + 1)]
def fake_get(path, params=None):
    sym = (params or {}).get("symbol")
    CALLS.append((path, sym))
    if path == "company-screener":
        return [{"symbol": "GOOD", "companyName": "Good Co", "marketCap": 900e6, "sector": "Technology", "industry": "Software", "exchangeShortName": "NASDAQ", "price": 20},
                {"symbol": "HIGH", "companyName": "Ran Already", "marketCap": 2e9, "sector": "Industrials", "exchangeShortName": "NYSE", "price": 38},
                {"symbol": "LOSS", "companyName": "Loss Inc", "marketCap": 2e9, "sector": "Healthcare", "exchangeShortName": "NYSE", "price": 5},
                {"symbol": "FAIR", "companyName": "Priced Right", "marketCap": 50e9, "sector": "Technology", "exchangeShortName": "NASDAQ", "price": 30},
                {"symbol": "WT-U", "companyName": "Units", "marketCap": 5e8, "exchangeShortName": "NASDAQ"}]
    if path == "ratios":
        return [{"GOOD": {"priceToEarningsRatio": 10, "enterpriseValueMultiple": 7, "priceToSalesRatio": 1.5, "priceToBookRatio": 1.2, "freeCashFlowPerShare": 1.6, "revenuePerShare": 10, "netProfitMargin": 0.18, "operatingProfitMargin": 0.22, "returnOnEquity": 0.21, "debtToEquityRatio": 0.25, "currentRatio": 2.0, "dividendYield": 0.01},
                 "HIGH": {"priceToEarningsRatio": 10, "freeCashFlowPerShare": 2, "revenuePerShare": 10, "netProfitMargin": 0.18, "returnOnEquity": 0.2, "debtToEquityRatio": 0.2},
                 "LOSS": {"priceToEarningsRatio": -4, "netProfitMargin": -0.3, "freeCashFlowPerShare": -0.5, "debtToEquityRatio": 0.1},
                 "FAIR": {"priceToEarningsRatio": 24, "enterpriseValueMultiple": 16, "priceToBookRatio": 5, "freeCashFlowPerShare": 1.0, "revenuePerShare": 12, "netProfitMargin": 0.08, "returnOnEquity": 0.12, "debtToEquityRatio": 0.6}}[sym]]
    if path == "quote":
        if sym == "SPY":
            return [{"price": SPYPX[0]}]
        return [{"GOOD": {"price": QPX.get("GOOD", 20), "yearLow": 18, "yearHigh": 40, "marketCap": 900e6, "pe": 10, "priceAvg50": 19},
                 "HIGH": {"price": 38, "yearLow": 18, "yearHigh": 40, "marketCap": 2e9, "pe": 10},
                 "LOSS": {"price": 5, "yearLow": 4, "yearHigh": 9, "marketCap": 2e9},
                 "FAIR": {"price": 30, "yearLow": 25, "yearHigh": 40, "marketCap": 50e9, "pe": 24}}[sym]]
    if path == "financial-growth":
        return [{"GOOD": {"growthRevenue": 0.22, "growthEPS": 0.25, "growthWeightedAverageShsOutDil": -0.04}, "FAIR": {"growthRevenue": 0.03, "growthEPS": 0.02}}[sym]]
    if path == "price-target-consensus":
        return [{"targetConsensus": {"GOOD": 27, "FAIR": 30.5}[sym]}]
    if path == "historical-price-eod/light":
        return hist(sym, 17.5, 20) if sym == "GOOD" else hist(sym, 29, 30)
    if path == "earnings":
        return [{"date": (today - timedelta(days=30 + 91 * i)).isoformat(), "epsActual": 0.5 + 0.1 * i, "epsEstimated": 0.45} for i in range(4)] + \
               [{"date": (today + timedelta(days=60)).isoformat(), "epsActual": None, "epsEstimated": 0.6}]
    if path == "grades":
        return [{"date": (today - timedelta(days=10)).isoformat(), "action": "upgrade"}, {"date": (today - timedelta(days=200)).isoformat(), "action": "downgrade"}]
    if path == "insider-trading/search":
        if sym == "GOOD":
            d = (today - timedelta(days=20)).isoformat()
            return [{"transactionType": "P-Purchase", "price": 19.5, "securitiesTransacted": 10000, "reportingName": "A", "transactionDate": d},
                    {"transactionType": "P-Purchase", "price": 19.8, "securitiesTransacted": 5000, "reportingName": "B", "transactionDate": d},
                    {"transactionType": "M-Exempt", "price": 0, "securitiesTransacted": 9999, "reportingName": "C", "transactionDate": d},
                    {"transactionType": "P-Purchase", "price": 15, "securitiesTransacted": 5000, "reportingName": "D", "transactionDate": (today - timedelta(days=400)).isoformat()}]
        return None   # 402 on the plan: rules cope
    raise RuntimeError("unexpected " + path)
gems._get = fake_get
gems.PACE_SECONDS = 0
logs = []
scored, passed = gems.run_pass(log=logs.append)
assert (scored, passed) == (4, 1), (scored, passed, logs)
deep = {s for p_, s in CALLS if p_ == "financial-growth"}
assert deep == {"GOOD", "FAIR"}, deep                               # HIGH and LOSS vetoed on the cheap stage
snap = gems.snapshot()
assert [g["symbol"] for g in snap["gems"]] == ["GOOD"], snap["gems"]
g = snap["gems"][0]
assert g["fair_price"] and g["upside"] > 15 and g["parts"]["catalyst"] >= 85 and g["facts"]["insider_buys"] == 2 and g["facts"]["insider_buyers"] == 2, g
fx = g["facts"]
assert fx["beats_4q"] == 4 and fx["upgrades"] == 1 and fx["downgrades"] == 0 and fx["activist_13d"] == 1 and fx["activist_who"].startswith("Starboard"), fx
assert fx["shares_growth"] == -4.0 and fx["ret_6m"] is not None and fx["ret_6m"] > 0 and fx["run_10d"] is not None and 0 < fx["run_10d"] < 5, fx
# the pass recorded today's list as picks, once
assert snap["track"]["picks"] == 1 and snap["picks"][0]["symbol"] == "GOOD" and snap["picks"][0]["spy"] == 500.0 and snap["picks"][0]["price"] == 20, snap["track"]
assert gems.record_picks(log=print) == 0                                   # same day: idempotent
assert gems.grade_picks(log=print) == 0                                    # nothing due yet
# thirty-one days later the stock is up 10% and SPY 2%: the 1m grade lands
store._conn().execute("UPDATE gem_picks SET picked_on = ?", ((today - timedelta(days=31)).isoformat(),)); store._conn().commit()
QPX["GOOD"] = 22.0; SPYPX[0] = 510.0
assert gems.grade_picks(log=print) == 1
tr = store.gem_track_summary()["horizons"]
assert tr["1m"]["all"] == {"n": 1, "ret": 10.0, "spy": 2.0, "hit": 100.0} and tr["1m"]["high_catalyst"]["n"] == 1 and tr["1m"]["low_catalyst"]["n"] == 0, tr
assert tr["3m"]["all"]["n"] == 0
# a symbol still on the list is not re-recorded inside the cooldown, but is after it
QPX["GOOD"] = 20.0
store._conn().execute("UPDATE gem_picks SET picked_on = ?", ((today - timedelta(days=20)).isoformat(),)); store._conn().commit()
assert gems.record_picks(log=print) == 0
store._conn().execute("UPDATE gem_picks SET picked_on = ?", ((today - timedelta(days=40)).isoformat(),)); store._conn().commit()
assert gems.record_picks(log=print) == 1 and store.gem_track_summary()["picks"] == 2
assert g["rule_version"] == gems.RULE_VERSION
assert store.gem("HIGH")["disqualified"].startswith("already at 91%")
assert store.gem("LOSS")["disqualified"].startswith("unprofitable")
assert "priced about right" in store.gem("FAIR")["disqualified"], store.gem("FAIR")["disqualified"]
assert snap["counts"] == {"scored": 4, "passed": 1, "shown": 1, "good": 1, "strong": 1}, snap["counts"]
assert snap["sectors"] == ["Technology"] and snap["min_score"] == 58
assert store.gems_rule_version() == gems.RULE_VERSION
# a second pass overwrites rather than duplicates
gems.run_pass(log=logs.append); assert store.gem_counts()["scored"] == 4
print("last log:", logs[-1]); print("ported rules ok")
