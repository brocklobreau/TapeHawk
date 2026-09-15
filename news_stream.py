"""
The Alpaca news websocket consumer.

This is the one component whose failure mode is silent. If it dies, the site
keeps serving yesterday's headlines and looks perfectly healthy -- no error
page, no 500, just a feed that quietly stopped. Everything below is built
around that: reconnect forever, never let one bad message kill the loop, and
publish a heartbeat the UI can check so "the feed is dead" is visible rather
than inferred.

Measured behaviour of this feed, 2026-09-15, 10 minutes during market hours:
  * delivery latency 0.10s fastest, 0.16s median (FMP, for contrast: 636s)
  * 0.8 headlines/minute, every one carrying ticker tags
  * 0 socket errors, 52 idle waits -- the connection is stable and the feed
    is genuinely that sparse, which is why classify.py's filtering matters
    as much as the speed does.

Runs as a daemon thread inside the web process. That is a deliberate choice
for v1: the work per message is parsing one small JSON object, so it cannot
meaningfully compete with request serving the way Bellwether's backtests do.
If volume ever grows, this moves to its own Render service and the only thing
that changes is where it writes.
"""
import json
import os
import ssl
import threading
import time
from datetime import datetime, timezone

import classify
import store

NEWS_WS = "wss://stream.data.alpaca.markets/v1beta1/news"

# Reconnect backoff. Starts fast because a blip should cost a second, not a
# minute, and caps so a prolonged outage does not turn into a hot loop
# hammering Alpaca and getting the key rate-limited.
BACKOFF_START = 1.0
BACKOFF_MAX = 60.0

_state = {
    "connected": False,
    "last_message_at": None,
    "last_connect_at": None,
    "last_error": None,
    "reconnects": 0,
    "received": 0,
    "stored": 0,
    "duplicates": 0,
    "filtered": 0,
}
_lock = threading.Lock()
_listeners = []          # queues for server-sent events


def status():
    with _lock:
        s = dict(_state)
    last = s.get("last_message_at")
    if last:
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(last)).total_seconds()
            s["seconds_since_last_message"] = round(age, 1)
        except ValueError:
            pass
    return s


def subscribe():
    """Register an SSE listener. Returns a queue the caller drains."""
    import queue
    q = queue.Queue(maxsize=200)
    with _lock:
        _listeners.append(q)
    return q


def unsubscribe(q):
    with _lock:
        if q in _listeners:
            _listeners.remove(q)


def _publish(article):
    """Push to every connected browser. A slow or dead client must never be
    able to block the ingest loop, so a full queue drops the message for that
    client rather than waiting -- they will pick it up on their next poll."""
    with _lock:
        targets = list(_listeners)
    for q in targets:
        try:
            q.put_nowait(article)
        except Exception:
            pass


def _creds():
    kid = os.environ.get("ALPACA_KEY_ID") or os.environ.get("APCA_API_KEY_ID")
    sec = os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("APCA_API_SECRET_KEY")
    return kid, sec


def _parse_ts(v):
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _handle(msg, log):
    if msg.get("T") != "n":
        return
    now = datetime.now(timezone.utc)
    created = _parse_ts(msg.get("created_at"))
    latency_ms = None
    if created:
        delta = (now - created).total_seconds() * 1000.0
        # Clock skew between Benzinga and us would otherwise be recorded as a
        # negative or absurd latency and poison the statistics the whole
        # premise of this service rests on.
        if -5000 <= delta <= 3600_000:
            latency_ms = int(delta)

    headline = (msg.get("headline") or "").strip()
    if not headline:
        return
    cats = classify.classify_headline(headline)
    noise = classify.is_noise(headline)

    article = {
        "alpaca_id": msg.get("id"),
        "created_at": (created or now).isoformat(),
        "received_at": now.isoformat(),
        "latency_ms": latency_ms,
        "headline": headline,
        "summary": (msg.get("summary") or "")[:600] or None,
        "author": msg.get("author"),
        "source": msg.get("source"),
        "url": msg.get("url"),
        "symbols": msg.get("symbols") or [],
        "categories": cats,
        "category_labels": [classify.CATEGORY_LABEL[c] for c in cats],
        "is_noise": noise,
    }

    with _lock:
        _state["received"] += 1
        _state["last_message_at"] = now.isoformat()
        if noise:
            _state["filtered"] += 1

    try:
        fresh = store.insert(article)
    except Exception as e:
        log(f"stream: store failed -- {e}")
        return
    with _lock:
        if fresh:
            _state["stored"] += 1
        else:
            _state["duplicates"] += 1
    # Log every stored headline. At the measured ~0.8/minute this is about 48
    # lines an hour -- trivial volume, and it is the only way to tell from
    # outside the process whether ingest is actually working. The failure mode
    # of this component is SILENT: if it stops, the site keeps serving old
    # headlines and looks perfectly healthy. Without this line the first sign
    # of trouble would be noticing the feed "seems quiet", which is
    # indistinguishable from a genuinely quiet news day.
    if fresh:
        lat = f"{latency_ms / 1000:.2f}s" if latency_ms is not None else "?"
        tags = ",".join(cats) or "-"
        syms = ",".join(article["symbols"][:4]) or "-"
        log(f"news [{lat}] [{tags}] [{syms}]"
            + (" FILTERED" if noise else "")
            + f" {headline[:96]}")

    # Only push genuinely new, non-filler headlines to open pages. A repeat of
    # a corrected article should not make the feed jump.
    if fresh and not noise:
        _publish(article)


def _run_once(kid, sec, log):
    from websocket import create_connection
    ws = create_connection(NEWS_WS, timeout=25,
                           sslopt={"cert_reqs": ssl.CERT_REQUIRED})
    try:
        ws.recv()                                     # greeting
        ws.send(json.dumps({"action": "auth", "key": kid, "secret": sec}))
        auth = ws.recv()
        if "authenticated" not in str(auth):
            raise RuntimeError(f"auth rejected: {str(auth)[:160]}")
        ws.send(json.dumps({"action": "subscribe", "news": ["*"]}))
        ws.recv()
        with _lock:
            _state["connected"] = True
            _state["last_connect_at"] = datetime.now(timezone.utc).isoformat()
            _state["last_error"] = None
        log("stream: connected to Alpaca news, subscribed to all symbols")

        # A read timeout is normal -- this feed is quiet for minutes at a
        # time. Only a real socket error should end the loop and trigger a
        # reconnect. Treating the two alike (as the first probe did) means a
        # dead socket looks exactly like a slow news day.
        ws.settimeout(40)
        last_beat = time.time()
        while True:
            try:
                raw = ws.recv()
            except Exception as e:
                if "timeout" in type(e).__name__.lower() or "timed out" in str(e).lower():
                    continue
                raise
            # Periodic proof-of-life. A quiet wire and a wedged reader look
            # identical in the logs otherwise, and "is it broken or is it just
            # slow news" is the question this service will get asked most.
            nowt = time.time()
            if nowt - last_beat > 900:
                last_beat = nowt
                with _lock:
                    st = dict(_state)
                log(f"stream: alive -- {st['stored']} stored, {st['filtered']} filtered, "
                    f"{st['duplicates']} duplicates, {st['reconnects']} reconnects "
                    f"since boot")
            if not raw:
                continue
            try:
                msgs = json.loads(raw)
            except ValueError:
                continue
            if isinstance(msgs, dict):
                msgs = [msgs]
            for m in msgs:
                try:
                    _handle(m, log)
                except Exception as e:
                    # One malformed article must never take down ingest.
                    log(f"stream: skipped a message -- {e}")
    finally:
        try:
            ws.close()
        except Exception:
            pass
        with _lock:
            _state["connected"] = False


def run_forever(log=print):
    kid, sec = _creds()
    if not kid or not sec:
        log("stream: ALPACA_KEY_ID / ALPACA_SECRET_KEY not set -- news feed disabled")
        with _lock:
            _state["last_error"] = "credentials not configured"
        return
    store.init()
    backoff = BACKOFF_START
    while True:
        try:
            _run_once(kid, sec, log)
            backoff = BACKOFF_START          # clean exit: reset the backoff
        except Exception as e:
            with _lock:
                _state["last_error"] = f"{type(e).__name__}: {str(e)[:160]}"
                _state["reconnects"] += 1
            log(f"stream: disconnected ({type(e).__name__}: {str(e)[:120]}) "
                f"-- reconnecting in {backoff:.0f}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)


def start(log=print):
    t = threading.Thread(target=run_forever, args=(log,), daemon=True,
                         name="alpaca-news")
    t.start()
    return t
