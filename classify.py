"""
Headline classification: what KIND of event is this, and is it filler?

Ported from the Bellwether dashboard, where it was built to make a research
page readable. It matters more here: this service ingests Benzinga's wire,
and measured over a 10-minute window that wire runs at ~0.8 headlines a
minute -- roughly 300-500 a day -- of which a meaningful share is listicle
content ("If You Invested $1000 In X 15 Years Ago...") and routine 13F
ownership churn rather than anything that moves a price.

A live feed that shows all of it in arrival order is not a squawk, it is a
firehose of noise with the occasional real story buried in it. Categorising
and filtering IS the product.

Stated plainly, because it decides how much to trust this: the matching
below is KEYWORD MATCHING, not comprehension. It can tell you a headline
mentions a Phase 3 trial. It cannot tell you the trial succeeded, that the
drug matters, or that the market has not already priced it. A "clinical" tag
is a pointer to go read the article, never a substitute for reading it.
"""
import html
import re

# Category -> the words that flag it. Ordered most-specific first so a
# headline about an FDA approval lands in "regulatory" rather than being
# swallowed by the generic positive-tone bucket.
CATALYST_PATTERNS = (
    ("clinical", (
        r"\bphase\s*(?:1|2|3|i{1,3})\b", r"\bclinical\s+trial", r"\btrial\s+results?\b",
        r"\btopline\b", r"\bprimary\s+endpoint", r"\befficacy\b", r"\bvaccine\b",
        r"\bfda\b", r"\bema\b", r"\bbreakthrough\s+therapy",
        # "approval" and "approved" used to be matched bare, which tagged
        # "U.S. Treasury's Bessent Says If Congressional Approval Is Needed..."
        # as REGULATORY / CLINICAL on the live feed. A visibly wrong tag on a
        # macro headline costs more trust than a missed tag on a real one, so
        # these now require drug/regulator context.
        r"\b(?:fda|ema|mhra|regulatory|marketing|drug)\s+approv",
        r"\bapprov(?:es|ed|al)\b[^.]{0,40}\b(?:drug|therapy|treatment|vaccine|indication|candidate)\b",
        r"\b(?:drug|therapy|treatment|vaccine|candidate)\b[^.]{0,40}\bapprov(?:es|ed|al)\b",
        r"\bfast\s+track\b", r"\borphan\s+drug\b", r"\bpdufa\b", r"\bnda\b", r"\bbla\b",
        r"\bcomplete\s+response\s+letter\b", r"\bcrl\b",
    )),
    ("m&a", (
        r"\bacquisition\b", r"\bacquires?\b", r"\bacquired\b", r"\bmerger\b", r"\bmerges?\b",
        r"\bbuyout\b", r"\btakeover\b", r"\btake\s+private\b", r"\bdivest", r"\bspin.?off\b",
        r"\bstrategic\s+alternatives\b",
    )),
    ("earnings", (
        r"\bearnings\b", r"\bq[1-4]\b", r"\bquarterly\s+results?\b", r"\bbeats?\s+estimates?\b",
        r"\bmisses?\s+estimates?\b", r"\bguidance\b", r"\boutlook\b", r"\brevenue\s+(?:rose|fell|grew)",
        r"\braises?\s+(?:full.year|fy|guidance|outlook)", r"\bcuts?\s+(?:guidance|outlook)",
        # Executives give guidance in prose, not in the word "guidance".
        # "Gilead Exec Says HIV Market Growth Should Normalize To 2%-3%" is
        # guidance in every sense except the vocabulary the pattern wanted.
        r"\b(?:ceo|cfo|exec(?:utive)?)\b.*\bsays\b.*\b(?:expects?|growth|margin|demand|sales|revenue|costs?)\b",
        r"\bsays?\s+(?:it\s+)?expects?\b", r"\bexpects?\s+(?:to|its|higher|lower|full)\b",
        r"\bshould\s+(?:normalize|improve|accelerate|moderate)\b",
        r"\breaffirms?\b", r"\bnarrows?\s+(?:guidance|outlook|range)\b",
    )),
    ("contract", (
        r"\bcontract\b", r"\bawarded\b", r"\border\s+(?:worth|valued)", r"\bpartnership\b",
        r"\bcollaborat", r"\blicensing\s+(?:deal|agreement)", r"\bsupply\s+agreement\b",
        r"\bjoint\s+venture\b", r"\bwins?\s+deal\b",
        # Verb forms. The live feed carried "Gilead And PAHO Partner To
        # Expand..." and "Japan Wind Order Lifts GE Vernova" -- both real
        # commercial events, both missed because the patterns only had the
        # noun forms "partnership" and "contract".
        r"\bpartners?\s+(?:with|to)\b", r"\bteams?\s+up\s+with\b",
        # Allow words between the verb and the noun: the live example was
        # "Signs $60 Billion Chip Supply Deal With Meta", where the strict
        # adjacent form matched nothing and the biggest contract on the wire
        # carried no contract tag.
        r"\bsigns?\b.{0,45}?\b(?:deal|agreement|contract|mou|pact)\b",
        r"\benters?\s+into\b.{0,40}?\b(?:agreement|deal|contract)\b",
        r"\b(?:wind|defense|supply|equipment)?\s*orders?\s+(?:lifts?|boosts?|worth|from|for)\b",
        r"\bselected\s+by\b", r"\bwins?\s+(?:contract|order|bid|tender)\b",
    )),
    ("legal", (
        r"\blawsuit\b", r"\bsued\b", r"\bsues\b", r"\blitigation\b", r"\binvestigation\b",
        r"\bprobe\b", r"\bsec\s+(?:filing|charges|investigat)", r"\bsettlement\b",
        r"\bfined\b", r"\bantitrust\b", r"\brecall\b", r"\bsubpoena\b",
    )),
    ("capital", (
        r"\boffering\b", r"\bdilut", r"\bbuyback\b", r"\brepurchase\b", r"\bdividend\b",
        r"\bstock\s+split\b", r"\breverse\s+split\b", r"\bconvertible\s+notes?\b",
        r"\bsecondary\s+offering\b", r"\bat.the.market\b", r"\bcapital\s+raise\b",
    )),
    ("analyst", (
        r"\bupgrade[sd]?\b", r"\bdowngrade[sd]?\b", r"\bprice\s+target\b", r"\binitiate[sd]?\s+coverage\b",
        r"\boverweight\b", r"\bunderweight\b", r"\breiterate[sd]?\b", r"\bbuy\s+rating\b",
    )),
    ("leadership", (
        r"\bceo\b", r"\bcfo\b", r"\bresign", r"\bsteps?\s+down\b", r"\bappoint", r"\bnames?\s+new\b",
        r"\binterim\s+(?:ceo|cfo)\b", r"\bboard\s+of\s+directors\b",
    )),
    ("product", (
        r"\blaunch", r"\bunveil", r"\breleases?\s+new\b", r"\bnext.generation\b",
        r"\bpatent\b", r"\bbreakthrough\b", r"\bmilestone\b",
    )),
)

CATEGORY_LABEL = {
    "clinical": "Regulatory / clinical",
    "m&a": "M&A",
    "earnings": "Earnings & guidance",
    "contract": "Contracts & partnerships",
    "legal": "Legal & regulatory risk",
    "capital": "Capital structure",
    "analyst": "Analyst actions",
    "leadership": "Leadership",
    "product": "Product & IP",
}

# Headlines that are filler: routine 13F/ownership churn, generic listicles.
# A research page that buries an FDA approval under forty "Meeder Advisory
# Services Inc. Acquires 1,946 Shares" items is worse than no news section,
# and those posts genuinely outnumber real ones on most large caps.
NOISE_PATTERNS = (
    r"\bacquires?\s+[\d,]+\s+shares\b",
    r"\bsells?\s+[\d,]+\s+shares\b",
    r"\bbuys?\s+[\d,]+\s+shares\b",
    r"\bshares\s+(?:sold|bought|acquired)\s+by\b",
    r"\bposition\s+(?:raised|lowered|trimmed|boosted)\s+by\b",
    r"\bhas\s+\$[\d.]+\s+(?:million|billion)\s+(?:position|stake|holdings)\b",
    r"\btakes?\s+position\s+in\b",
    r"\bstake\s+(?:raised|lowered|boosted|trimmed)\b",
    r"\b(?:invests?|investment)\s+in\s+.*\bshares\b",
    # 13F ownership churn. These matter more than the share-count patterns
    # above, because "Acquires New Position in" contains the word "acquires"
    # and was being tagged as M&A -- so every large cap's M&A section filled
    # up with routine fund filings, which is precisely the noise this list
    # exists to remove. Caught by the classifier's own tests, 2026-09-15.
    r"\bacquires?\s+new\s+position\b",
    r"\bnew\s+position\s+in\b",
    r"\b(?:boosts?|lowers?|trims?|raises?|grows?|reduces?)\s+(?:its\s+)?"
    r"(?:stake|position|holdings|shares)\b",
    r"\bhas\s+position\s+in\b",
    r"\bpurchases?\s+new\s+(?:stake|shares|position)\b",
    r"\bsells?\s+(?:shares|stake|position)\s+(?:of|in)\b",

    # --- editorial / SEO filler ---
    # Benzinga's wire carries a steady stream of evergreen content alongside
    # real news. Every pattern below was taken from headlines this service
    # actually received on 2026-09-15, not invented: the retrospective-return
    # listicle and the newsletter column are the two most common by far, and
    # they arrive at the same speed as an FDA approval. A feed that ranks them
    # equally is not a squawk.
    r"\bif\s+you\s+(?:had\s+)?invested\s+\$",
    r"\bhere'?s\s+how\s+much\s+you\s+would\s+have\b",
    r"\byou\s+would\s+have\s+this\s+much\b",
    r"\b\d+\s+years?\s+ago\b.*\binvest",
    r"^breakfast\s+news\b",
    r"\bmorning\s+memo\b",
    r"\bmarket\s+clubhouse\b",
    r"\bwhat\s+\d+\s+analyst\s+ratings?\s+have\s+to\s+say\b",
    r"\b\d+\s+analysts?\s+have\s+this\s+to\s+say\b",
    r"\ba\s+closer\s+look\s+at\b",
    r"\bhere'?s\s+why\s+you\s+should\b",
    r"\bstocks?\s+to\s+watch\s+(?:today|this\s+week)\b",
    r"\btop\s+\d+\s+.*\bstocks?\b.*\b(?:buy|watch|consider)\b",
    r"\binsights?\s+into\b.*\banalyst\s+ratings\b",
    r"\bhow\s+is\s+the\s+market\s+feeling\s+about\b",
    # Options-income listicles. "How To Earn $500 A Month From MillerKnoll
    # Stock Ahead Of Q1 Earnings" was tagged as EARNINGS by the category
    # matcher, which is worse than missing it -- filler promoted into a real
    # category is filler the filters cannot hide.
    r"\bhow\s+to\s+earn\s+\$[\d,]+\s+(?:a|per)\s+(?:month|year|week)\b",
    r"\bhow\s+much\s+you\s+would\s+have\s+made\b",
    r"\bearn\s+\$[\d,]+\s+(?:a|per)\s+month\s+from\b",
    # "$100 Invested In Aon 15 Years Ago Would Be Worth This Much Today" --
    # same listicle, different wording, straight off the live wire.
    r"\$[\d,]+\s+invested\s+in\b",
    r"\bwould\s+be\s+worth\s+this\s+much\b",
    r"\bif\s+you\s+(?:had\s+)?bought\b.{0,30}\b(?:years?|decade)\s+ago\b",
)

_NOISE_RE = [re.compile(p, re.I) for p in NOISE_PATTERNS]
_CAT_RE = [(cat, [re.compile(p, re.I) for p in pats]) for cat, pats in CATALYST_PATTERNS]

# Wire copy does not arrive in plain ASCII, and every pattern above is written
# in plain ASCII. Benzinga sends curly apostrophes and HTML entities, so
# "Here's How Much You Would Have Made..." arrives as "Here\u2019s ..." or as
# "Here&#39;s ..." -- neither of which matches an apostrophe written as '.
#
# This is not hypothetical tidying. That exact headline reached the live feed
# on 2026-09-15 and was NOT filtered, because the filter was looking for a
# character the wire never sends. Normalising first is the difference between
# a filter that works on test data and one that works on the wire.
_SUBS = {
    "\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2026": "...", "\u00a0": " ",
}


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]*\n\s*\n\s*")


def strip_html(text):
    """Article bodies arrive as HTML. Block-level tags become paragraph breaks
    before stripping, so sentences do not run together into one wall of text."""
    t = str(text or "")
    t = re.sub(r"</(p|div|li|h[1-6]|tr)>", "\n\n", t, flags=re.I)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", t, flags=re.I | re.S)
    t = _TAG_RE.sub("", t)
    t = normalize(t)
    t = _WS_RE.sub("\n\n", t)
    return t.strip()


def normalize(text):
    t = html.unescape(str(text or ""))
    for bad, good in _SUBS.items():
        t = t.replace(bad, good)
    return t


def is_noise(title):
    t = normalize(title)
    return any(r.search(t) for r in _NOISE_RE)


def classify_headline(title):
    """Every category a headline matches, most-specific first. Empty list
    means nothing recognisable fired -- which is information too: it is
    usually commentary rather than an event."""
    t = normalize(title)
    return [cat for cat, regexes in _CAT_RE if any(r.search(t) for r in regexes)]


def organise_news(articles, limit=40):
    """Split a raw news feed into real events and filler, tag the events, and
    keep them newest-first. Returns (events, noise_count)."""
    events, noise = [], 0
    for a in (articles or [])[:limit * 3]:
        title = (a.get("title") or "").strip()
        if not title:
            continue
        if is_noise(title):
            noise += 1
            continue
        cats = classify_headline(title)
        events.append({
            "title": title,
            "url": a.get("url"),
            "site": a.get("site") or a.get("publisher"),
            "published": a.get("publishedDate") or a.get("date"),
            "categories": cats,
            "category_labels": [CATEGORY_LABEL[c] for c in cats],
            "snippet": (a.get("text") or "")[:280] or None,
        })
        if len(events) >= limit:
            break
    return events, noise


def catalyst_summary(events):
    """Counts by category, so the page can say '3 regulatory/clinical items
    in the last 30 days' instead of making the reader scan the list."""
    counts = {}
    for e in events:
        for c in e["categories"]:
            counts[c] = counts.get(c, 0) + 1
    return [{"category": c, "label": CATEGORY_LABEL[c], "count": n}
            for c, n in sorted(counts.items(), key=lambda kv: -kv[1])]


# --- how big a deal is this? ----------------------------------------------
#
# The feed runs ~0.8 headlines a minute, most of which are ordinary. What a
# trader actually wants is the two or three a day that genuinely matter --
# the AMD/Meta $60bn kind -- surfaced above everything else.
#
# What this DOES: score a headline on things that are mechanically visible.
# A dollar figure is the strongest single signal there is; $60 billion and
# "analyst nudges target" are not the same event and the arithmetic knows it.
# Category, magnitude words and breadth of tickers refine it.
#
# What this does NOT do, stated plainly because the distinction is the whole
# honesty of the feature: it does not UNDERSTAND the news. Given "AMD signs
# $60B deal with Meta, warrants vest as shares reach $600", it will correctly
# shout that this is enormous. It will not tell you the warrant structure
# means Meta is effectively long AMD upside. That is comprehension, and no
# amount of regex gets there. The score is a spotlight, not an analyst.
#
# Every flagged headline carries its REASONS, so when it surfaces something
# stupid you can see exactly which rule misfired and fix that rule.

_MAGNITUDE = {
    "halted": 3, "halt": 3, "bankruptcy": 3, "chapter 11": 3, "delisted": 3,
    "record": 1, "largest": 2, "biggest": 2, "historic": 2, "unprecedented": 2,
    "plunges": 2, "plunge": 2, "soars": 2, "soar": 2, "surges": 2, "surge": 2,
    "crashes": 2, "collapse": 2, "slashes": 2, "doubles": 2, "triples": 2,
    "breakthrough": 2, "landmark": 2, "massive": 1, "sweeping": 1,
}
_MAG_RE = {w: re.compile(r"\b" + re.escape(w) + r"\b", re.I) for w in _MAGNITUDE}

# Category weights. M&A and regulatory/clinical outcomes move prices hardest;
# an analyst note almost never does on its own.
_CAT_WEIGHT = {"m&a": 3, "clinical": 3, "contract": 2, "capital": 2,
               "legal": 2, "earnings": 1, "leadership": 1, "product": 1,
               "analyst": 0}

# Events that are market-moving on their own, whatever the dollar figure.
# Added after testing: an FDA approval and a trading halt both scored 3 and
# would have been buried, which is exactly backwards -- a halt is the single
# most urgent thing that can appear on a wire, and it never carries a price
# tag. Weighting purely on money was a bias toward big-cap M&A and against
# biotech, where the whole company can reprice on a one-line regulatory
# decision with no number in it at all.
_CRITICAL_PATTERNS = (
    r"\bfda\s+approv", r"\bapproved\s+by\s+the\s+fda\b",
    r"\bcomplete\s+response\s+letter\b", r"\bfda\s+reject",
    r"\btrading\s+halted\b", r"\bhalted\s+(?:in|for|pending|on)\b",
    # The live wire produced "ReTo Eco-Solutions Shared Halted On Circuit
    # Breaker To The Upside; Stock Now Up 271.69%" -- a halt AND a 271% move,
    # and it reached the feed unflagged because the pattern wanted the exact
    # phrase "trading halted".
    r"\bcircuit\s+breaker\b", r"\bshares?\s+halted\b", r"\bhalt(?:ed)?\s+to\s+the\s+(?:up|down)side\b",
    r"\bresumes?\s+trading\b",
    r"\bbankrupt", r"\bchapter\s+11\b", r"\bdelisted\b", r"\bdefaults?\s+on\b",
    r"\bmerger\s+agreement\b", r"\bdefinitive\s+agreement\b",
    r"\bto\s+acquire\b.*\$", r"\bacquires?\b.*\bfor\s+\$",
    r"\bphase\s*3\b.*\b(?:meets?|hits?|fails?|misses?|beats?)\b",
    r"\bfails?\s+(?:to\s+meet\s+)?(?:primary\s+)?endpoint\b",
    r"\bmeets?\s+primary\s+endpoint\b",
    r"\bguidance\s+(?:cut|slashed|withdrawn|pulled)\b",
    r"\bwithdraws?\b.{0,20}?\b(?:guidance|outlook|forecast)\b",
    r"\bsuspends?\b.{0,20}?\b(?:guidance|outlook|dividend)\b",
    r"\bceo\s+(?:resigns|steps?\s+down|ousted|fired)\b",
    r"\bstock\s+split\b", r"\btender\s+offer\b",
    r"\bgoing\s+private\b", r"\btakeover\s+bid\b",
)
_CRITICAL_RE = [re.compile(p, re.I) for p in _CRITICAL_PATTERNS]

_MONEY_RE = re.compile(
    r"\$\s?([\d,]+(?:\.\d+)?)\s*(trillion|billion|million|bn|mn|[bmt])\b", re.I)
_MULT = {"trillion": 1e12, "t": 1e12, "billion": 1e9, "bn": 1e9, "b": 1e9,
         "million": 1e6, "mn": 1e6, "m": 1e6}


def biggest_dollar_amount(text):
    """Largest dollar figure in the headline, in dollars. None if there is
    no figure. Reads $60 billion, $4.9B and $500 million alike."""
    best = None
    for m in _MONEY_RE.finditer(normalize(text)):
        try:
            v = float(m.group(1).replace(",", "")) * _MULT[m.group(2).lower()]
        except (ValueError, KeyError):
            continue
        if best is None or v > best:
            best = v
    return best


def importance(title, symbols=None, categories=None):
    """Returns {"score", "big", "reasons", "dollars"}.

    `big` is what the UI promotes. The threshold is set so a headline needs
    real weight -- a large sum, or a major category plus a magnitude word --
    rather than any single weak hint. Tuned deliberately toward missing
    borderline stories rather than flooding the section: a "big news" rail
    that fires ten times an hour is one nobody reads."""
    t = normalize(title)
    cats = categories if categories is not None else classify_headline(t)
    score, reasons = 0, []

    dollars = biggest_dollar_amount(t)
    if dollars:
        if dollars >= 50e9:
            score += 6; reasons.append(f"${dollars/1e9:,.0f}B — enormous")
        elif dollars >= 10e9:
            score += 5; reasons.append(f"${dollars/1e9:,.1f}B")
        elif dollars >= 1e9:
            score += 3; reasons.append(f"${dollars/1e9:,.1f}B")
        elif dollars >= 250e6:
            score += 1; reasons.append(f"${dollars/1e6:,.0f}M")

    for c in cats:
        w = _CAT_WEIGHT.get(c, 0)
        if w:
            score += w
            reasons.append(CATEGORY_LABEL.get(c, c))

    for word, w in _MAGNITUDE.items():
        if _MAG_RE[word].search(t):
            score += w
            reasons.append(f"“{word}”")
            break          # one magnitude word is the signal; five is a style

    for r in _CRITICAL_RE:
        m = r.search(t)
        if m:
            score += 4
            reasons.append(f"critical event ({m.group(0).strip().lower()})")
            break

    n = len(symbols or [])
    if n >= 4:
        score += 1
        reasons.append(f"{n} tickers affected")

    # Filler can never be big news, whatever else it scores. An options-income
    # listicle mentioning "$500 a month" must not reach the top rail.
    if is_noise(t):
        return {"score": 0, "big": False, "reasons": ["filtered as routine"],
                "dollars": dollars}

    return {"score": score, "big": score >= 5, "reasons": reasons,
            "dollars": dollars}


# --- good news or bad news? -----------------------------------------------
#
# Harder than importance, and the failure mode is worse: a wrong "POSITIVE"
# on a headline someone is about to act on does more damage than no label.
# So "unclear" is a first-class answer here, not a fallback.
#
# Three cases that break naive sentiment scoring, all handled explicitly:
#   * A TRADING HALT has no direction. "Halted to the upside, up 271%" and
#     "halted pending news" are the same event with opposite meanings.
#   * An ACQUISITION is good for the target and expensive for the acquirer --
#     one headline, opposite signs depending which ticker you hold.
#   * "FDA" appears in approvals AND rejections. The verb carries the sign.
#
# An explicit price move outranks vocabulary, because it is the market's own
# verdict rather than our inference from word choice.

_POS_PATTERNS = (
    (r"\b(?:surges?|soars?|jumps?|rallies|rallied|climbs?|spikes?)\b", "rising"),
    (r"\bto\s+the\s+upside\b", "moving up"),
    (r"\b(?:fda|ema|regulatory)\s+approv", "regulatory approval"),
    (r"\bapprov(?:es|ed|al)\b[^.]{0,40}\b(?:drug|therapy|treatment|vaccine)\b", "approval"),
    (r"\bmeets?\s+primary\s+endpoint\b", "trial succeeded"),
    (r"\bbeats?\b[^.]{0,25}\b(?:estimates?|expectations?|consensus)\b", "beat expectations"),
    (r"\braises?\s+(?:full.year\s+|fy\s+)?(?:guidance|outlook|forecast)\b", "guidance raised"),
    (r"\bupgrade[sd]?\b", "analyst upgrade"),
    (r"\braises?\s+price\s+target\b", "target raised"),
    (r"\b(?:wins?|awarded|secures?)\b[^.]{0,30}\b(?:contract|order|deal|bid)\b", "won business"),
    (r"\bbuyback\b|\brepurchase\s+program\b", "buyback"),
    (r"\b(?:raises?|initiates?|increases?)\s+dividend\b", "dividend raised"),
    (r"\brecord\s+(?:revenue|profit|quarter|earnings|sales)\b", "record results"),
)

_NEG_PATTERNS = (
    (r"\b(?:plunges?|plummets?|tumbles?|sinks?|slides?|crashes?|slumps?)\b", "falling"),
    (r"\bto\s+the\s+downside\b", "moving down"),
    (r"\btrading\s+lower\b", "trading lower"),
    (r"\bcomplete\s+response\s+letter\b|\b(?:fda|regulator|ema)\s+reject|"
     r"\brejects?\s+(?:the\s+)?(?:approval|application|filing)\b", "regulatory rejection"),
    (r"\bfails?\s+(?:to\s+meet\s+)?(?:primary\s+)?endpoint\b", "trial failed"),
    (r"\bmisses?\b[^.]{0,25}\b(?:estimates?|expectations?|consensus)\b", "missed expectations"),
    (r"\b(?:cuts?|slashes?|lowers?|withdraws?|suspends?)\b[^.]{0,20}\b(?:guidance|outlook|forecast)\b",
     "guidance cut"),
    (r"\bdowngrade[sd]?\b", "analyst downgrade"),
    (r"\blowers?\s+price\s+target\b", "target cut"),
    (r"\bbankrupt|\bchapter\s+11\b|\bdefaults?\s+on\b", "insolvency"),
    (r"\b(?:lawsuit|sued|investigation|probe|subpoena|antitrust)\b", "legal trouble"),
    (r"\brecall(?:s|ed|ing)?\b", "recall"),
    (r"\blayoffs?\b|\bcuts?\s+\d+.{0,12}\bjobs\b", "layoffs"),
    (r"\b(?:suspends?|cuts?|eliminates?)\s+dividend\b", "dividend cut"),
    (r"\bdelisted\b|\bdelisting\b", "delisting"),
    (r"\bshort\s+seller\b|\bfraud\b", "fraud allegation"),
)

# Events whose direction genuinely cannot be read off the headline.
_AMBIGUOUS = (
    (r"\btrading\s+halted\b|\bshares?\s+halted\b|\bcircuit\s+breaker\b",
     "a halt can be up or down"),
    (r"\b(?:acquires?|acquisition|to\s+acquire|merger|takeover)\b",
     "good for the target, costly for the buyer"),
)

_POS_RE = [(re.compile(p, re.I), why) for p, why in _POS_PATTERNS]
_NEG_RE = [(re.compile(p, re.I), why) for p, why in _NEG_PATTERNS]
_AMB_RE = [(re.compile(p, re.I), why) for p, why in _AMBIGUOUS]

# Negation guard. "Novavax Vaccine FAILS TO MEET Primary Endpoint" matched
# the POSITIVE pattern for "meets primary endpoint" and came out as mixed --
# the worst possible answer for an unambiguously bad headline. The same trap
# waits in "fails to win contract" and "did not beat estimates", so this is
# fixed once, generally, rather than per verb: any positive match whose
# immediately preceding words negate it is discarded.
_NEGATORS = re.compile(
    r"\b(?:fail(?:s|ed|ing)?\s+to|did\s*n[o']t|does\s*n[o']t|do\s*n[o']t|won[o']t|"
    r"unable\s+to|denied|rejects?|rejected|refuses?\s+to|falls?\s+short\s+of|"
    r"no\s+longer|without)\s*$", re.I)
_NEG_LOOKBACK = 28


def _negated(text, match_start):
    """Was this positive phrase negated by what comes immediately before it?"""
    before = text[max(0, match_start - _NEG_LOOKBACK):match_start]
    return bool(_NEGATORS.search(before))


_MOVE_UP = re.compile(r"\b(?:up|gain(?:s|ed)?|rose|rises?|climb(?:s|ed)?)\s+(\d+(?:\.\d+)?)%", re.I)
_MOVE_DOWN = re.compile(r"\b(?:down|fell|falls?|drop(?:s|ped)?|lost|loses?)\s+(\d+(?:\.\d+)?)%", re.I)


# How a negated positive should read. Without this the output was literally
# "failed to approval", which looks like a bug even when the verdict is right.
_NEGATED_PHRASING = {
    "won business": "failed to win the business",
    "beat expectations": "did not beat expectations",
    "trial succeeded": "did not hit the endpoint",
    "regulatory approval": "approval refused",
    "approval": "approval refused",
    "guidance raised": "guidance not raised",
    "analyst upgrade": "no upgrade",
    "target raised": "target not raised",
    "record results": "no record result",
    "rising": "not rising",
}


def tone(title):
    """{"direction": positive|negative|unclear, "reasons": [...]}

    Conservative by design. When positive and negative signals both fire and
    neither is an explicit price move, the answer is "unclear" -- mixed
    evidence is a real state, and flattening it to a guess is how a label
    stops being worth reading."""
    t = normalize(title)

    up, down = _MOVE_UP.search(t), _MOVE_DOWN.search(t)
    if up and not down:
        return {"direction": "positive", "reasons": [f"stock up {up.group(1)}%"]}
    if down and not up:
        return {"direction": "negative", "reasons": [f"stock down {down.group(1)}%"]}

    pos, neg = [], [why for r, why in _NEG_RE if r.search(t)]
    for r, why in _POS_RE:
        m = r.search(t)
        if not m:
            continue
        if _negated(t, m.start()):
            # A negated positive is not merely "not positive" -- it is
            # negative evidence. "Company Fails To Win Air Force Contract"
            # read as "unclear" until this was recorded on the negative side,
            # which is a worse answer than the false positive it replaced.
            neg.append(_NEGATED_PHRASING.get(why, "failed to " + why))
        else:
            pos.append(why)
    if pos and not neg:
        return {"direction": "positive", "reasons": pos[:3]}
    if neg and not pos:
        return {"direction": "negative", "reasons": neg[:3]}
    if pos and neg:
        return {"direction": "unclear",
                "reasons": ["mixed: " + ", ".join(pos[:2] + neg[:2])]}
    for r, why in _AMB_RE:
        if r.search(t):
            return {"direction": "unclear", "reasons": [why]}
    return {"direction": "unclear", "reasons": []}


# --- what is this likely to do? -------------------------------------------
#
# Deliberately qualitative. It would be easy to print "expected move: 4.2%"
# and it would be fiction -- nothing here knows the company's market cap, its
# float, what was already priced in, or what the tape is doing. A made-up
# number is worse than no number because it looks like knowledge.
#
# What this DOES say is what KIND of reaction the event type usually
# produces, and on what timescale, with the reasoning attached. The timing
# claim is not invented either: measured on 6,358 real headlines against
# 5-minute bars earlier in this project, 42% of the two-hour move was spent
# within 5 minutes and 56% within 15. That is why every note below points at
# the first few minutes rather than the rest of the day.

_IMPACT_RULES = (
    ("halt", r"\btrading\s+halted\b|\bshares?\s+halted\b|\bcircuit\s+breaker\b", "high",
     "Halts are called for disorderly moves or pending news. Expect a large "
     "gap when trading resumes — the size is unknowable until it does."),
    ("clinical", r"\bfda\s+approv|\bcomplete\s+response\s+letter\b|\bfda\s+reject|"
     r"\b(?:meets?|fails?)\b[^.]{0,30}\bendpoint\b", "high",
     "Regulatory decisions reprice drug developers hard, and the smaller the "
     "company the harder — a single approval can be most of the thesis."),
    ("insolvency", r"\bbankrupt|\bchapter\s+11\b|\bdelisted\b|\bdefaults?\s+on\b", "high",
     "Existential events. Equity holders are usually last in line."),
    ("guidance", r"\b(?:cuts?|slashes?|withdraws?|suspends?|raises?)\b[^.]{0,20}"
     r"\b(?:guidance|outlook|forecast)\b", "high",
     "Guidance changes move stocks the same session — they reset what every "
     "model downstream is built on."),
    ("m&a", r"\b(?:acquires?|acquisition|to\s+acquire|merger|takeover|tender\s+offer)\b", "high",
     "Targets typically jump toward the offer price and stay there; acquirers "
     "often drift the other way on the cost."),
    ("bigmoney", r"\$\s?\d[\d,.]*\s*(?:billion|bn|b)\b", "medium",
     "Large contracts matter in proportion to the company's size — the same "
     "number is transformative for a mid-cap and a rounding error for a mega-cap."),
    ("earnings", r"\b(?:beats?|misses?)\b[^.]{0,25}\b(?:estimates?|expectations?)\b", "medium",
     "Earnings surprises usually move the stock immediately, then fade or "
     "extend depending on what management says next."),
    ("legal", r"\b(?:lawsuit|sued|investigation|probe|antitrust|recall|fraud)\b", "medium",
     "Legal and regulatory trouble tends to grind rather than gap — the cost "
     "is rarely knowable on day one."),
    ("leadership", r"\bceo\s+(?:resigns|steps?\s+down|ousted|fired)\b", "medium",
     "Leadership exits move stocks most when unexpected or unexplained."),
    ("analyst", r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|\bprice\s+target\b", "low",
     "Broker notes usually produce a small, short-lived move unless the call "
     "is a real outlier."),
)
_IMPACT_RE = [(k, re.compile(p, re.I), lvl, note) for k, p, lvl, note in _IMPACT_RULES]

IMPACT_LABEL = {"high": "Likely a big mover",
                "medium": "Should move the stock",
                "low": "Probably a small move"}


def impact(title, symbols=None):
    """{"level": high|medium|low|None, "label", "note", "breadth"}

    First matching rule wins, and they are ordered by how decisively the
    event type usually moves a price. Returns level None when nothing
    recognisable fires, rather than inventing a guess."""
    t = normalize(title)
    n = len(symbols or [])
    breadth = (f"{n} tickers affected" if n >= 4
               else (", ".join(symbols) if symbols else None))
    for _key, rx, level, note in _IMPACT_RE:
        if rx.search(t):
            return {"level": level, "label": IMPACT_LABEL[level],
                    "note": note, "breadth": breadth}
    return {"level": None, "label": None, "note": None, "breadth": breadth}
