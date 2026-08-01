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

        # The original strategy counted SWAPS, not seconds: it needed 250 swaps of
        # evidence before entering and a 300-swap slope window. We poll once a
        # second instead of reading every swap, so those windows have to be
        # converted using how fast the coin is actually trading.
        #
        # It matters enormously. A coin doing 4,000 swaps per 5 minutes reaches
        # 250 swaps in ~18 seconds; treating "250" as 250 polls would make you
        # wait 4 minutes — long enough for the whole move on a coin this fast.
        self.warmup, self.slope_win = self._windows(swaps_per_sec)

        self.prices = deque(maxlen=self.slope_win)
        self.win = deque(maxlen=5)        # median-5 smoothing: ignores single-poll noise
        self.n = 0                        # polls seen

        self.raw_tap = None               # first RAW price — the measurement baseline
        self.raw_peak = None              # highest RAW price since tap
        self.hi = None                    # rolling high since tap
        self.low = None                   # lowest price during the current dip
        self.peak = None                  # highest smoothed price since tap
        self.tap_px = None                # first price we saw (your greenlight reference)

        self.state = "WAIT"
        self.entry = None                 # fill price
        self.ppeak = None                 # peak since entry (the trailing stop rides this)
        self.tp_done = 0                  # how many ladder rungs have fired

    # ── helpers ─────────────────────────────────────────────────────────────
    @staticmethod
    def _windows(swaps_per_sec):
        """Convert the strategy's swap-count windows into poll counts.

        Returns (warmup_polls, slope_window_polls). With no rate available we
        fall back to the configured defaults.

        Both are clamped: fast coins still get a few seconds of observation
        rather than firing on the second poll, and slow ones don't wait forever.
        """
        if not swaps_per_sec or swaps_per_sec <= 0:
            return C.WARMUP, C.SLOPE_WIN

        polls_per_swap = 1.0 / (swaps_per_sec * C.POLL_SEC)
        warmup = int(C.WARMUP_SWAPS * polls_per_swap)
        slope = int(C.SLOPE_SWAPS * polls_per_swap)
        return (max(C.WARMUP_MIN, min(C.WARMUP_MAX, warmup)),
                max(C.SLOPE_MIN, min(C.SLOPE_MAX, slope)))

    def _smooth(self):
        """Median of the last 5 polls. One bad print can't move this."""
        return statistics.median(self.win) if len(self.win) >= 3 else self.win[-1]

    def _trend_ok(self):
        """Is the recent half of the window still >= the older half? (not rolling over)"""
        if len(self.prices) < self.slope_win:
            return True                   # not enough history yet — don't block on it
        seg = list(self.prices)
        h = self.slope_win // 2
        older = sum(seg[:h]) / h
        recent = sum(seg[h:]) / h
        return recent >= older * (1 - C.SLOPE_TOL)

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

        # Measurement uses the RAW price. Smoothing exists so a one-second wick
        # can't trigger a trade — but it also erases the spike entirely, and a
        # coin that really ran 3x would report 1.4x. Trade on smoothed, measure
        # on real.
        if self.raw_tap is None:
            self.raw_tap = self.raw_peak = price
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
        if price <= self.hi * (1 - self.dip):
            self.low = price if self.low is None else min(self.low, price)
            return None

        # 2. no dip yet, nothing to reclaim
        if self.low is None:
            return None

        # 3. has it bounced off that low?
        if price < self.low * self.bounce:
            return None

        # 4. bounce confirmed — now the filters
        if self.n < self.warmup:
            return None                                    # haven't watched it long enough
        if not self._trend_ok():
            return None                                    # trend rolling over, skip
        if sp < self.peak * (1 - self.deadcat):
            self.low = None                                # falling knife — reset, wait for a new dip
            return None
        if price >= self.peak * self.pico:
            self.low = None                                # too close to the top — wait for a lower entry
            return None
        if not self.bs_ok():
            # More is being sold than bought right this minute. Don't buy into
            # that — but keep the dip on record and keep watching, because this
            # flips back and the setup may still be here.
            return None

        return ("BUY",)

    def _pos(self, sp):
        self.ppeak = max(self.ppeak, sp)

        # trailing stop first — protecting capital beats squeezing the last rung
        if sp < self.ppeak * (1 - self.kill):
            return ("SELLALL", "kill")

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
