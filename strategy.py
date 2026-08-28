"""
strategy.py — the decision engine. Pure logic, no network, no money.

Feed it a price once per second and it tells you what to do: BUY, TP1, TP2, KILL.
Because it's pure, you can replay a whole day of prices through it in a second —
that's what the dry-run tooling in Module 07 does.

THE SHAPE OF A TRADE
  WAIT  you tapped greenlight. We watch. We do NOT buy the price you tapped at.
        First we need a dip (-15% off the high), then a bounce (+4% off that low).
        That's the "reclaim": let the froth flush, buy the recovery.
  POS   we're in. Sell 70% at 1.5x, the last 30% at 2x. If price falls 50% from
        its peak while we hold, dump everything.
  DONE  flat. Tap again if you want another round.
"""
import statistics

# The two gates that throw the dip away. Their message survives the reset,
# because "the setup vanished, and here is why" is the one explanation the
# panel cannot reconstruct on its own.
FELL_THROUGH = "still falling — the dip reset, waiting for a fresh one"
RAN_AWAY = "ran back up near the high — the dip reset, waiting for a lower entry"
RESET_REASONS = (FELL_THROUGH, RAN_AWAY)

import time
from collections import deque

import config as C


class Session:
    """One coin, one greenlight, from tap to flat."""

    def __init__(self, mint, name, volr=None, swaps_per_sec=None, cfg=None):
        self.mint, self.name = mint, name
        self.volr = volr                  # buy vol / sell vol over the last 5m

        # Your settings, resolved ONCE when the session starts. Deliberately not
        # re-read while a trade is open: editing a take-profit on a position
        # you're already holding is how people talk themselves into moving a stop.
        cfg = cfg or {}
        self.dip = float(cfg.get("dip", C.DIP))
        self.bounce = float(cfg.get("bounce", C.BOUNCE))
        self.kill = float(cfg.get("kill", C.KILL))
        # 1 = give back `kill` from the peak since entry (rides up with the
        # price). 0 = a plain stop, `kill` below the entry, which never exits a
        # position that is still above where you bought.
        self.trail_stop = bool(float(cfg.get("trail_stop", 1)))
        # Once TP1 has sold, refuse to let the stop on what's left sit below
        # the entry. Only meaningful with a second rung — with one rung the
        # whole position leaves at TP1 and there is nothing to protect.
        self.be_after_tp1 = bool(float(cfg.get("be_after_tp1", 0)))
        self.deadcat = float(cfg.get("deadcat", C.DEADCAT))
        self.pico = float(cfg.get("pico", C.PICO))
        self.volr_min = float(cfg.get("volr_min", C.VOLR_MIN))
        if "tp1" in cfg:
            if float(cfg.get("use_tp2", 1)):
                self.tps = (float(cfg["tp1"]), float(cfg.get("tp2", C.TPS[1])))
                f1 = float(cfg.get("frac1", C.FRACS[0]))
                self.fracs = (f1, round(1.0 - f1, 6))
            else:
                # One rung: sell the whole position at TP1 and re-greenlight if
                # you want another round.
                self.tps, self.fracs = (float(cfg["tp1"]),), (1.0,)
        else:
            self.tps, self.fracs = C.TPS, C.FRACS

        # How long to watch before a buy is allowed. This used to be derived
        # from the coin's swap rate — 250 swaps of "evidence" converted into
        # polls — which was inherited from a backtest that read every swap
        # rather than polling once a second. Nothing downstream needed 250
        # observations: the only mechanism with a real requirement is the
        # outlier median, which wants 10. The slope filter it also fed has
        # been removed, so what is left is a plain question with a plain
        # answer — how many seconds of tape before you will act on it.
        self.warmup = int(round(
            max(C.WARMUP_MIN_SEC, min(C.WARMUP_MAX_SEC,
                float(cfg.get("warmup_sec", C.WARMUP_SEC)))) / C.POLL_SEC))

        # Long enough for the 15-poll outlier median with room to spare. Not a
        # strategy window any more, just a buffer.
        self.prices = deque(maxlen=C.PRICE_WIN)
        # How many polls the median spans. 1 acts on the price as read; higher
        # ignores single-poll noise at the cost of reacting that much later.
        self.smoothing = max(1, int(float(cfg.get("smoothing", C.SMOOTHING))))
        self.win = deque(maxlen=self.smoothing)
        self.n = 0                        # polls seen

        self.raw_tap = None               # first RAW price — the measurement baseline
        self.raw_peak = None              # highest RAW price since tap
        self.hi = None                    # rolling high since tap
        self.low = None                   # lowest price during the current dip
        self.peak = None                  # highest smoothed price since tap
        self.tap_px = None                # first price we saw (your greenlight reference)
        # Why the last confirmed reclaim was refused. None = nothing refusing.
        # Published to the terminal so "waiting for entry" can say what for.
        self.hold_reason = None
        # When you tapped greenlight. record() writes this so the peak
        # refresher knows which candles belong to your session; without it
        # record() raised and the whole journal row was lost.
        self.tap_ts = time.time()

        self.state = "WAIT"
        self.entry = None                 # fill price
        self.ppeak = None                 # peak since entry (the trailing stop rides this)
        self.tp_done = 0                  # how many ladder rungs have fired

    # ── helpers ─────────────────────────────────────────────────────────────
    def _smooth(self):
        """The price decisions run on.

        At smoothing = 1 this is simply the latest price, so a take-profit or a
        stop fires on the tick that crosses it. Above 1 it is the median of that
        many polls: one bad print cannot move a median, but the trigger arrives
        later, and a spike shorter than half the window never registers at all.
        """
        if self.smoothing <= 1 or len(self.win) < 3:
            return self.win[-1]
        return statistics.median(self.win)

    def blocked_reason(self):
        """Why we'd refuse to watch this coin at all. None = worth watching.

        Buy/sell balance is deliberately NOT checked here. It's a trailing
        5-minute ratio that flips constantly, so one bad reading at the moment
        you greenlight says almost nothing about the moment we'd actually buy —
        and rejecting the whole session on it means never looking again. It's
        checked at the buy instead, against the current reading.
        """
        return None

    def bs_ok(self):
        """Is buy/sell flow healthy right now? Checked at the moment of entry."""
        if not C.USE_VOLR or self.volr is None:
            return True
        return self.volr >= self.volr_min

    # ── the loop ────────────────────────────────────────────────────────────
    def feed(self, price):
        """One price, once per second. Returns an action or None.

        Actions: ("BUY",) ("SELL", fraction, label) ("SELLALL", reason)
        """
        if price is None or price <= 0:
            return None

        # Is this price wildly off the recent median? Noted for the PEAK
        # MEASUREMENT below — deliberately not used to drop the price.
        #
        # Trading decisions run on _smooth(), which is the latest price unless
        # smoothing is turned up, in which case it is a median of that many,
        # and a single bad print cannot move a median; it takes 3 of 5. That
        # already does what a phantom-print guard is for. Gating the feed on
        # this as well was not a second layer of safety but a failure mode:
        # a discarded price was never recorded, so the median could not follow
        # a real collapse, every later tick was rejected against the same stale
        # median, and the stop-loss was never evaluated again. 1TOADCOIN
        # reached -96% with a -50% stop armed and blind.
        outlier = False
        if len(self.prices) >= 10:
            med = statistics.median(list(self.prices)[-15:])
            outlier = med > 0 and (price > 4 * med or price < med / 4)

        self.prices.append(price)
        self.win.append(price)
        self.n += 1
        sp = self._smooth()

        # Measurement uses the RAW price. Smoothing exists so a one-second wick
        # can't trigger a trade — but it also erases the spike entirely, and a
        # coin that really ran 3x would report 1.4x. Trade on smoothed, measure
        # on real.
        if self.raw_tap is None:
            self.raw_tap = self.raw_peak = price
        # Skip outliers here and only here. raw_peak is raw so that a genuine
        # spike is not flattened by smoothing — which is exactly why one
        # phantom high would overstate peak-after-tap for the whole session.
        if not outlier:
            self.raw_peak = max(self.raw_peak, price)

        if self.hi is None:
            self.hi = self.peak = self.tap_px = sp
        self.hi = max(self.hi, sp)
        self.peak = max(self.peak, sp)

        if self.state == "WAIT":
            return self._wait(price, sp)
        if self.state == "POS":
            return self._pos(sp)
        return None

    def _wait(self, price, sp):
        # 1. are we in a dip? track how low it goes.
        #
        # Deliberately does NOT return. Tracking the low and testing the bounce
        # are separate jobs, and stopping here made the second one unreachable
        # until price was back above the threshold — so the threshold, not the
        # low, was the real entry price. A 20% dip and a 45% dip both bought at
        # -20%, the second only after a 33% climb off the bottom. Falling
        # through lets a genuine reclaim fire where it happens.
        if price <= self.hi * (1 - self.dip):
            self.low = price if self.low is None else min(self.low, price)

        # 2. no dip yet, nothing to reclaim
        if self.low is None:
            # A reset reason explains why the window disappeared, so it stays.
            # Anything else is stale — it described a gate that is no longer
            # the thing standing in the way.
            if self.hold_reason not in RESET_REASONS:
                self.hold_reason = None
            return None

        # 3. dipped, but not bounced yet. Said plainly, because otherwise this
        #    stage inherits whatever the last gate happened to say.
        if price < self.low * self.bounce:
            self.hold_reason = (f"dipped — waiting for a "
                                f"{(self.bounce - 1) * 100:.0f}% bounce off the low")
            return None

        # 4. bounce confirmed — now the filters. Each records why it said no,
        #    so the terminal can show it instead of an unexplained wait.
        if self.n < self.warmup:
            self.hold_reason = f"warming up, {self.warmup - self.n}s left"
            return None
        if sp < self.peak * (1 - self.deadcat):
            self.low = None                                # falling knife — reset, wait for a new dip
            self.hold_reason = FELL_THROUGH
            return None
        if price >= self.peak * self.pico:
            self.low = None                                # too close to the top — wait for a lower entry
            self.hold_reason = RAN_AWAY
            return None
        if not self.bs_ok():
            # More is being sold than bought right this minute. Don't buy into
            # that — but keep the dip on record and keep watching, because this
            # flips back and the setup may still be here.
            self.hold_reason = (f"more selling than buying "
                                f"(B/S {self.volr:.2f}, floor {self.volr_min:.2f})")
            return None

        self.hold_reason = None
        return ("BUY",)

    def _pos(self, sp):
        # Tracked either way: the journal reports the peak, and a strategy that
        # switches to trailing mid-life would otherwise start from nothing.
        self.ppeak = max(self.ppeak, sp)

        # Stop first — protecting capital beats squeezing the last rung.
        # A trailing stop measures from the high, a plain one from the entry.
        ref = self.ppeak if self.trail_stop else self.entry
        floor = ref * (1 - self.kill)
        if self.be_after_tp1 and self.tp_done:
            # max, never a straight assignment: a coin that ran to 3x already
            # has a trailing stop at 1.5x, and dropping that to the entry to
            # honour a "break even" setting would be moving a stop DOWN. The
            # floor only ever ratchets up.
            floor = max(floor, self.entry)
        if sp < floor:
            reason = "trailing kill" if self.trail_stop else "kill"
            if self.be_after_tp1 and self.tp_done and floor == self.entry:
                reason = "break-even stop"
            return ("SELLALL", reason)

        # ladder: 70% at 1.5x, 30% at 2x
        if self.tp_done < len(self.tps) and sp >= self.entry * self.tps[self.tp_done]:
            i = self.tp_done
            self.tp_done += 1
            return ("SELL", self.fracs[i], f"TP{self.tps[i]}x")
        return None

    # ── state transitions (called by the bot once a fill is confirmed) ──────
    def on_filled(self, price):
        self.entry = price
        self.ppeak = price
        self.state = "POS"

    def on_closed(self):
        self.state = "DONE"

    # ── reporting ───────────────────────────────────────────────────────────
    def peak_since_tap(self):
        """How far it actually ran after you tapped — your selection, measured
        independently of whether the strategy captured it.

        Raw prices, not smoothed: this is a measurement, not a trade trigger.
        """
        if not self.raw_tap or not self.raw_peak:
            return None
        return self.raw_peak / self.raw_tap
