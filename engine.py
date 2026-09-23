"""
The bot. One idea, stated once:

    A stock halts to the upside on real news. The headline that says so lands
    on the wire while the stock is still frozen. When it reopens it usually
    jumps. Be in for the jump, and be out the moment the jump stops.

Nothing here predicts. The entry is mechanical (the reopen), and the exit is
whatever the tape does next: the price gives back a slice of its high, or
stops making highs, or breaks the entry. There is no profit target because
the whole edge is not knowing how far it goes, and no timer because a 20-
second trade and a four-minute trade are the same trade.

Lifecycle of one trade:

    headline  -> parse_halt_headline()   is this a halt-to-the-upside story?
              -> guards                  enabled, hours, size, price, count, losses
    waiting   -> wait_for_reopen()       first print after the freeze
    entering  -> marketable limit buy    never a market order into a reopen
    holding   -> exit_reason() each tick trail / stall / stop / fuse / close
    exiting   -> marketable limit sell   re-priced until it fills
    done      -> P&L, seconds held, reason -- written down, shown, never hidden

Everything that can be a pure function is one, so the rules can be tested
without a broker. Everything that touches a broker goes through the objects
passed into Engine(), so the whole lifecycle runs against a fake in tests.
"""
import math
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone

import store

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                                    # pragma: no cover
    ET = None

# ---- settings ----------------------------------------------------------------
# Every one of these can be changed from the dashboard; the store keeps the
# override. A note on size: the paper simulator fills any quantity at the
# quote, so a $10,000 order on a thin small cap fills perfectly on paper and
# would NOT in real life -- it would walk the book and get a worse average.
# Paper results at this size are an upper bound on what the same rules would
# do with real money; when it goes live, size to the stock, not the account.
DEFAULTS = {
    "enabled": True,
    "dollars_per_trade": 10000.0,     # sized for a $100k paper account; see the note on fills below
    "max_positions": 3,
    "max_trades_per_symbol_day": 1,
    "daily_loss_limit": 2000.0,       # stop taking NEW trades after losing this much today
    "min_price": 1.0,
    "max_price": 200.0,
    "min_headline_pct": 10.0,         # "Stock Now Up 8%" is not a halt worth chasing
    "trail_pct": 4.0,                 # out when price gives back this much from its high
    "trail_arm_pct": 3.0,             # the trail only switches on once the stock is this far above the entry
    "stop_pct": 10.0,                 # out when price is this far under the entry -- room for the dip before the move
    "stall_seconds": 45,              # out when no new high for this long and price is off the high
    "stall_min_off_high_pct": 1.0,
    "max_hold_seconds": 900,          # a fuse, not a strategy: 15 minutes and it is not a quick trade
    "entry_slip_pct": 1.0,            # limit = ask * (1 + this)
    "exit_slip_pct": 1.5,             # limit = bid * (1 - this)
    "entry_timeout_s": 8,
    "entry_retries": 1,
    "exit_reprice_s": 5,
    "exit_reprice_pct": 3.0,
    "max_chase_pct": 8.0,             # skip if price is already this far above the reopen print
    "late_seconds": 90,               # headline older than this on a stock already trading = late
    "already_trading_s": 5,           # a print this fresh means it is not halted right now
    "reopen_wait_max_s": 1200,        # T1 news halts can run long; give up after this
    "regular_hours_only": True,
    "close_flat_time": "15:55",       # ET; exit everything by then
    "halt_silence_s": 10,             # no quotes or prints for this long while holding = halted again
    "min_halt_news_importance": 7,    # Halts-tab signal: the story must score at least this (Big News is 5)
    "max_halt_seq": 2,                # Halts-tab signal: only the 1st or 2nd halt of the day
}

HALT_UP = re.compile(r"halt(?:ed|s)?\b.*?(?:to the )?upside|circuit breaker[^.]*?upside|"
                     r"halt(?:ed|s)?\b.*?\bup\s+\d", re.I)
HALT_DOWN = re.compile(r"halt(?:ed|s)?\b.*?(?:to the )?downside|circuit breaker[^.]*?downside|"
                       r"halt(?:ed|s)?\b.*?\bdown\s+\d", re.I)
RESUME = re.compile(r"\bresum(?:e|es|ed|ption)\b", re.I)
PCT = re.compile(r"(?:up|down)\s+(\d+(?:\.\d+)?)\s*%", re.I)


def parse_halt_headline(headline):
    """What kind of halt story is this, and how far was the stock up?

    Returns {"kind": "halt_up" | "halt_down" | "resume", "pct": float|None}
    or None when the headline is not about a halt at all. The Benzinga wire
    form is 'XYZ Shares Halted On Circuit Breaker To The Upside, Stock Now
    Up 141.72%'; the resume form is 'XYZ Shares Resume Trade, Stock Up 95%'.
    """
    h = headline or ""
    if not re.search(r"\bhalt|circuit breaker|\bresum", h, re.I):
        return None
    m = PCT.search(h)
    pct = float(m.group(1)) if m else None
    if RESUME.search(h) and not re.search(r"\bhalt", h, re.I):
        kind = "resume"
    elif HALT_DOWN.search(h):
        kind = "halt_down"
    elif HALT_UP.search(h):
        kind = "halt_up"
    elif re.search(r"\bhalt", h, re.I) and m and re.search(r"\bup\b", h, re.I):
        kind = "halt_up"
    elif re.search(r"\bhalt", h, re.I):
        kind = "halt_unknown"
    else:
        return None
    return {"kind": kind, "pct": pct}


def size(dollars, price):
    if not price or price <= 0:
        return 0
    return int(math.floor(dollars / price))


def exit_reason(pos, price, now_s, cfg):
    """Pure. pos: dict with entry_price, high, high_at (epoch s), entry_at
    (epoch s). Returns a reason string or None."""
    entry = pos["entry_price"]
    high = max(pos.get("high") or entry, price)
    if price <= entry * (1 - cfg["stop_pct"] / 100):
        return "stop"
    # The trail is armed only once the stock has actually run. Before that a
    # tick 0.1% over the entry followed by a 4% wobble would have counted as
    # "gave back 4% from the high" and sold into the dip that comes before
    # the move -- the VSA pattern: open 5.50, dip to 5.10, rip to 6.30.
    armed = high >= entry * (1 + cfg.get("trail_arm_pct", 0) / 100)
    if armed and price <= high * (1 - cfg["trail_pct"] / 100):
        return "trail"
    off_high = (high - price) / high * 100 if high else 0
    if (now_s - (pos.get("high_at") or pos["entry_at"]) >= cfg["stall_seconds"]
            and off_high >= cfg["stall_min_off_high_pct"]):
        return "stall"
    if now_s - pos["entry_at"] >= cfg["max_hold_seconds"]:
        return "fuse"
    return None


def et_now():
    return datetime.now(ET) if ET else datetime.now(timezone.utc)


def in_regular_hours(now=None):
    n = now or et_now()
    if n.weekday() >= 5:
        return False
    m = n.hour * 60 + n.minute
    return 9 * 60 + 30 <= m < 16 * 60


def past_flat_time(cfg, now=None):
    n = now or et_now()
    try:
        hh, mm = (cfg.get("close_flat_time") or "15:55").split(":")
        return n.hour * 60 + n.minute >= int(hh) * 60 + int(mm)
    except ValueError:
        return False


class Engine:
    def __init__(self, trading, latest_trade, latest_quote, stream, log=print, paper=True, clock=time.time):
        self.trading = trading
        self.latest_trade = latest_trade
        self.latest_quote = latest_quote
        self.stream = stream            # .watch(sym) / .unwatch(sym)
        self.log = log
        self.paper = paper
        self.clock = clock
        self.live = {}                  # symbol -> Trade
        self.lock = threading.Lock()
        self.started_at = time.time()
        self.headlines_seen = 0
        self.halt_headlines = 0

    # ---- settings ------------------------------------------------------------
    def cfg(self):
        c = dict(DEFAULTS)
        c.update({k: v for k, v in store.all_settings().items() if k in DEFAULTS})
        return c

    def set(self, key, value):
        if key not in DEFAULTS:
            raise ValueError(f"unknown setting {key}")
        kind = type(DEFAULTS[key])
        if kind is bool:
            value = str(value).lower() in ("1", "true", "on", "yes")
        elif kind is int:
            value = int(float(value))
        elif kind is float:
            value = float(value)
        else:
            value = str(value)
        store.set_setting(key, value)
        store.event(f"setting {key} = {value}", "info")
        return value

    # ---- signals -------------------------------------------------------------
    def on_headline(self, h):
        self.headlines_seen += 1
        info = parse_halt_headline(h.get("headline"))
        if not info:
            return None
        self.halt_headlines += 1
        syms = [s for s in (h.get("symbols") or []) if s and s.isalnum() and len(s) <= 5]
        sym = syms[0] if syms else None
        if info["kind"] != "halt_up":
            store.add_signal(sym or "?", h.get("headline"), h.get("url"), info["pct"], h.get("source"),
                             "skipped", f"not a halt to the upside ({info['kind']})")
            return None
        if not sym:
            store.add_signal("?", h.get("headline"), h.get("url"), info["pct"], h.get("source"),
                             "skipped", "no ticker on the headline")
            return None
        return self._signal(sym, h, info, "halt to the upside")

    def on_halt(self, halt):
        """A news-backed halt from Tapehawk's Halts tab (the halt_watch
        poller). The halt itself is the signal; the story is why it counts.
        Same guards and the same trade as a wire headline."""
        self.halt_headlines += 1
        sym = halt.get("symbol")
        h = {"headline": halt.get("news_headline") or f"{sym} halted ({halt.get('code')})",
             "symbols": [sym], "url": halt.get("news_url"),
             "created_at": halt.get("halted_at"), "source": "tapehawk-halts"}
        info = {"kind": "halt_up", "pct": halt.get("run_in_pct")}
        why_taken = (f"news-backed halt #{halt.get('seq') or '?'} of the day, "
                     f"story importance {halt.get('news_importance')}")
        return self._signal(sym, h, info, why_taken)

    def _signal(self, sym, h, info, why_taken):
        cfg = self.cfg()
        why = self.guard(sym, info, cfg)
        if why:
            store.add_signal(sym, h.get("headline"), h.get("url"), info["pct"], h.get("source"), "skipped", why)
            self.log(f"skip {sym}: {why}")
            return None
        is_test = (h.get("source") == "test")
        if is_test:
            why_taken = "test halt (not counted in stats)"
        sid = store.add_signal(sym, h.get("headline"), h.get("url"), info["pct"], h.get("source"), "taken", why_taken)
        tid = store.new_trade(sid, sym, paper=self.paper, test=is_test)
        store.set_signal_decision(sid, "taken", why_taken, trade_id=tid)
        t = Trade(self, sym, tid, sid, h, info, cfg)
        with self.lock:
            self.live[sym] = t
        t.start()
        store.event(f"signal taken: {h.get('headline')}", "info", sym)
        return t

    def guard(self, sym, info, cfg):
        if not cfg["enabled"]:
            return "bot is paused"
        if cfg["regular_hours_only"] and not in_regular_hours():
            return "outside regular hours"
        if past_flat_time(cfg):
            return "too close to the close"
        if info["pct"] is not None and info["pct"] < cfg["min_headline_pct"]:
            return f"headline move {info['pct']:g}% is under the {cfg['min_headline_pct']:g}% minimum"
        with self.lock:
            if sym in self.live:
                return "already in this symbol"
            if len(self.live) >= cfg["max_positions"]:
                return f"already holding {len(self.live)} (max {cfg['max_positions']})"
        taken = [s for s in store.signals_today(sym) if s["decision"] == "taken"]
        if len(taken) >= cfg["max_trades_per_symbol_day"]:
            return f"already traded {sym} {len(taken)}x today"
        pnl, _n = store.pnl_today()
        if pnl <= -abs(cfg["daily_loss_limit"]):
            return f"daily loss limit hit ({pnl:+.2f})"
        return None

    # ---- ticks ---------------------------------------------------------------
    def on_trade(self, sym, price, qty, at):
        t = self.live.get(sym)
        if t:
            t.tick(price, at)

    def on_quote(self, sym, bid, ask, at):
        t = self.live.get(sym)
        if t:
            t.quote(bid, ask)

    def _finish(self, sym):
        with self.lock:
            self.live.pop(sym, None)
        try:
            self.stream.unwatch(sym)
        except Exception:
            pass

    # ---- controls ------------------------------------------------------------
    def flatten(self, reason="manual"):
        n = 0
        with self.lock:
            trades = list(self.live.values())
        for t in trades:
            t.force_exit = reason
            n += 1
        store.event(f"flatten requested ({n} open)", "warn")
        return n

    def recover(self):
        """Called once after the threads start. Every trade the database says
        is still in flight is reconciled with the broker: a position there is
        adopted and managed to its exit; no position means the shares were
        never bought (or were sold by hand), and the row is closed out with a
        note. Returns (adopted, closed)."""
        adopted = closed = 0
        try:
            positions = {p.get("symbol"): p for p in (self.trading.positions() or [])}
        except Exception as e:
            self.log(f"recover: could not read positions ({str(e)[:100]})")
            store.event(f"recovery skipped: could not read broker positions ({str(e)[:100]})", "error")
            return 0, 0
        cfg = self.cfg()
        for row in store.open_trades():
            sym = row["symbol"]
            pos = positions.get(sym)
            if pos and float(pos.get("qty") or 0) > 0 and row["state"] in ("holding", "exiting", "entering"):
                t = Trade.adopt(self, row, pos, cfg)
                with self.lock:
                    self.live[sym] = t
                t.start()
                adopted += 1
            else:
                why = ("restart: no position at broker" if row["state"] in ("holding", "exiting", "entering")
                       else "restart before the reopen")
                store.update_trade(row["id"], state="abandoned", exit_reason=why,
                                   notes=((row.get("notes") or "") + "; " if row.get("notes") else "") + why)
                store.set_signal_decision(row["signal_id"], "abandoned", why)
                store.event(f"closed out after restart: {why}", "warn", sym)
                closed += 1
        # Positions at the broker that no trade row explains are reported, not
        # touched: they may be the user's own.
        for sym, pos in positions.items():
            if sym not in self.live and float(pos.get("qty") or 0) > 0:
                store.event(f"broker holds {pos.get('qty')} {sym} that no trade explains -- left alone", "warn", sym)
        if adopted or closed:
            store.event(f"recovery: adopted {adopted}, closed out {closed}", "info")
        return adopted, closed

    def status(self):
        with self.lock:
            live = [t.snapshot() for t in self.live.values()]
        return {"open": live, "headlines_seen": self.headlines_seen,
                "halt_headlines": self.halt_headlines, "started_at": self.started_at}


class Trade(threading.Thread):
    def __init__(self, eng, sym, trade_id, signal_id, headline, info, cfg):
        super().__init__(name=f"trade-{sym}", daemon=True)
        self.eng, self.sym, self.id, self.sid = eng, sym, trade_id, signal_id
        self.headline, self.info, self.cfg = headline, info, cfg
        self.state = "waiting_reopen"
        self.ticks = deque(maxlen=500)      # (epoch_s, price)
        self.bid = self.ask = None
        self.baseline_at = None             # last print before the freeze (epoch s)
        self.signal_at = time.time()
        self.reopen_price = None
        self.entry_price = None
        self.entry_at = None
        self.qty = 0
        self.high = None
        self.high_at = None
        self.last = None
        self.last_at = None
        self.force_exit = None
        self.exit_reason = None
        self.notes = []
        self.quote_at = None
        self.halted_again = False
        self._rest_at = None
        self._rest_noted = False
        self.adopted = False

    def log(self, msg, level="info"):
        self.eng.log(f"{self.sym}: {msg}")
        store.event(msg, level, self.sym)

    def tick(self, price, at):
        ts = at.timestamp() if hasattr(at, "timestamp") else float(at)
        self.ticks.append((ts, price))
        self.last, self.last_at = price, ts
        self.quote_at = time.time()
        if self.state == "holding":
            if self.high is None or price > self.high:
                self.high, self.high_at = price, ts

    def quote(self, bid, ask):
        self.bid, self.ask = bid or None, ask or None
        self.quote_at = time.time()

    def _rest_tick(self):
        """When the stream is silent, ask REST for the latest print. A print
        newer than anything we have means the STOCK is trading and only our
        stream is quiet -- so the print is fed in as a tick and the rules run
        on it. No newer print means the stock really is halted. Rate-limited
        to one call every two seconds per trade."""
        now = time.time()
        if now - (self._rest_at or 0) < 2:
            return False
        self._rest_at = now
        try:
            lt = self.eng.latest_trade(self.sym)
        except Exception:
            return False
        if not lt or not lt.get("at"):
            return False
        ts = lt["at"].timestamp()
        if self.last_at is None or ts > self.last_at + 0.001:
            if now - ts < self.cfg["halt_silence_s"]:
                if not self._rest_noted:
                    self._rest_noted = True
                    self.log("stream quiet but REST shows fresh prints -- managing on REST prices", "warn")
                self.tick(lt["price"], lt["at"])
                return True
        return False

    def tape_silent(self):
        """A stock that has halted AGAIN goes completely quiet: no prints and
        no quote updates. The exit rules must not run on a frozen price --
        the stall timer would fire, and the sell ladder would walk the limit
        down through a halt that reopens higher. So while the tape is silent
        the trade just waits, and the rules pick up on the first print back."""
        if self.quote_at is None:
            return False
        return time.time() - self.quote_at > self.cfg["halt_silence_s"]

    def snapshot(self):
        return {"id": self.id, "symbol": self.sym, "state": self.state, "headline": self.headline.get("headline"),
                "pct_in_headline": self.info.get("pct"), "signal_at": self.signal_at,
                "reopen_price": self.reopen_price, "entry_price": self.entry_price, "entry_at": self.entry_at,
                "qty": self.qty, "high": self.high, "last": self.last, "last_at": self.last_at,
                "bid": self.bid, "ask": self.ask,
                "trail_at": round(self.high * (1 - self.cfg["trail_pct"] / 100), 4) if self.high else None,
                "stop_at": round(self.entry_price * (1 - self.cfg["stop_pct"] / 100), 4) if self.entry_price else None,
                "unrealised": round((self.last - self.entry_price) * self.qty, 2) if (self.last and self.entry_price) else None,
                "unrealised_pct": round((self.last - self.entry_price) / self.entry_price * 100, 2) if (self.last and self.entry_price) else None,
                "seconds": round(time.time() - self.entry_at, 1) if self.entry_at else None}

    # ---- lifecycle -----------------------------------------------------------
    @classmethod
    def adopt(cls, eng, row, position, cfg):
        """Rebuild a trade that was holding when the process died -- a deploy,
        a crash -- from its database row and the broker's live position, and
        carry on managing the exit. Without this a redeploy mid-trade leaves
        shares sitting at the broker with nobody watching them."""
        t = cls(eng, row["symbol"], row["id"], row["signal_id"],
                {"headline": "(adopted after restart)"}, {"pct": None}, cfg)
        t.adopted = True
        t.qty = int(float(position.get("qty") or row.get("qty") or 0))
        t.entry_price = float(row.get("entry_price") or position.get("avg_entry_price") or 0)
        try:
            t.entry_at = datetime.fromisoformat(row["entry_at"]).timestamp()
        except (TypeError, ValueError):
            t.entry_at = time.time()
        t.high = max(float(row.get("high") or 0), t.entry_price)
        t.high_at = t.entry_at
        t.reopen_price = row.get("reopen_price")
        t.state = "holding"
        t.notes.append("adopted after restart")
        return t

    def run(self):
        if self.adopted:
            try:
                self.eng.stream.watch(self.sym)
                self.log(f"adopted {self.qty} shares @ {self.entry_price:.2f} after a restart -- managing the exit", "warn")
                self._set("holding", notes="; ".join(self.notes))
                self.hold()
                self.exit()
            except Exception as e:
                self.log(f"adopted trade error: {str(e)[:160]}", "error")
            finally:
                self.eng._finish(self.sym)
            return
        try:
            self.eng.stream.watch(self.sym)
            if not self.wait_for_reopen():
                return self.abandon(self.exit_reason or "no reopen")
            if not self.enter():
                return self.abandon(self.exit_reason or "no fill")
            self.hold()
            self.exit()
        except Exception as e:
            self.log(f"trade thread error: {str(e)[:160]}", "error")
            try:
                if self.qty and self.state in ("holding", "exiting"):
                    self.exit_reason = "error"
                    self.exit()
            except Exception as e2:
                self.log(f"could not exit after error: {str(e2)[:120]}", "error")
        finally:
            self.eng._finish(self.sym)

    def _set(self, state, **fields):
        self.state = state
        store.update_trade(self.id, state=state, **fields)

    def _fresh_ticks(self, after_s):
        return [(ts, p) for ts, p in self.ticks if ts > after_s]

    def wait_for_reopen(self):
        cfg = self.cfg
        try:
            lt = self.eng.latest_trade(self.sym)
        except Exception as e:
            self.log(f"latest trade lookup failed: {str(e)[:100]}", "warn")
            lt = None
        now = time.time()
        if lt and lt.get("at"):
            age = now - lt["at"].timestamp()
            self.baseline_at = lt["at"].timestamp()
            if age < cfg["already_trading_s"]:
                # Not frozen. Either the headline is late or the halt is over.
                head_age = now - self.signal_at
                created = self.headline.get("created_at")
                if created:
                    try:
                        from alpaca import _ts
                        c = _ts(created)
                        if c:
                            head_age = now - c.timestamp()
                    except Exception:
                        pass
                if head_age > cfg["late_seconds"]:
                    self.exit_reason = f"late: already trading, headline {head_age:.0f}s old"
                    return False
                self.reopen_price = lt["price"]
                self.notes.append("entered on a stock already trading (headline fresh)")
                self._set("entering", reopen_at=store.now(), reopen_price=self.reopen_price)
                self.log(f"already trading at {lt['price']:.2f}, headline is fresh -- entering")
                return True
        else:
            self.baseline_at = now
        self._set("waiting_reopen")
        self.log(f"halted -- waiting for the reopen (last print {lt['price']:.2f})" if lt else "waiting for the reopen")
        deadline = now + cfg["reopen_wait_max_s"]
        last_poll = 0
        while time.time() < deadline:
            if self.force_exit:
                self.exit_reason = "cancelled before reopen"
                return False
            fresh = self._fresh_ticks(self.baseline_at + 0.001)
            if fresh:
                self.reopen_price = fresh[0][1]
                break
            if time.time() - last_poll >= 2:
                last_poll = time.time()
                try:
                    lt2 = self.eng.latest_trade(self.sym)
                    if lt2 and lt2.get("at") and lt2["at"].timestamp() > self.baseline_at + 0.001:
                        self.reopen_price = lt2["price"]
                        self.tick(lt2["price"], lt2["at"])
                        break
                except Exception:
                    pass
            time.sleep(0.2)
        if self.reopen_price is None:
            self.exit_reason = "reopen never came"
            return False
        self._set("entering", reopen_at=store.now(), reopen_price=self.reopen_price)
        self.log(f"reopened at {self.reopen_price:.2f}")
        return True

    def _ref_price(self, side):
        try:
            q = self.eng.latest_quote(self.sym)
        except Exception:
            q = None
        if q:
            self.quote(q.get("bid"), q.get("ask"))
        px = (self.ask if side == "buy" else self.bid) or self.last or self.reopen_price
        return px

    def _wait_fill(self, order_id, timeout):
        end = time.time() + timeout
        o = None
        while time.time() < end:
            o = self.eng.trading.order(order_id)
            st = o.get("status")
            if st == "filled":
                return o
            if st in ("canceled", "expired", "rejected"):
                return o
            time.sleep(0.4)
        return o

    def enter(self):
        cfg = self.cfg
        for attempt in range(cfg["entry_retries"] + 1):
            ref = self._ref_price("buy")
            if not ref:
                self.exit_reason = "no price to buy at"
                return False
            if ref > self.reopen_price * (1 + cfg["max_chase_pct"] / 100):
                self.exit_reason = f"ran away: {ref:.2f} is {(ref / self.reopen_price - 1) * 100:.1f}% over the reopen"
                return False
            if not (cfg["min_price"] <= ref <= cfg["max_price"]):
                self.exit_reason = f"price {ref:.2f} outside {cfg['min_price']}-{cfg['max_price']}"
                return False
            limit = round(ref * (1 + cfg["entry_slip_pct"] / 100), 2 if ref >= 1 else 4)
            qty = size(cfg["dollars_per_trade"], limit)
            if qty < 1:
                self.exit_reason = "position size rounds to zero shares"
                return False
            o = self.eng.trading.submit(self.sym, qty, "buy", limit_price=limit, client_id=f"hh-{self.id}-b{attempt}")
            self.log(f"buy {qty} @ limit {limit:.2f} (attempt {attempt + 1})")
            o = self._wait_fill(o["id"], cfg["entry_timeout_s"])
            filled = int(float(o.get("filled_qty") or 0))
            if o.get("status") != "filled":
                self.eng.trading.cancel(o["id"])
                time.sleep(0.3)
                o = self.eng.trading.order(o["id"])
                filled = int(float(o.get("filled_qty") or 0))
            if filled > 0:
                self.qty = filled
                self.entry_price = float(o.get("filled_avg_price") or limit)
                self.entry_at = time.time()
                self.high, self.high_at = self.entry_price, self.entry_at
                if self.last and self.last > self.high:
                    self.high, self.high_at = self.last, self.last_at or self.entry_at
                self._set("holding", entry_at=store.now(), entry_price=self.entry_price, qty=self.qty,
                          entry_order_id=o["id"], high=self.high)
                self.log(f"filled {filled} @ {self.entry_price:.2f}" + (" (partial)" if filled < qty else ""))
                return True
            self.log("no fill, re-pricing" if attempt < cfg["entry_retries"] else "no fill, giving up", "warn")
        self.exit_reason = "no fill"
        return False

    def hold(self):
        cfg = self.cfg
        last_write = 0
        while True:
            if self.force_exit:
                self.exit_reason = self.force_exit
                return
            if past_flat_time(cfg):
                self.exit_reason = "close"
                return
            now = time.time()
            if now - self.entry_at >= cfg["max_hold_seconds"]:
                self.exit_reason = "fuse"
                return
            if self.tape_silent() and not self._rest_tick():
                if not self.halted_again:
                    self.halted_again = True
                    self.log(f"tape went silent at {self.last:.2f} -- looks halted again, holding through it" if self.last else "tape silent -- holding")
                    store.update_trade(self.id, notes="halted again while holding")
                    self.notes.append("halted again while holding")
                time.sleep(0.25)
                continue
            if self.halted_again:
                self.halted_again = False
                self.log(f"tape is back at {self.last:.2f} (high was {self.high:.2f})" if self.last else "tape is back")
            if self.last is not None:
                pos = {"entry_price": self.entry_price, "high": self.high, "high_at": self.high_at, "entry_at": self.entry_at}
                r = exit_reason(pos, self.last, now, cfg)
                if r:
                    self.exit_reason = r
                    return
            elif now - self.entry_at >= cfg["max_hold_seconds"]:
                self.exit_reason = "fuse"
                return
            if now - last_write >= 2:
                last_write = now
                store.update_trade(self.id, high=self.high, last=self.last,
                                   last_at=datetime.fromtimestamp(self.last_at, timezone.utc).isoformat() if self.last_at else None)
            time.sleep(0.25)

    def exit(self):
        cfg = self.cfg
        if not self.qty:
            return
        self._set("exiting", exit_reason=self.exit_reason)
        self.log(f"exit: {self.exit_reason} (last {self.last:.2f}, high {self.high:.2f})" if self.last else f"exit: {self.exit_reason}")
        remaining = self.qty
        proceeds = 0.0
        sold = 0
        order_id = None
        for attempt in range(4):
            # Never walk the ladder down through a halt: wait for the tape.
            waited = 0
            while self.tape_silent() and not self._rest_tick() and waited < cfg["reopen_wait_max_s"]:
                if waited == 0:
                    self.log("tape silent while exiting -- halted again, waiting for the reopen before selling", "warn")
                time.sleep(0.5)
                waited += 0.5
            ref = self._ref_price("sell") or self.last or self.entry_price
            off = cfg["exit_slip_pct"] + attempt * cfg["exit_reprice_pct"]
            limit = round(ref * (1 - off / 100), 2 if ref >= 1 else 4)
            if attempt == 3:
                o = self.eng.trading.submit(self.sym, remaining, "sell", client_id=f"hh-{self.id}-s{attempt}")
                self.log(f"sell {remaining} at MARKET (last resort)", "warn")
            else:
                o = self.eng.trading.submit(self.sym, remaining, "sell", limit_price=limit, client_id=f"hh-{self.id}-s{attempt}")
                self.log(f"sell {remaining} @ limit {limit:.2f}")
            order_id = o["id"]
            o = self._wait_fill(order_id, cfg["exit_reprice_s"] if attempt < 3 else 20)
            filled = int(float(o.get("filled_qty") or 0))
            if o.get("status") != "filled":
                self.eng.trading.cancel(order_id)
                time.sleep(0.3)
                o = self.eng.trading.order(order_id)
                filled = int(float(o.get("filled_qty") or 0))
            if filled:
                proceeds += filled * float(o.get("filled_avg_price") or limit)
                sold += filled
                remaining -= filled
            if remaining <= 0:
                break
        if remaining > 0:
            self.log(f"{remaining} shares still open after 4 attempts -- closing position", "error")
            try:
                self.eng.trading.close_position(self.sym)
            except Exception as e:
                self.log(f"close_position failed: {str(e)[:100]}", "error")
        exit_price = round(proceeds / sold, 4) if sold else None
        pnl = round((exit_price - self.entry_price) * sold, 2) if exit_price else None
        pnl_pct = round((exit_price / self.entry_price - 1) * 100, 2) if exit_price else None
        held = round(time.time() - self.entry_at, 1)
        self._set("done", exit_at=store.now(), exit_price=exit_price, exit_order_id=order_id,
                  pnl=pnl, pnl_pct=pnl_pct, seconds_held=held, high=self.high, last=self.last,
                  notes="; ".join(self.notes) or None)
        self.log(f"done: {pnl_pct:+.2f}% ({pnl:+.2f}) in {held:.0f}s, exit on {self.exit_reason}" if pnl is not None
                 else f"done with no fill recorded ({self.exit_reason})", "info" if (pnl or 0) >= 0 else "warn")

    def abandon(self, why):
        self._set("abandoned", exit_reason=why, notes="; ".join(self.notes) or None)
        store.set_signal_decision(self.sid, "abandoned", why)
        self.log(f"abandoned: {why}", "warn")
