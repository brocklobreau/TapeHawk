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
import re

# Category -> the words that flag it. Ordered most-specific first so a
# headline about an FDA approval lands in "regulatory" rather than being
# swallowed by the generic positive-tone bucket.
CATALYST_PATTERNS = (
    ("clinical", (
        r"\bphase\s*(?:1|2|3|i{1,3})\b", r"\bclinical\s+trial", r"\btrial\s+results?\b",
        r"\btopline\b", r"\bprimary\s+endpoint", r"\befficacy\b", r"\bvaccine\b",
        r"\bfda\b", r"\bema\b", r"\bapproval\b", r"\bapproved\b", r"\bbreakthrough\s+therapy",
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
    )),
    ("contract", (
        r"\bcontract\b", r"\bawarded\b", r"\border\s+(?:worth|valued)", r"\bpartnership\b",
        r"\bcollaborat", r"\blicensing\s+(?:deal|agreement)", r"\bsupply\s+agreement\b",
        r"\bjoint\s+venture\b", r"\bwins?\s+deal\b",
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
)

_NOISE_RE = [re.compile(p, re.I) for p in NOISE_PATTERNS]
_CAT_RE = [(cat, [re.compile(p, re.I) for p in pats]) for cat, pats in CATALYST_PATTERNS]


def is_noise(title):
    t = title or ""
    return any(r.search(t) for r in _NOISE_RE)


def classify_headline(title):
    """Every category a headline matches, most-specific first. Empty list
    means nothing recognisable fired -- which is information too: it is
    usually commentary rather than an event."""
    t = title or ""
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
