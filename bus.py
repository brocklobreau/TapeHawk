"""
The one stream every headline goes out on.

Whatever brought a headline in -- a wire's RSS poll, the rtpr socket, or
(before v2 step 9 switched it off) the Benzinga socket -- it leaves through
here: every open /api/stream connection (browsers, Halthawk) and the Snipe
tab's own listener hold a queue, and publish() drops the article into each.

This used to live inside news_stream.py, which meant the Benzinga socket
owned the stream and the wires borrowed it. Now that the wires ARE the
stream, the queues live on their own and neither module needs the other.

A slow or dead listener must never be able to hold up ingest, so a full
queue drops the message for that listener rather than waiting -- a browser
picks it up on its next poll, Halthawk on its /api/feed?since_id poll.
"""
import queue
import threading
from datetime import datetime, timezone

QUEUE_MAX = 200

_lock = threading.Lock()
_listeners = []
_state = {"published": 0, "dropped": 0, "last_published_at": None}


def subscribe():
    """Register a listener. Returns the queue the caller drains."""
    q = queue.Queue(maxsize=QUEUE_MAX)
    with _lock:
        _listeners.append(q)
    return q


def unsubscribe(q):
    with _lock:
        if q in _listeners:
            _listeners.remove(q)


def publish(article):
    """Push one article to every listener. Never blocks, never raises."""
    with _lock:
        targets = list(_listeners)
        _state["published"] += 1
        _state["last_published_at"] = datetime.now(timezone.utc).isoformat()
    for q in targets:
        try:
            q.put_nowait(article)
        except Exception:
            with _lock:
                _state["dropped"] += 1


# The old name, so anything still holding news_stream._publish keeps working.
_publish = publish


def listeners():
    with _lock:
        return len(_listeners)


def status():
    with _lock:
        s = dict(_state)
    s["listeners"] = listeners()
    return s
