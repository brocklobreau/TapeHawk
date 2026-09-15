"""
Company research: what does this business do, what is it worth, is it cheap.

Deliberately NOT a port of Bellwether's scoring stack. That stack exists to
drive a trading bot and carries composite scores, checklists and signals --
and this session established that its entry signal picks worse than random,
so importing it here would dress up a measured non-edge as research.

What this does instead is report the FACTS a person asked for -- valuation,
growth, margins, cash flow, balance sheet, analyst view -- and compute one
honest, transparent valuation read whose arithmetic is shown rather than
hidden behind a score. No buy/sell call: those numbers are the input to your
judgement, not a substitute for it.
"""
import os
import time

import requests

BASE = "https://financialmodelingprep.com/stable"
TIMEOUT = 15
_cache = {}
CACHE_SECONDS = 600


class FMPError(RuntimeError):
    pass


def _key():
    k = os.environ.get("FMP_API_KEY")
    if not k:
        raise FMPError("FMP_API_KEY is not set")
    return k


def _get(path, params=None):
    p = dict(params or {})
    p["apikey"] = _key()
    r = requests.get(f"{BASE}/{path}", params=p, timeout=TIMEOUT)
    if r.status_code in (401, 403):
        raise FMPError(f"FMP rejected the request ({r.status_code}) for {path}")
    if r.status_code == 402:
        return None                     # plan-restricted: treat as absent
    if r.status_code != 200:
        raise FMPError(f"FMP returned {r.status_code} for {path}")
    return r.json()


def _first(v):
    if isinstance(v, list):
        return v[0] if v else {}
    return v or {}


def _pct(v):
    """FMP returns some ratios as fractions (0.27) and some as percentages.
    Multiplying blindly turns a 27% margin into 2700%, so anything already
    outside the plausible fraction range is passed through untouched."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f * 100, 2) if -1.5 <= f <= 1.5 else round(f, 2)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def valuation_read(pe, fwd_pe, growth_pct, margin_pct, upside_pct):
    """One transparent read, with its reasoning returned alongside it.

    This is not a model and does not pretend to be. It is four widely used
    rules of thumb, each stated so you can disagree with any of them -- which
    is the point. A single number like "value score 63" hides exactly the
    assumptions you would want to argue with.
    """
    points, notes = [], []
    if pe is not None:
        if pe <= 0:
            notes.append("No P/E — the company is not profitable on a trailing basis.")
        elif pe < 15:
            points.append(1); notes.append(f"P/E of {pe:.1f} is low against the ~18-20 long-run market average.")
        elif pe < 25:
            points.append(0); notes.append(f"P/E of {pe:.1f} is around the market average.")
        else:
            points.append(-1); notes.append(f"P/E of {pe:.1f} is rich — it needs growth to justify it.")
    if fwd_pe is not None and pe and fwd_pe > 0 and pe > 0:
        if fwd_pe < pe * 0.85:
            points.append(1)
            notes.append(f"Forward P/E ({fwd_pe:.1f}) is well below trailing — earnings are expected to grow.")
        elif fwd_pe > pe * 1.15:
            points.append(-1)
            notes.append(f"Forward P/E ({fwd_pe:.1f}) is above trailing — earnings are expected to fall.")
    if growth_pct is not None:
        if growth_pct > 15:
            points.append(1); notes.append(f"Revenue growing {growth_pct:.1f}%.")
        elif growth_pct < 0:
            points.append(-1); notes.append(f"Revenue shrinking {growth_pct:.1f}%.")
    if margin_pct is not None:
        if margin_pct > 15:
            points.append(1); notes.append(f"Net margin {margin_pct:.1f}% — genuinely profitable.")
        elif margin_pct < 0:
            points.append(-1); notes.append(f"Net margin {margin_pct:.1f}% — losing money.")
    if upside_pct is not None:
        if upside_pct > 15:
            points.append(1); notes.append(f"Analyst targets imply {upside_pct:.0f}% upside.")
        elif upside_pct < -5:
            points.append(-1); notes.append(f"Analyst targets sit {abs(upside_pct):.0f}% BELOW the current price.")

    if not points:
        return {"read": "Not enough data", "score": None, "notes":
                ["Not enough fundamental data to form a view."]}
    total = sum(points)
    if total >= 2:
        read = "Looks cheap on these numbers"
    elif total <= -2:
        read = "Looks expensive on these numbers"
    else:
        read = "Roughly fairly priced on these numbers"
    notes.append("This is four rules of thumb added up, not a valuation model. "
                 "A cheap-looking price is often cheap for a reason — read the news below.")
    return {"read": read, "score": total, "notes": notes}


def lookup(symbol):
    symbol = symbol.upper().strip()
    hit = _cache.get(symbol)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return dict(hit[1], cached=True)

    q = _first(_get("quote", {"symbol": symbol}))
    if not q or q.get("price") is None:
        raise FMPError(f"No data found for {symbol}")

    prof = _first(_get("profile", {"symbol": symbol}))
    ratios = _first(_get("ratios", {"symbol": symbol})) or {}
    growth = _first(_get("financial-growth", {"symbol": symbol})) or {}
    target = _first(_get("price-target-consensus", {"symbol": symbol})) or {}
    grades = _first(_get("grades-consensus", {"symbol": symbol})) or {}

    price = _num(q.get("price"))
    tgt = _num(target.get("targetConsensus") or target.get("targetMedian"))
    upside = round((tgt - price) / price * 100, 1) if (tgt and price) else None

    pe = _num(q.get("pe")) or _num(ratios.get("priceToEarningsRatio"))
    fwd_pe = _num(ratios.get("forwardPriceToEarningsGrowthRatio"))
    margin = _pct(ratios.get("netProfitMargin"))
    rev_growth = _pct(growth.get("growthRevenue"))
    fcf = _num(ratios.get("freeCashFlowPerShare"))
    shares = _num(q.get("sharesOutstanding"))

    data = {
        "ticker": symbol,
        "name": prof.get("companyName") or q.get("name") or symbol,
        "price": price,
        "change_pct": _num(q.get("changePercentage") or q.get("changesPercentage")),
        "market_cap": _num(q.get("marketCap") or prof.get("mktCap")),
        "sector": prof.get("sector"),
        "industry": prof.get("industry"),
        "exchange": prof.get("exchange") or prof.get("exchangeShortName"),
        "description": prof.get("description"),
        "ceo": prof.get("ceo"),
        "employees": prof.get("fullTimeEmployees"),
        "website": prof.get("website"),
        "country": prof.get("country"),
        "valuation": {
            "pe": pe, "forward_pe": fwd_pe,
            "price_to_book": _num(ratios.get("priceToBookRatio")),
            "price_to_sales": _num(ratios.get("priceToSalesRatio")),
            "ev_to_ebitda": _num(ratios.get("enterpriseValueMultiple")),
            "dividend_yield_pct": _pct(ratios.get("dividendYield")),
            "analyst_target": tgt,
            "analyst_upside_pct": upside,
            "analyst_consensus": grades.get("consensus"),
        },
        "performance": {
            "revenue_growth_pct": rev_growth,
            "net_margin_pct": margin,
            "operating_margin_pct": _pct(ratios.get("operatingProfitMargin")),
            "gross_margin_pct": _pct(ratios.get("grossProfitMargin")),
            "roe_pct": _pct(ratios.get("returnOnEquity")),
            "fcf_per_share": fcf,
            "fcf_total": round(fcf * shares) if (fcf and shares) else None,
            "debt_to_equity": _num(ratios.get("debtToEquityRatio")),
            "current_ratio": _num(ratios.get("currentRatio")),
        },
        "day": {
            "open": _num(q.get("open")), "high": _num(q.get("dayHigh")),
            "low": _num(q.get("dayLow")), "volume": _num(q.get("volume")),
            "year_high": _num(q.get("yearHigh")), "year_low": _num(q.get("yearLow")),
        },
    }
    data["read"] = valuation_read(pe, fwd_pe, rev_growth, margin, upside)
    data["cached"] = False
    _cache[symbol] = (time.time(), data)
    if len(_cache) > 300:
        for k, _v in sorted(_cache.items(), key=lambda kv: kv[1][0])[:100]:
            _cache.pop(k, None)
    return data
