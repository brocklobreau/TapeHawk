"""Optional tabs (2026-10-07): with TAPEHAWK_TABS unset Halts, SEC Filings and Hidden Gems are off -- their pages say
so, their APIs answer 404 with the plain words, their watchers do not start, the halt grader does not run -- while
/api/halts (the bot's halt record) always answers and the halt poller always starts. Naming them in the variable
turns them on again. No network: every starter is replaced by a recorder."""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["TAPEHAWK_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.pop("TAPEHAWK_TABS", None)
os.environ.pop("FMP_API_KEY", None)                    # no key: the grader thread and the size worker never start
import store; store.init()
import tabs
import app, companies, filings, gems, halts, outcomes, snipe, wires

NAV_SCRIPT = "fetch('/api/tabs')"
# the pieces the browser runs: a script that reads t.on, or never removes a link, is not the hide script
HIDE_PARTS = ("(t.off||[])", "a.remove()", "nav.tabs a[href=\"/'+n+'\"]")
HOW = "TAPEHAWK_TABS"
def hides(body):
    return NAV_SCRIPT in body and all(p in body for p in HIDE_PARTS)

# --- the switch itself ---
assert tabs.on() == set() and tabs.off() == ["halts", "filings", "gems"], (tabs.on(), tabs.off())
assert not tabs.is_on("halts") and not tabs.is_on("filings") and not tabs.is_on("gems")
os.environ["TAPEHAWK_TABS"] = " HALTS, filings ,bogus,, "
assert tabs.on() == {"halts", "filings"} and tabs.off() == ["gems"], (tabs.on(), tabs.off())
assert tabs.is_on("filings") and not tabs.is_on("gems")
os.environ["TAPEHAWK_TABS"] = ""
assert tabs.on() == set()
os.environ.pop("TAPEHAWK_TABS", None)
st = tabs.status()
assert st["on"] == [] and st["off"] == ["halts", "filings", "gems"] and st["always"] == ["live", "snipe", "scoreboard"], st
assert HOW in st["how"] and "Render" in st["how"], st["how"]
assert tabs.off_message("filings").startswith("The SEC Filings tab is switched off. Set TAPEHAWK_TABS"), tabs.off_message("filings")

# --- the recorders: nothing here may start a thread or touch the network ---
started = []
def _rec(name):
    def f(*a, **k):
        started.append(name)
        return True                                    # companies.start answers True = the worker is up
    return f
filings.start = _rec("filings")
gems.start = _rec("gems")
halts.start = _rec("halts")
wires.start = _rec("wires")
snipe.start = _rec("snipe")
companies.start = _rec("companies")
companies.warm = _rec("warm")
app._rescore_once = lambda: None                       # the thread still spawns; it does nothing
app._start_grader = lambda: None
graded = []
outcomes.grade_pending = lambda *a, **k: graded.append("headlines")
halts.grade_pending = lambda *a, **k: graded.append("halts")

app._started_pid = os.getpid()                         # the request hook would otherwise start every background thread here
c = app.app.test_client()

# --- all three off ---
r = c.get("/api/tabs")
assert r.status_code == 200 and r.get_json()["off"] == ["halts", "filings", "gems"] and r.get_json()["on"] == [], r.get_json()
for path in ("/halts", "/filings", "/gems"):
    r = c.get(path)
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and "switched off" in body and hides(body) and HOW in body, (path, r.status_code)
    assert "Back to Live" in body, path
    assert 'href="/"' in body and 'href="/snipe"' in body, path           # the same nav as the other pages
# off.html fills the tab's name in the browser from the path: pin the label table (each pair right) and the lookup
body = c.get("/halts").get_data(as_text=True)
for key, lab in (("halts", "Halts"), ("filings", "SEC Filings"), ("gems", "Hidden Gems")):
    assert f"{key}:'{lab}'" in body, (key, lab)
assert "location.pathname" in body and "getElementById('tabname')" in body and "getElementById('tabkey')" in body
for path, name in (("/api/filings", "SEC Filings"), ("/api/gems", "Hidden Gems"), ("/api/offerings?ticker=ACME", "SEC Filings")):
    r = c.get(path)
    j = r.get_json()
    assert r.status_code == 404 and name in j["error"] and "switched off" in j["error"] and HOW in j["error"], (path, r.status_code, j)
# the bot's read: the exact parameters halt_watch.py sends, and the plain one
for path in ("/api/halts?limit=150&all=1", "/api/halts"):
    r = c.get(path)
    assert r.status_code == 200 and "halts" in r.get_json() and isinstance(r.get_json()["halts"], list), (path, r.status_code)
for path in ("/", "/snipe", "/scoreboard"):
    r = c.get(path)
    assert r.status_code == 200 and hides(r.get_data(as_text=True)), path
r = c.get("/api/scoreboard?days=30")                   # the page's own API (sqlite only), added with its route
assert r.status_code == 200 and "flat_band" in r.get_json(), (r.status_code, r.get_json())
r = c.get("/api/status")
assert r.status_code == 200 and r.get_json()["tabs"]["off"] == ["halts", "filings", "gems"], r.get_json().get("tabs")

# start_once: halts started, filings and gems not
started.clear(); app._started_pid = None
app.start_once()
assert "halts" in started and "filings" not in started and "gems" not in started, started
assert "wires" in started and "snipe" in started and "companies" in started and "warm" in started, started
assert app._started_pid == os.getpid()

# one grading pass: the headline grader runs, the halt grader does not
graded.clear(); app._grade_once()
assert graded == ["headlines"], graded

# --- all three on (case and spaces ignored) ---
os.environ["TAPEHAWK_TABS"] = "halts, Filings ,gems"
r = c.get("/api/tabs")
assert r.get_json()["on"] == ["halts", "filings", "gems"] and r.get_json()["off"] == [], r.get_json()
OFF_MARK = 'id="tabname"'                              # only off.html has it (the nav script's comment says "switched off" on every page)
r = c.get("/halts")
body = r.get_data(as_text=True)
assert r.status_code == 200 and "api/halts" in body and OFF_MARK not in body and hides(body)
for path, api in (("/filings", "api/filings"), ("/gems", "api/gems")):
    body = c.get(path).get_data(as_text=True)
    assert api in body and OFF_MARK not in body and hides(body), (path, [p for p in HIDE_PARTS if p not in body])
assert c.get("/api/filings").status_code == 200, c.get("/api/filings").get_json()
assert c.get("/api/gems").status_code == 200, c.get("/api/gems").get_json()
assert c.get("/api/halts?limit=150&all=1").status_code == 200
started.clear(); app._started_pid = None
app.start_once()
assert "halts" in started and "filings" in started and "gems" in started, started
graded.clear(); app._grade_once()
assert graded == ["headlines", "halts"], graded

# --- halts alone ---
os.environ["TAPEHAWK_TABS"] = "halts"
assert tabs.on() == {"halts"} and c.get("/api/tabs").get_json()["off"] == ["filings", "gems"]
assert "api/halts" in c.get("/halts").get_data(as_text=True) and OFF_MARK not in c.get("/halts").get_data(as_text=True)
assert OFF_MARK in c.get("/filings").get_data(as_text=True)
assert OFF_MARK in c.get("/gems").get_data(as_text=True)
assert c.get("/api/filings").status_code == 404 and c.get("/api/gems").status_code == 404
assert c.get("/api/halts?limit=150&all=1").status_code == 200
started.clear(); app._started_pid = None
app.start_once()
assert "halts" in started and "filings" not in started and "gems" not in started, started
graded.clear(); app._grade_once()
assert graded == ["headlines", "halts"], graded

print("tabs ok")
