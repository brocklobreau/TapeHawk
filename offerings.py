"""
The offering check: does this company have supply waiting to hit the tape?

A halt runner dies most often the same way -- the company sells shares into the
buying. The forms that announce, permit or register that supply are all public
on EDGAR, filed under the company's own CIK, and EDGAR publishes each company's
filing history as one JSON document. This reads it and answers the question a
trader has five minutes to answer: offering, shelf, PIPE, resale -- yes or no,
when, and a link to the document.

What it can and cannot see:
  - A priced offering (424B5, 424B4) and a shelf (S-3, F-3): directly.
  - A PIPE or registered direct: the 8-K item 3.02 or 1.01 that announces it,
    and the Form D for the exempt sale.
  - Resale registrations (S-1, 424B3) that let earlier buyers unload: directly.
  - An at-the-market programme: NOT directly. An ATM lives inside an 8-K 1.01
    exhibit or a prospectus supplement. This flags the container and says so.

Every verdict here is a description of what was filed. It is not a prediction.
Rules and forms are EDGAR's; the reading of them is ours.
"""
import re
import time
from datetime import date, datetime, timedelta, timezone

import filings

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
CACHE_SECONDS = 30 * 60
LIST_DAYS = 180              # how far back the filing list goes
SHELF_YEARS = 3              # a shelf registration is usable for three years
_cache = {}
_status = {"lookups": 0, "last_error": None}

# What each form means for supply. Severity: 'red' = shares are being or were
# just sold; 'amber' = permission or registration that makes selling easy;
# 'note' = context worth seeing. Anything not listed is ignored.
FORMS = {
    "424B5": ("red",   "Prospectus supplement — an offering priced or underway"),
    "424B4": ("red",   "Final prospectus — offering priced"),
    "424B2": ("red",   "Prospectus supplement — offering terms"),
    "424B7": ("amber", "Prospectus supplement — resale by existing holders"),
    "424B3": ("amber", "Prospectus — usually a resale registration for PIPE or warrant holders"),
    "D":     ("red",   "Form D — an exempt (private) sale of securities was made"),
    "D/A":   ("red",   "Form D amendment — private sale updated"),
    "S-3":   ("amber", "Shelf registration — permission to sell shares quickly, for three years"),
    "S-3/A": ("amber", "Shelf registration, amended"),
    "S-3ASR":("amber", "Automatic shelf — usable immediately"),
    "F-3":   ("amber", "Shelf registration (foreign issuer)"),
    "F-3/A": ("amber", "Shelf registration (foreign issuer), amended"),
    "F-3ASR":("amber", "Automatic shelf (foreign issuer)"),
    "S-1":   ("amber", "Registration statement — new shares or a resale by holders"),
    "S-1/A": ("amber", "Registration statement, amended"),
    "F-1":   ("amber", "Registration statement (foreign issuer)"),
    "F-1/A": ("amber", "Registration statement (foreign issuer), amended"),
    "EFFECT":("amber", "Registration declared effective — the shares it covers can now be sold"),
    "8-K":   (None,    None),          # judged by item, below
    "8-K/A": (None,    None),
    "6-K":   ("note",  "Foreign issuer report — offerings by foreign filers often arrive here"),
}
ITEM_FLAGS = {
    "3.02": ("red",   "8-K item 3.02 — unregistered sale of equity (a PIPE or registered direct)"),
    "1.01": ("amber", "8-K item 1.01 — material agreement; a securities purchase or ATM agreement lands here"),
    "3.03": ("amber", "8-K item 3.03 — rights of security holders modified"),
    "5.03": ("note",  "8-K item 5.03 — charter or bylaw change (reverse splits and share-count increases live here)"),
}


def _parse_date(s):
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _doc_url(cik, accession, primary):
    acc = (accession or "").replace("-", "")
    if not cik or not acc:
        return None
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}"
    return f"{base}/{primary}" if primary else f"{base}/{accession}-index.htm"


def classify(form, items):
    """(severity, label) for one filing, or (None, None) to skip it."""
    form = (form or "").upper().strip()
    if form in ("8-K", "8-K/A"):
        best = (None, None)
        rank = {"red": 3, "amber": 2, "note": 1, None: 0}
        for it in filings.parse_items(items or ""):
            sev, label = ITEM_FLAGS.get(it, (None, None))
            if rank[sev] > rank[best[0]]:
                best = (sev, label)
        return best
    return FORMS.get(form, (None, None))


def parse_submissions(payload, cik, today=None):
    """Flatten EDGAR's column-oriented 'recent' block into flagged rows."""
    today = today or date.today()
    fil = (payload or {}).get("filings") if isinstance(payload, dict) else None
    rec = fil.get("recent") if isinstance(fil, dict) else None
    if not isinstance(rec, dict):
        return []
    forms = rec.get("form") or []
    dates = rec.get("filingDate") or []
    accs = rec.get("accessionNumber") or []
    docs = rec.get("primaryDocument") or []
    items = rec.get("items") or []
    descs = rec.get("primaryDocDescription") or []
    out = []
    for i, form in enumerate(forms):
        d = _parse_date(dates[i] if i < len(dates) else None)
        if d is None:
            continue
        age = (today - d).days
        sev, label = classify(form, items[i] if i < len(items) else "")
        if not sev:
            continue
        # Shelves matter for three years; everything else for the list window.
        limit = SHELF_YEARS * 366 if form.upper().startswith(("S-3", "F-3")) else LIST_DAYS
        if age > limit:
            continue
        out.append({
            "form": form, "date": d.isoformat(), "age_days": age,
            "severity": sev, "label": label,
            "items": filings.parse_items(items[i] if i < len(items) else ""),
            "description": (descs[i] if i < len(descs) else "") or None,
            "url": _doc_url(cik, accs[i] if i < len(accs) else None,
                            docs[i] if i < len(docs) else None),
        })
    out.sort(key=lambda r: r["date"], reverse=True)
    return out


def verdict(rows):
    """The short answer, as a list of findings worst-first. Empty = nothing
    found, which is stated as such rather than as 'clean'."""
    def within(days, pred):
        return [r for r in rows if r["age_days"] <= days and pred(r)]

    f = []
    priced = within(45, lambda r: r["form"].upper() in ("424B5", "424B4", "424B2"))
    if priced:
        f.append({"severity": "red", "text":
                  f"Offering priced {priced[0]['age_days']} day(s) ago ({priced[0]['form']}). "
                  "New shares are in the market at a discount to where it traded."})
    pipe = within(90, lambda r: "3.02" in r["items"] or r["form"].upper() in ("D", "D/A"))
    if pipe:
        f.append({"severity": "red", "text":
                  f"Private placement or unregistered sale {pipe[0]['age_days']} day(s) ago "
                  f"({pipe[0]['form']}). Those buyers usually sell into strength."})
    resale = within(120, lambda r: r["form"].upper() in ("424B3", "424B7", "S-1", "S-1/A", "F-1", "F-1/A"))
    if resale:
        f.append({"severity": "amber", "text":
                  f"Resale registration {resale[0]['age_days']} day(s) ago ({resale[0]['form']}). "
                  "Earlier buyers or warrant holders can now unload."})
    shelf = [r for r in rows if r["form"].upper().startswith(("S-3", "F-3"))]
    if shelf:
        f.append({"severity": "amber", "text":
                  f"Active shelf ({shelf[0]['form']}, filed {shelf[0]['date']}). "
                  "The company can price an offering in an afternoon — and an ATM may already be running off it."})
    agr = within(60, lambda r: "1.01" in r["items"] and "3.02" not in r["items"])
    if agr and not pipe:
        f.append({"severity": "amber", "text":
                  f"Material agreement {agr[0]['age_days']} day(s) ago (8-K 1.01). "
                  "Open it: securities purchase and at-the-market agreements are filed under this item."})
    eff = within(60, lambda r: r["form"].upper() == "EFFECT")
    if eff and not priced:
        f.append({"severity": "amber", "text":
                  f"A registration went effective {eff[0]['age_days']} day(s) ago. "
                  "Whatever it covers can be sold now."})
    return f


def check(ticker, log=print):
    """The whole answer for one ticker. Cached; never raises."""
    t = (ticker or "").upper().strip()
    hit = _cache.get(t)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return dict(hit[1], cached=True)
    cik, name = filings.cik_for(t, log=log)
    if not cik:
        return {"ticker": t, "error": f"{t} is not in the SEC's ticker table. "
                "Foreign and OTC names are often missing; try the company's CIK on EDGAR directly."}
    _status["lookups"] += 1
    try:
        payload = filings._get(SUBMISSIONS_URL.format(cik=cik), as_json=True)
    except Exception as e:
        _status["last_error"] = str(e)
        log(f"offerings: EDGAR did not answer for {t} ({e})")
        return {"ticker": t, "cik": cik, "name": name,
                "error": "EDGAR did not answer. Try again in a moment.", "detail": str(e)}
    rows = parse_submissions(payload, cik)
    out = {"ticker": t, "cik": cik,
           "name": (payload or {}).get("name") or name,
           "checked_at": datetime.now(timezone.utc).isoformat(),
           "window_days": LIST_DAYS, "shelf_years": SHELF_YEARS,
           "filings": rows, "verdict": verdict(rows),
           "edgar_url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik:010d}&type=&dateb=&owner=include&count=40",
           "cached": False}
    _cache[t] = (time.time(), out)
    if len(_cache) > 200:
        for k, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[:50]:
            _cache.pop(k, None)
    return out


def status():
    return dict(_status, cached=len(_cache))
