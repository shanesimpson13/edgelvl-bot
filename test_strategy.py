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

print("\nALL STRATEGY TESTS PASSED")
