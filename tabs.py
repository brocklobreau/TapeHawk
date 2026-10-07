"""
Which optional tabs are switched on.

The owner asked (2026-10-07) to switch off the tabs not in use -- Halts,
SEC Filings and Hidden Gems -- to keep TapeHawk light (CPU, outside API
calls, bandwidth), and to be able to turn one back on later without a code
change. So the switch is one environment variable, TAPEHAWK_TABS, a comma
list of the optional tabs that are ON; unset or empty means none of them.
Live, Snipe and the Scoreboard are always on.

What a switch stops is the PAGE and the work that only the page needs: the
EDGAR watcher and /api/filings + /api/offerings (filings), the FMP screener
pass and /api/gems (gems), the halts page and the FMP halt grading (halts).
The Nasdaq halt poller and /api/halts run whatever this says: Halthawk reads
/api/halts every few seconds for the exchange's halt record.

The variable is read on every call, never cached, so a test can flip it.
"""
import os

ENV = "TAPEHAWK_TABS"
OPTIONAL = ("halts", "filings", "gems")
LABELS = {"halts": "Halts", "filings": "SEC Filings", "gems": "Hidden Gems"}
ALWAYS = ("live", "snipe", "scoreboard")
HOW = ("set TAPEHAWK_TABS in Render's Environment tab, for example "
       "halts,filings,gems, then redeploy")


def on():
    """The set of optional tabs switched on: case and spaces ignored,
    unknown names ignored, unset or empty = none."""
    raw = os.environ.get(ENV, "") or ""
    names = {p.strip().lower() for p in raw.split(",")}
    return {n for n in names if n in OPTIONAL}


def off():
    """The optional tabs switched off, in the nav's order."""
    live = on()
    return [n for n in OPTIONAL if n not in live]


def is_on(name):
    return name in on()


def label(name):
    return LABELS.get(name, name)


def off_message(name):
    """The plain-words answer an API gives when its tab is off."""
    return f"The {label(name)} tab is switched off. {HOW[0].upper() + HOW[1:]}."


def status():
    live = on()
    return {"on": [n for n in OPTIONAL if n in live],
            "off": off(),
            "always": list(ALWAYS),
            "how": HOW}
