"""Logic test: drive the strategy with synthetic prices using the REAL shipped config."""
import os
os.environ.update({k: "x" for k in
                   ["EDGE_API_KEY", "RPC_URL", "PRIVATE_KEY",
                    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]})
import config as C
from strategy import Session

RUNWAY = [100.0] * 400        # plenty of flat tape at 1s polls to clear any warmup
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

# 3 — B/S never refuses a session (it gates the buy instead; see test 12)
assert Session("m", "BAD", volr=0.5).blocked_reason() is None
assert Session("m", "GOOD", volr=1.4).blocked_reason() is None
assert not Session("m", "BAD", volr=0.5).bs_ok(), "weak flow should block the BUY"
assert Session("m", "GOOD", volr=1.4).bs_ok()
print("PASS 3 · weak B/S blocks the buy, never the session")

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

# 7 — warmup: no entry before we've watched long enough.
# Built from the session's OWN warmup rather than a hard-coded 20 polls, which
# silently stopped testing anything the moment the default changed.
_w = Session("m", "probe").warmup
s, ev = drive([100.0] * max(0, _w - len(DIP) - len(BOUNCE) - 1) + DIP + BOUNCE)
assert not any(e[0][0] == "BUY" for e in ev), "must not trade a coin it just met"
s, ev = drive([100.0] * (_w + 1) + DIP + BOUNCE)
assert any(e[0][0] == "BUY" for e in ev), "must trade once the warmup is served"
print(f"PASS 7 · warmup enforced ({_w} polls) and released")

# 8 — selection metric is independent of the trade
s, _ = drive(RUNWAY + [84.0, 130.0, 250.0])
print(f"PASS 8 · peak_after_tap = {s.peak_since_tap():.2f}x")



def test_warmup_setting():
    """Warmup is a plain number of seconds you choose, clamped to 5-60.

    It used to be derived from the coin's swap rate. That number came from a
    backtest that read every swap rather than polling once a second, and the
    filter it existed to feed has been removed.
    """
    default = Session("m", "default")
    quick   = Session("m", "quick", cfg={"warmup_sec": 5})
    slow    = Session("m", "slow",  cfg={"warmup_sec": 60})
    silly   = Session("m", "silly", cfg={"warmup_sec": 9999})
    tiny    = Session("m", "tiny",  cfg={"warmup_sec": 0})

    assert default.warmup == int(C.WARMUP_SEC / C.POLL_SEC), default.warmup
    assert quick.warmup == int(C.WARMUP_MIN_SEC / C.POLL_SEC)
    assert slow.warmup == int(C.WARMUP_MAX_SEC / C.POLL_SEC)
    assert silly.warmup == slow.warmup, "must clamp, not obey"
    assert tiny.warmup == quick.warmup, "must clamp, not obey"

    # and a coin must actually be enterable the moment its warmup is over
    px = [100.0] * default.warmup + [100.0, 84.0, 88.0]
    got = [default.feed(p) for p in px]
    assert ("BUY",) in got, "should enter as soon as the warmup is served"
    print(f"PASS 9 · warmup is a setting "
          f"(default={default.warmup}s, clamped {quick.warmup}-{slow.warmup}s)")


test_warmup_setting()


def test_trend_filter_gone():
    """A deep dip that reclaims must not be refused for 'rolling over'.

    The slope filter compared the recent half of a ~31-poll window with the
    older half. A dip-and-reclaim completes well inside that, so the dip WAS
    the downslope it measured: it refused the deepest setups, which are the
    ones the entry rule exists to catch.
    """
    assert not hasattr(Session("m", "x"), "_trend_ok"), "filter should be gone"
    ramp = lambda a, b, n: [a + (b - a) * i / (n - 1) for i in range(n)]
    tape = [100.0] * 60 + ramp(100, 58, 12) + ramp(58, 70, 20)
    s, ev = drive(tape)
    buys = [e[1] for e in ev if e[0][0] == "BUY"]
    assert buys, "a 42% dip that reclaims must be buyable"
    assert buys[0] < 70, f"should buy near the low, got {buys[0]}"
    print(f"PASS 13 · deep dip buys at {buys[0]:.1f} "
          f"({(buys[0]/100 - 1) * 100:+.0f}% off the high)")


test_trend_filter_gone()


def test_break_even_after_tp1():
    """With be_after_tp1, what is left after rung 1 cannot exit below entry."""
    cfg = {"tp1": 1.5, "tp2": 4.0, "use_tp2": 1, "frac1": 0.7,
           "dip": 0.15, "bounce": 1.04, "kill": 0.5, "trail_stop": 1,
           "be_after_tp1": 1}
    s = Session("m", "BE", volr=2.0, cfg=cfg)
    for p in [100.0] * 60 + [95, 88, 84, 83]:
        s.feed(p)
    act = s.feed(87.0)
    assert act == ("BUY",), f"expected entry, got {act}"
    s.on_filled(87.0)

    # rung 1 at 1.5x
    fired = [s.feed(p) for p in (100.0, 120.0, 131.0)]
    assert any(a and a[0] == "SELL" for a in fired), "TP1 should fire at 1.5x"
    assert s.tp_done == 1

    # now walk it back down. Without the setting this would ride to 131*0.5=65.
    out = None
    for p in (120.0, 110.0, 100.0, 92.0, 88.0, 86.9, 80.0, 70.0):
        a = s.feed(p)
        if a and a[0] == "SELLALL":
            out = (a, p); break
    assert out, "the runner must exit once it loses break-even"
    assert out[1] >= 87.0 * 0.99, f"exited at {out[1]}, below entry 87.0"
    print(f"PASS 14 · break-even stop held the runner at {out[1]:.1f} "
          f"(entry 87.0, reason {out[0][1]!r})")


test_break_even_after_tp1()


def test_custom_settings():
    """Settings from the terminal must actually drive the session."""
    cfg = {"dip": 0.30, "bounce": 1.10, "kill": 0.25, "tp1": 3.0, "tp2": 5.0,
           "frac1": 0.5, "volr_min": 2.0}
    s = Session("m", "custom", volr=1.5, cfg=cfg)

    assert s.dip == 0.30 and s.bounce == 1.10 and s.kill == 0.25
    assert s.tps == (3.0, 5.0) and s.fracs == (0.5, 0.5), s.fracs
    # volr 1.5 passes the shipped 1.0 floor but not this user's 2.0 — and that
    # now defers the buy rather than refusing the session
    assert s.blocked_reason() is None
    assert not s.bs_ok(), "custom B/S floor must apply at the buy"

    # and the shipped defaults still apply when nothing is set
    d = Session("m", "default", volr=1.5)
    assert d.dip == C.DIP and d.tps == C.TPS and d.bs_ok()

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


def test_bs_checked_at_buy():
    """Buy/sell balance gates the BUY, not the session.

    It's a trailing 5-minute ratio that flips constantly. Refusing to watch a
    coin because of one weak reading at greenlight time is how CallDog got
    skipped and then ran 2x. The reading that matters is the one at the moment
    we'd actually buy.
    """
    weak = Session("m", "weak", volr=0.88)
    assert weak.blocked_reason() is None, "a weak reading must not refuse the session"
    weak.warmup = 1
    got = [weak.feed(p) for p in [100.0] * 5 + [80.0, 88.0]]
    assert ("BUY",) not in got, "must not buy while more is being sold than bought"

    # same setup, flow recovers (as the 15s refresh would update it)
    ok = Session("m", "recovers", volr=0.88)
    ok.warmup = 1
    for p in [100.0] * 5 + [80.0]:
        ok.feed(p)
    ok.volr = 1.20
    assert ok.feed(88.0) == ("BUY",), "should buy once buy-side flow returns"

    # and an unknown reading never blocks
    unknown = Session("m", "unknown")
    assert unknown.bs_ok(), "no data must not mean no trade"
    print("PASS 12 · B/S gates the buy, not the session")


test_bs_checked_at_buy()
print("\nALL STRATEGY TESTS PASSED")
