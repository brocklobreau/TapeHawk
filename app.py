"""
Tapehawk — real-time market headlines with instant company context.

Two halves, deliberately joined: a live feed of Benzinga headlines arriving
in ~0.15 seconds, and a research panel that answers "so what is this company
and is it cheap" without leaving the page. Newsquawk does the first far
better than this will (they have analysts on a live audio squawk); what they
do not do is put the company's cash flow next to the headline.

Architecture note: the websocket consumer runs as a daemon thread in this
process. Per message it parses one small JSON object, so unlike Bellwether's
backtests it cannot meaningfully compete with request serving. If the feed
ever gets dense this moves to its own service; nothing else would change.
"""
import gzip
import json
import os
import queue
import threading
import time
from datetime import datetime, timezone

from flask import Flask, Response, request, send_from_directory

import news_stream
import research
import store

app = Flask(__name__, static_folder=None)
HERE = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


# --- gzip: the page and the feed JSON both compress ~8x for ~4ms ------------
COMPRESSIBLE = ("text/html", "text/css", "application/javascript",
                "application/json", "image/svg+xml", "text/plain")


@app.after_request
def _compress(response):
    try:
        if "gzip" not in request.headers.get("Accept-Encoding", "").lower():
            return response
        # Never compress an SSE stream: it is open-ended, and buffering it to
        # compress would hold every headline until the connection closed --
        # turning a live feed into no feed at all.
        if response.mimetype == "text/event-stream":
            return response
        if not (200 <= response.status_code < 300):
            return response
        if response.headers.get("Content-Encoding"):
            return response
        if response.mimetype not in COMPRESSIBLE:
            return response
        if response.direct_passthrough:
            response.direct_passthrough = False
        data = response.get_data()
        if len(data) < 1024:
            return response
        packed = gzip.compress(data, 6)
        if len(packed) >= len(data):
            return response
        response.set_data(packed)
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Content-Length"] = str(len(packed))
        v = response.headers.get("Vary")
        response.headers["Vary"] = (v + ", Accept-Encoding") if v and "accept-encoding" not in v.lower() else (v or "Accept-Encoding")
    except Exception:
        return response
    return response


@app.route("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.route("/api/feed")
def api_feed():
    """Initial page load, and the polling fallback when SSE is unavailable."""
    try:
        rows = store.recent(
            limit=int(request.args.get("limit", 80)),
            since_id=request.args.get("since_id", type=int),
            symbol=(request.args.get("symbol") or "").strip() or None,
            category=(request.args.get("category") or "").strip() or None,
            include_noise=request.args.get("include_noise") == "1",
            search=(request.args.get("q") or "").strip() or None,
            min_importance=5 if request.args.get("big") == "1" else None,
        )
        return {"headlines": rows, "stats": store.stats(),
                "stream": news_stream.status()}
    except Exception as e:
        log(f"feed failed: {e}")
        return {"error": "Could not read the feed."}, 500


@app.route("/api/article/<int:article_id>")
def api_article(article_id):
    """The story behind a headline: Benzinga's own summary and body. Fetched
    on click rather than shipped with the feed, so the feed stays fast."""
    row = store.get(article_id)
    if not row:
        return {"error": "Not found"}, 404
    return row


@app.route("/api/stream")
def api_stream():
    """Server-sent events: headlines pushed the moment they arrive.

    SSE rather than a websocket because the traffic is one-directional and
    SSE reconnects on its own — a browser that sleeps on a phone comes back
    without any code from us. The keepalive comment every 20s matters: this
    feed is quiet for minutes at a time, and proxies close idle connections,
    which would look to the user like the news stopped.
    """
    def gen():
        q = news_stream.subscribe()
        try:
            yield "retry: 3000\n\n"
            last_ping = time.time()
            while True:
                try:
                    item = q.get(timeout=5)
                    yield f"data: {json.dumps(item)}\n\n"
                except queue.Empty:
                    if time.time() - last_ping > 20:
                        yield ": keepalive\n\n"
                        last_ping = time.time()
        except GeneratorExit:
            pass
        finally:
            news_stream.unsubscribe(q)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


@app.route("/api/lookup")
def api_lookup():
    raw = (request.args.get("ticker") or "").strip().upper()
    if not raw or len(raw) > 12 or not all(c.isalnum() or c in ".-" for c in raw):
        return {"error": "Enter a ticker symbol, for example NVDA or BRK-B."}, 400
    try:
        data = research.lookup(raw)
        data["news"] = store.recent(limit=25, symbol=raw)
        return data
    except research.FMPError as e:
        return {"error": f"Couldn't find data for “{raw}”. {e}"}, 404
    except Exception as e:
        log(f"lookup failed for {raw}: {e}")
        return {"error": "Lookup failed — try again shortly."}, 502


@app.route("/api/status")
def api_status():
    return {"stream": news_stream.status(), "store": store.stats(),
            "now": datetime.now(timezone.utc).isoformat()}


@app.route("/healthz")
def healthz():
    return {"ok": True}


_started = False
_start_lock = threading.Lock()


def start_once():
    """Guarded because gunicorn imports this module per worker. With more than
    one worker each would open its own Alpaca socket and write the same rows —
    so this service runs ONE worker, exactly like Bellwether, and for the same
    reason."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
        store.init()
        # Score anything that arrived before the Big News feature shipped, so
        # the rail reflects the whole archive rather than only what happens to
        # land after a deploy.
        try:
            import classify
            store.backfill_importance(classify.importance, classify.tone,
                                      classify.impact, log=log)
        except Exception as e:
            log(f"importance backfill skipped: {e}")
        news_stream.start(log=log)
        log("tapehawk: started")


start_once()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
