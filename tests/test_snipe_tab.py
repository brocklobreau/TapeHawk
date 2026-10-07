"""Tapehawk's Snipe tab: setups from the wire, the band, the state machine, halts three ways."""
import os, sys, tempfile, time
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["TAPEHAWK_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
import store; store.init()
import snipe

# --- what counts ---
K = snipe.setup_kind
assert K("Acme Receives FDA Approval For Zedox", ["ACME"]) == "fda"
assert K("Widget To Be Acquired For $12 Per Share In Cash", ["WDGT", "GNT"]) == "buyout"
assert K("SmallSat Wins $40M Navy Contract", ["SSAT"]) == "contract"
assert K("Acme Shares Halted On Circuit Breaker To The Upside", ["ACME"]) is None
assert K("Acme Q2 Earnings Beat", ["ACME"]) is None
assert K("Acme Announces Something Vague But Big", ["ACME"], importance=6, tone="positive", big=True) == "big"
assert K("Acme Announces Something Vague But Big", ["ACME"], importance=6, tone="negative", big=True) is None
assert K("Five Stocks With FDA Approvals", ["A", "B", "C", "D"]) is None
# v2 step 9: a piece written after the move never opens a setup, however good the news in it sounds
for late in ("Acme Shares Are Trading Higher After FDA Approval Of Zedox",
             "Acme Stock Is Up 40% After The Company Wins A $40 Million Navy Contract",
             "Why Acme Shares Are Trading Higher Today",
             "Acme Shares Trading Lower After Phase 3 Trial Meets Primary Endpoint",
             "Acme Soars After Definitive Merger Agreement With Giant",
             "Here's Why Acme Stock Is Moving: FDA Clearance For Its Monitor",
             "Acme Stock Jumps On FDA Approval: What You Need To Know"):
    assert K(late, ["ACME"], importance=7, tone="positive", big=True) is None, late
    assert snipe.on_headline({"headline": late, "symbols": ["ACME"], "importance": 7, "tone": "positive", "big": True}) is None, late
# ... while the release itself still does
assert K("Acme Therapeutics Receives FDA Approval for Zedox in Adults", ["ACME"]) == "fda"
assert K("Acme Wins $40 Million Navy Contract; Shares Outstanding Unchanged", ["ACME"]) == "contract"
# a business number moving in a real release is news, not a piece about the stock's move
assert K("Acme Revenue Jumps On New $40 Million Navy Contract", ["ACME"]) == "contract"
assert K("Acme Sales Surge As Zedox Receives FDA Approval", ["ACME"]) == "fda"
assert K("What You Need To Know About Acme's FDA Approval For Zedox", ["ACME"]) == "fda"
assert K("Acme Jumps On FDA Approval For Zedox", ["ACME"]) is None                       # the stock as the subject: still refused

# --- the band ---
assert snipe.band_pct(5.0) == 10.0 and snipe.band_pct(1.5) == 20.0 and abs(snipe.band_pct(0.5) - 30.0) < 0.01
noon = datetime(2026, 4, 7, 16, 0, tzinfo=timezone.utc).timestamp()        # 12:00 ET
opening = datetime(2026, 4, 7, 13, 35, tzinfo=timezone.utc).timestamp()    # 9:35 ET
assert snipe.band_pct(5.0, noon) == 10.0 and snipe.band_pct(5.0, opening) == 20.0

# --- a setup, tick by tick ---
T = noon
art = {"id": 1, "headline": "Acme Receives FDA Approval For Zedox", "symbols": ["ACME"], "url": "https://x/1",
       "created_at": datetime.fromtimestamp(T, timezone.utc).isoformat(), "importance": 7, "tone": "positive", "big": True}
s = snipe.on_headline(art, now_s=T + 0.2)
assert s and s["symbol"] == "ACME" and s["category"] == "fda" and s["state"] == "fresh"
assert snipe.on_headline(art, now_s=T + 1) is None and s["alerts"]          # the same story again: noted, not doubled
assert snipe.on_headline({"headline": "Acme Q2 Earnings", "symbols": ["ACME"]}, now_s=T + 1) is None

tape = {}
def snaps(symbols):
    return {sym: tape[sym] for sym in symbols if sym in tape}
# first poll: 10.00 printed just before the story -> the reference
tape["ACME"] = (10.00, T - 1)
assert snipe.poll_once(store, snaps, now_s=T + 2) == [] and s["ref"] == 10.0 and s["pct"] == 0 and s["band_price"] == 11.0
tape["ACME"] = (10.20, T + 5);  snipe.poll_once(store, snaps, now_s=T + 6)
assert s["state"] == "fresh" and s["pct"] == 2.0
tape["ACME"] = (10.35, T + 9);  fired = snipe.poll_once(store, snaps, now_s=T + 10)
assert fired == [("moving", "ACME")] and s["state"] == "moving", (fired, s["state"])
# speed: the 60s-ago price exists once a minute has passed
tape["ACME"] = (10.60, T + 70); snipe.poll_once(store, snaps, now_s=T + 72)
assert s["pct_60s"] is not None and s["pct_60s"] > 0
# near the band: the band is ~10% over the 5-minute average of what has been seen (~10.5), so ~11.5; at 11.20 it is within 3%
tape["ACME"] = (11.20, T + 80); fired = snipe.poll_once(store, snaps, now_s=T + 82)
assert fired == [("near", "ACME")] and s["state"] == "near" and s["to_band_pct"] <= 3.0, (fired, s["state"], s["to_band_pct"], s["band_price"])
# the wire's halt headline: halted at once
snipe.on_headline({"headline": "Acme Shares Halted On Circuit Breaker To The Upside, Stock Now Up 12%", "symbols": ["ACME"]}, now_s=T + 100)
assert s["state"] == "halted" and s["halt_source"] == "wire" and s["halts"] == 1
# stale prints during the halt change nothing
tape["ACME"] = (11.20, T + 80); snipe.poll_once(store, snaps, now_s=T + 200)
assert s["state"] == "halted"
# a print well after the halt = reopened
tape["ACME"] = (12.40, T + 400); fired = snipe.poll_once(store, snaps, now_s=T + 402)
assert fired == [("reopened", "ACME")] and s["state"] == "reopened" and s["reopen_price"] == 12.4 and s["reopen_pct"] == 24.0
# expiry after 30 minutes -> history with the outcome
snipe.poll_once(store, snaps, now_s=T + 31 * 60)
snap = snipe.snapshot()
assert not snap["watching"] and snap["history"][0]["symbol"] == "ACME" and snap["history"][0]["peak_pct"] == 24.0 and snap["history"][0]["halts"] == 1
assert [e["kind"] for e in snap["events"]] == ["new", "moving", "near", "halted", "reopened"], [e["kind"] for e in snap["events"]]

# --- silence = halted; a fade; a downside halt ends it ---
s2 = snipe.on_headline({"headline": "Beta Wins $90M Army Contract", "symbols": ["BETA"], "created_at": datetime.fromtimestamp(T, timezone.utc).isoformat()}, now_s=T)
tape["BETA"] = (4.00, T - 2); snipe.poll_once(store, snaps, now_s=T + 2)
tape["BETA"] = (4.20, T + 20); snipe.poll_once(store, snaps, now_s=T + 22)
assert s2["state"] == "moving"
snipe.poll_once(store, snaps, now_s=T + 90)                   # no new print for 70s while running
assert s2["state"] == "halted" and s2["halt_source"] == "silence", s2["state"]
s3 = snipe.on_headline({"headline": "Gamma Announces Strategic Partnership With Delta", "symbols": ["GAMA", "DLTA"], "created_at": datetime.fromtimestamp(T, timezone.utc).isoformat()}, now_s=T)
tape["GAMA"] = (2.00, T - 1); snipe.poll_once(store, snaps, now_s=T + 2)
tape["GAMA"] = (2.08, T + 10); snipe.poll_once(store, snaps, now_s=T + 12)
assert s3["state"] == "moving" and s3["band_pct"] == 20.0
tape["GAMA"] = (2.01, T + 30); snipe.poll_once(store, snaps, now_s=T + 32)
assert s3["state"] == "faded"
snipe.on_headline({"headline": "Gamma Shares Halted On Circuit Breaker To The Downside", "symbols": ["GAMA"]}, now_s=T + 40)
assert s3["state"] == "done" and s3["end_reason"] == "halted to the downside"

# --- the exchange's halt table, authoritative ---
s4 = snipe.on_headline({"headline": "Epsilon Receives FDA Clearance For Its Monitor", "symbols": ["EPSN"], "created_at": datetime.fromtimestamp(T, timezone.utc).isoformat()}, now_s=T)
tape["EPSN"] = (6.00, T - 1); snipe.poll_once(store, snaps, now_s=T + 2)
store.upsert_halt({"halt_key": "EPSN|x", "symbol": "EPSN", "code": "LUDP", "halted_at": datetime.fromtimestamp(T + 50, timezone.utc).isoformat(),
                   "halt_date": "2026-04-07", "resumed_at": None, "market": "NASDAQ", "name": "Epsilon"})
snipe._last_halt_check[0] = 0
snipe.poll_once(store, snaps, now_s=T + 60)
assert s4["state"] == "halted" and s4["halt_source"].startswith("exchange"), (s4["state"], s4["halt_source"])

# --- a failing price feed is reported, not fatal ---
def boom(symbols): raise RuntimeError("429 too many requests")
assert snipe.poll_once(store, boom, now_s=T + 70) == [] and "429" in snipe.status()["last_error"]
st = snipe.status()
assert st["setups"] == 4 and st["poll_errors"] == 1
print("snipe tab ok")
