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
from collections import deque

import config as C


class Session:
    """One coin, one greenlight, from tap to flat."""

    def __init__(self, mint, name, volr=None):
        self.mint, self.name = mint, name
        self.volr = volr                  # from the signal feed; None if unknown

        self.prices = deque(maxlen=C.SLOPE_WIN)
        self.win = deque(maxlen=5)        # median-5 smoothing: ignores single-poll noise
        self.n = 0                        # polls seen

        self.hi = None                    # rolling high since tap
        self.low = None                   # lowest price during the current dip
        self.peak = None                  # highest smoothed price since tap
        self.tap_px = None                # first price we saw (your greenlight reference)

        self.state = "WAIT"
        self.entry = None                 # fill price
        self.ppeak = None                 # peak since entry (the trailing stop rides this)
        self.tp_done = 0                  # how many ladder rungs have fired

    # ── helpers ─────────────────────────────────────────────────────────────
    def _smooth(self):
        """Median of the last 5 polls. One bad print can't move this."""
        return statistics.median(self.win) if len(self.win) >= 3 else self.win[-1]

    def _trend_ok(self):
        """Is the recent half of the window still >= the older half? (not rolling over)"""
        if len(self.prices) < C.SLOPE_WIN:
            return True                   # not enough history yet — don't block on it
        seg = list(self.prices)
        h = C.SLOPE_WIN // 2
        older = sum(seg[:h]) / h
        recent = sum(seg[h:]) / h
        return recent >= older * (1 - C.SLOPE_TOL)

    def blocked_reason(self):
        """Why we'd refuse to trade this coin at all. None = it's tradeable."""
        if C.USE_VOLR and self.volr is not None and self.volr < C.VOLR_MIN:
            return f"volR {self.volr:.2f} < {C.VOLR_MIN} — early sell pressure (0/13 historically)"
        return None

    # ── the loop ────────────────────────────────────────────────────────────
    def feed(self, price):
        """One price, once per second. Returns an action or None.

        Actions: ("BUY",) ("SELL", fraction, label) ("SELLALL", reason)
        """
        if price is None or price <= 0:
            return None

        # phantom-print guard: ignore anything 4x off the recent median.
        # (a bad quote should never trigger a trade)
        if len(self.prices) >= 10:
            med = statistics.median(list(self.prices)[-15:])
            if med > 0 and (price > 4 * med or price < med / 4):
                return None

        self.prices.append(price)
        self.win.append(price)
        self.n += 1
        sp = self._smooth()

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
        if price <= self.hi * (1 - C.DIP):
            self.low = price if self.low is None else min(self.low, price)
            return None

        # 2. no dip yet, nothing to reclaim
        if self.low is None:
            return None

        # 3. has it bounced off that low?
        if price < self.low * C.BOUNCE:
            return None

        # 4. bounce confirmed — now the filters
        if self.n < C.WARMUP:
            return None                                    # haven't watched it long enough
        if not self._trend_ok():
            return None                                    # trend rolling over, skip
        if sp < self.peak * (1 - C.DEADCAT):
            self.low = None                                # falling knife — reset, wait for a new dip
            return None
        if price >= self.peak * C.PICO:
            self.low = None                                # too close to the top — wait for a lower entry
            return None

        return ("BUY",)

    def _pos(self, sp):
        self.ppeak = max(self.ppeak, sp)

        # trailing stop first — protecting capital beats squeezing the last rung
        if sp < self.ppeak * (1 - C.KILL):
            return ("SELLALL", "kill")

        # ladder: 70% at 1.5x, 30% at 2x
        if self.tp_done < len(C.TPS) and sp >= self.entry * C.TPS[self.tp_done]:
            i = self.tp_done
            self.tp_done += 1
            return ("SELL", C.FRACS[i], f"TP{C.TPS[i]}x")
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
        """How far it ran after you tapped. This measures YOUR SELECTION,
        independently of whether the strategy captured it. Module 05 lives on this."""
        if not self.tap_px or not self.peak:
            return None
        return self.peak / self.tap_px
