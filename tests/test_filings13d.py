"""The renamed form type: SCHEDULE 13D must be found on every source."""
import os, sys, tempfile
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["TAPEHAWK_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
import store; store.init()
import filings

assert filings._is_form("SCHEDULE 13D", "SC 13D") and filings._is_form("SCHEDULE 13D/A", "SC 13D")
assert filings._is_form("SC 13D", "SC 13D") and filings._is_form("SC 13D/A", "SC 13D")
assert not filings._is_form("SCHEDULE 13G", "SC 13D") and not filings._is_form("SC 13G", "SC 13D")
assert filings._is_form("8-K", "8-K") and not filings._is_form("8-K12B", "8-K")

today = datetime.now(timezone.utc)
d = today.strftime("%Y-%m-%d")
ATOM = f"""<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>SCHEDULE 13D/A - ACME CORP (0000123456) (Subject)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/123456/000092189526001234/0000921895-26-001234-index.htm"/>
<category scheme="https://www.sec.gov/" label="form type" term="SCHEDULE 13D/A"/>
<updated>{d}T16:05:12-04:00</updated></entry>
<entry><title>SCHEDULE 13D - WIDGET INC (0000222222) (Subject)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/222222/000092189526001235/0000921895-26-001235-index.htm"/>
<category scheme="https://www.sec.gov/" label="form type" term="SCHEDULE 13D"/>
<updated>{d}T16:07:00-04:00</updated></entry>
<entry><title>SCHEDULE 13G - PASSIVE FUND (0000333333) (Filer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/333333/000092189526001236/0000921895-26-001236-index.htm"/>
<category scheme="https://www.sec.gov/" label="form type" term="SCHEDULE 13G"/>
<updated>{d}T16:08:00-04:00</updated></entry>
</feed>"""
rows = filings.parse_atom(ATOM, want="SC 13D")
assert [r["form"] for r in rows] == ["SCHEDULE 13D/A", "SCHEDULE 13D"], rows
assert rows[1]["issuer_cik"] == 222222 and rows[1]["issuer_name"] == "WIDGET INC" and rows[1]["accession"] == "0000921895-26-001235"

ymd = today.strftime("%Y%m%d")
INDEX = f"""Description:           Daily Index of EDGAR Dissemination Feed by Form Type
Last Data Received:    September 22, 2026
Comments:              webmaster@sec.gov
Anonymous FTP:         ftp://ftp.sec.gov/edgar/



Form Type   Company Name                                                  CIK         Date Filed  File Name
---------------------------------------------------------------------------------------------------------------------------------------------
4           SOMEBODY                                                      1000001     {ymd}    edgar/data/1000001/0000950170-26-000001.txt
8-K         ACME CORP                                                     123456      {ymd}    edgar/data/123456/0000950170-26-000002.txt
SCHEDULE 13D  WIDGET INC                                                  222222      {ymd}    edgar/data/222222/0000921895-26-001235.txt
SCHEDULE 13D/A ACME CORP                                                  123456      {ymd}    edgar/data/123456/0000921895-26-001234.txt
SCHEDULE 13G  PASSIVE FUND                                                333333      {ymd}    edgar/data/333333/0000921895-26-001236.txt
"""
rows = filings.parse_daily_index(INDEX, want="SC 13D")
assert filings._index_diag["header_found"], filings._index_diag
assert sorted(r["form"] for r in rows) == ["SCHEDULE 13D", "SCHEDULE 13D/A"], rows
assert filings._index_diag["has_want"] and filings._index_diag["rows"] == 5
# a header spelled differently still parses through the fallback and says what it saw
rows2 = filings.parse_daily_index(INDEX.replace("File Name", "Filename"), want="SC 13D")
assert len(rows2) == 2 and filings._index_diag["header_found"]
rows3 = filings.parse_daily_index(INDEX.replace("Form Type", "Type of Form"), want="SC 13D")
assert len(rows3) == 2 and not filings._index_diag["header_found"] and filings._index_diag["header_line"] is None

# collect(): the new spelling is asked for first and wins
calls = []
def fake_get(url, as_json=False, max_bytes=None, _retry=True):
    calls.append((url, _retry))
    if "getcurrent" in url and "SCHEDULE+13D" in url:
        return ATOM
    raise AssertionError("should not reach " + url)
filings._get = fake_get
rows, source = filings.collect("SC 13D", log=print)
assert source == "atom (SCHEDULE 13D)" and len(rows) == 2, (source, rows)
assert len(calls) == 1 and "type=SCHEDULE+13D" in calls[0][0]

# every source empty, today's index missing: the old spelling is tried, today's 403 reads as "not published yet"
calls.clear(); logs = []
def fake_get2(url, as_json=False, max_bytes=None, _retry=True):
    calls.append((url, _retry))
    if "getcurrent" in url:
        return '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'
    if "efts" in url:
        return {"hits": {"hits": []}}
    if ymd in url:
        raise filings.SecError("403 for " + url + " -- said: AccessDenied")
    return INDEX.replace("SCHEDULE 13D", "SCHEDULE 13G")
filings._get = fake_get2
rows, source = filings.collect("SC 13D", log=logs.append)
assert rows == [] and source is None
atoms = [u for u, _ in calls if "getcurrent" in u]
assert len(atoms) == 2 and "SCHEDULE+13D" in atoms[0] and "SC+13D" in atoms[1], atoms
assert any("efts" in u and "SCHEDULE+13D,SC+13D" in u for u, _ in calls)
todays = [r for u, r in calls if ymd in u]
assert todays == [False] or not todays, todays        # no backed-off retry on today's index (weekend: no call at all)
msg = logs[-1]
assert "not published yet (403)" in msg or ymd not in msg, msg
assert "NO header row" not in msg, msg

# the store finds the renamed form for the gems catalyst
store.insert_filing({"accession": "0000921895-26-001235", "kind": "13d", "form": "SCHEDULE 13D", "ticker": "WDGT",
                     "reporting_person": "Starboard Value LP", "percent": 6.1, "filed_at": d, "seen_at": today.isoformat()})
a = store.activist_13d("WDGT")
assert a["activist_13d"] == 1 and a["activist_who"] == "Starboard Value LP", a
print("13D rename ok")
