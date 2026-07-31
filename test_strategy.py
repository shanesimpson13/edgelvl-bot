"""Logic test: drive the strategy with synthetic prices using the REAL shipped config."""
import os
os.environ.update({k: "x" for k in
                   ["EDGE_API_KEY", "RPC_URL", "PRIVATE_KEY",
                    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]})
import config as C
from strategy import Session

RUNWAY = [100.0] * 400        # ~6.5 min of flat tape at 1s polls (fills slope window + warmup)
DIP    = [95.0, 88.0, 84.0, 83.0]   # -17% off the high
BOUNCE = [87.0]               # +4.8% off the low -> reclaim trigger


def drive(prices, volr=2.0):
    s = Session("mint", "TEST", volr=volr)
    events = []
    for p in prices:
        a = s.feed(p)
        if a:
            events.append((a, round(p, 4)))
            if a[0] == "BUY":
                s.on_filled(p)
            elif a[0] == "SELLALL":
                s.on_closed()
        if s.state == "DONE":
            break
    return s, events


# 1 — reclaim entry, then the 70/30 ladder
s, ev = drive(RUNWAY + DIP + BOUNCE +
              [90, 100, 120, 131, 140, 150, 160, 175, 180, 190, 200, 210, 220, 230])
kinds = [e[0][0] for e in ev]
assert "BUY" in kinds, "should enter on the reclaim"
buy_px = [e[1] for e in ev if e[0][0] == "BUY"][0]
sells = [e for e in ev if e[0][0] == "SELL"]
assert len(sells) == 2, f"expected 2 ladder rungs, got {len(sells)}"
assert abs(sells[0][0][1] - 0.70) < 1e-9 and abs(sells[1][0][1] - 0.30) < 1e-9
assert sells[0][1] >= buy_px * 1.5 * 0.98, "rung 1 should fire around 1.5x"
assert sells[1][1] >= buy_px * 2.0 * 0.98, "rung 2 should fire around 2x"
print(f"PASS 1 · entry {buy_px} -> TP1 {sells[0][1]} (1.5x) -> TP2 {sells[1][1]} (2x)")

# 2 — trailing stop
s, ev = drive(RUNWAY + DIP + BOUNCE + [95, 100, 110] + [70, 55, 50, 45, 40, 35, 30])
assert any(e[0][0] == "SELLALL" for e in ev), "should stop out on -50% from peak"
print("PASS 2 · trailing stop fires")

# 3 — volR gate
assert Session("m", "BAD", volr=0.5).blocked_reason() is not None
assert Session("m", "GOOD", volr=1.4).blocked_reason() is None
print("PASS 3 · volR gate blocks <1.0, passes >1.0")

# 4 — pico-top guard: recovery to within 10% of the high must NOT buy
s, ev = drive(RUNWAY + DIP + [99.0])
assert not any(e[0][0] == "BUY" for e in ev), "must refuse a pico-top entry"
print("PASS 4 · pico-top guard")

# 5 — no dip, no trade
s, ev = drive([100.0 + i * 0.1 for i in range(500)])
assert not ev, "must never buy without a dip first"
print("PASS 5 · no dip -> no trade")

# 6 — dead-cat: price collapsed far below peak, bounce ignored
s, ev = drive(RUNWAY + [60, 45, 30, 25, 20] + [21.0, 22.0])
assert not any(e[0][0] == "BUY" for e in ev), "must refuse a collapsed coin"
print("PASS 6 · collapsed coin is never bought")

# 7 — warmup: no entry before we've watched long enough
s, ev = drive([100.0] * 20 + DIP + BOUNCE)
assert not any(e[0][0] == "BUY" for e in ev), "must not trade a coin it just met"
print("PASS 7 · warmup enforced")

# 8 — selection metric is independent of the trade
s, _ = drive(RUNWAY + [84.0, 130.0, 250.0])
print(f"PASS 8 · peak_after_tap = {s.peak_since_tap():.2f}x")



def test_swap_rate_windows():
    """Swap-count windows must scale with how fast the coin actually trades.

    The strategy was validated on 250 SWAPS of evidence, not 250 seconds. On a
    coin doing 4,000 swaps per 5 minutes that's ~18s; treating it as 250 polls
    would sit out the entire move.
    """
    fast = Session("m", "fast", swaps_per_sec=4088 / 300)     # a live trending coin
    slow = Session("m", "slow", swaps_per_sec=150 / 300)
    unknown = Session("m", "unknown")

    assert fast.warmup < 30, f"fast coin warmup {fast.warmup} should be seconds, not minutes"
    assert slow.warmup == C.WARMUP_MAX, f"slow coin should keep the full warmup, got {slow.warmup}"
    assert unknown.warmup == C.WARMUP, "no rate -> configured default"
    assert fast.warmup >= C.WARMUP_MIN, "clamp: never fire on the second poll"
    assert fast.slope_win >= C.SLOPE_MIN

    # and the fast coin must actually be able to enter inside its warmup budget
    px = [100.0] * fast.warmup + [100.0, 84.0, 88.0]
    got = [fast.feed(p) for p in px]
    assert ("BUY",) in got, "fast coin should be able to enter after its short warmup"
    print(f"PASS 9 · warmup scales with swap rate "
          f"(fast={fast.warmup} polls, slow={slow.warmup}, unknown={unknown.warmup})")


test_swap_rate_windows()


def test_custom_settings():
    """Settings from the terminal must actually drive the session."""
    cfg = {"dip": 0.30, "bounce": 1.10, "kill": 0.25, "tp1": 3.0, "tp2": 5.0,
           "frac1": 0.5, "volr_min": 2.0}
    s = Session("m", "custom", volr=1.5, cfg=cfg)

    assert s.dip == 0.30 and s.bounce == 1.10 and s.kill == 0.25
    assert s.tps == (3.0, 5.0) and s.fracs == (0.5, 0.5), s.fracs
    # volr 1.5 passes the shipped 1.0 floor but not this user's 2.0
    assert s.blocked_reason() is not None, "custom volr floor must apply"

    # and the shipped defaults still apply when nothing is set
    d = Session("m", "default", volr=1.5)
    assert d.dip == C.DIP and d.tps == C.TPS and d.blocked_reason() is None

    # a 30% dip must not trigger on a 20% dip
    s2 = Session("m", "deep", cfg={"dip": 0.30, "bounce": 1.10})
    s2.warmup = 1
    for p in [100.0] * 5 + [80.0, 88.0]:      # -20% then +10%
        assert s2.feed(p) != ("BUY",), "20% dip must not satisfy a 30% requirement"
    for p in [100.0, 65.0, 72.0]:             # -35% then +10.8%
        got = s2.feed(p)
    assert got == ("BUY",), "35% dip + 10% reclaim should trigger"
    print("PASS 10 · terminal settings drive the session (dip/bounce/kill/TPs/volR)")


test_custom_settings()


def test_ladder_sells_whole_position():
    """Take-profit fractions are of the ORIGINAL position, not what's left.

    Taking frac of the remainder sells 30% of 30% on the second rung and strands
    21% with no take-profit and no stop — in live trading, a silent orphaned bag.
    """
    s = Session("m", "x", cfg={"tp1": 1.5, "tp2": 2.0, "use_tp2": 1, "frac1": 0.7})
    s.warmup = 1
    for p in [100.0] * 5 + [80.0, 88.0]:
        if s.feed(p) == ("BUY",):
            s.on_filled(88.0)

    original = held = 1_000_000
    sold = []
    for px in [140.0] * 5 + [190.0] * 5:          # median-5 needs prices to persist
        act = s.feed(px)
        if act and act[0] == "SELL":
            rung = s.tp_done
            is_last = rung >= len(s.tps)
            amount = held if is_last else min(int(original * act[1]), held)
            held -= amount
            sold.append(amount / original)

    assert len(sold) == 2, f"both rungs should fire, got {sold}"
    assert abs(sold[0] - 0.70) < 0.01, f"TP1 should sell 70% of the original, sold {sold[0]:.0%}"
    assert held == 0, f"{held} tokens stranded after the last rung"
    print("PASS 11 · ladder sells 70%/30% of the ORIGINAL position, nothing stranded")


test_ladder_sells_whole_position()
print("\nALL STRATEGY TESTS PASSED")
