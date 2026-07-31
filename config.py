"""
config.py — every knob in one place.

These are DEFAULTS, not gospel. Modules 05-07 walk you through measuring your
own results and tuning them. Read the notes: each number has a reason.
"""
import os

# ── your credentials ────────────────────────────────────────────────────────
EDGE_API_KEY   = os.environ["EDGE_API_KEY"]          # from edgelvl.app/welcome
EDGE_API       = "https://api.scgalpha.com"
TG_TOKEN       = os.environ["TELEGRAM_BOT_TOKEN"]    # your bot, from @BotFather
TG_CHAT        = os.environ["TELEGRAM_CHAT_ID"]      # your user id, from @userinfobot

# Only needed when you go LIVE (Module 08). Dry run reads prices from Jupiter
# and never touches an RPC or your wallet, so leave these empty until then.
RPC_URL        = os.environ.get("RPC_URL", "https://api.mainnet-beta.solana.com")
PRIVATE_KEY    = os.environ.get("PRIVATE_KEY", "")   # dedicated trading wallet, base58
JUP_API_KEY    = os.environ.get("JUP_API_KEY", "")   # free from portal.jup.ag (live only)

# ── where state lives ───────────────────────────────────────────────────────
# Open positions are written here after every change so a crash or restart
# can't lose track of a bag you're holding. Don't delete this while trading.
STATE_FILE     = os.environ.get("STATE_FILE", "state/positions.json")

# ── money ───────────────────────────────────────────────────────────────────
DRY_RUN        = os.environ.get("DRY_RUN", "1") == "1"   # 1 = simulate, 0 = REAL MONEY
SIZE_SOL       = float(os.environ.get("SIZE_SOL", "0.05"))  # per trade

# ── the feed ────────────────────────────────────────────────────────────────
FEED_POLL_SEC  = 10      # how often to check for new signals (10s = you see an alert
                         # within ~5s of it firing; polling faster gains nothing)
POLL_SEC       = 1.0     # price poll interval. 1s is the sweet spot: fast enough to
                         # track the move, slow enough to ignore 1-second wick fakes.
PROBE_SOL      = 0.01    # quote size used to measure price (consistent yardstick)

# ── entry: the reclaim ──────────────────────────────────────────────────────
# Wait for a dip off the high, then buy the bounce. Never buy the vertical top.
DIP            = 0.15    # need a -15% dip off the rolling high before we look to buy
BOUNCE         = 1.04    # then +4% up off that dip low = the reclaim trigger
DEADCAT        = 0.50    # skip if price is >50% below the running peak (falling knife)
PICO           = 0.90    # skip if the bounce is already within 10% of the high
                         #   (that's a pico-top entry — wait for a lower one)
SLOPE_WIN      = 300     # slope window in POLLS. At 1s polling = a 5-minute window.
SLOPE_TOL      = 0.02    # allow 2% droop before calling the trend "rolling over"
WARMUP         = 250     # polls to observe before ANY entry (~4 min). Stops you buying
                         #   into a coin you've only seen 3 seconds of.

# ── volR gate (needs the signal feed) ───────────────────────────────────────
# Early buy-volume vs sell-volume. Our data: coins alerting with volR < 1.0 went
# 0-for-13. It's the single best "don't touch this" filter we have.
# It can't be computed from price alone, so it arrives with the signal.
USE_VOLR       = True
VOLR_MIN       = 1.0     # skip the coin entirely if volR is below this

# ── exit: the ladder ────────────────────────────────────────────────────────
# Sell 70% at 1.5x, the rest at 2x. Fully out at 2x — no runner, no bag-holding.
TPS            = (1.5, 2.0)
FRACS          = (0.70, 0.30)
# Want a moonbag? Add a rung and keep a slice for the tail:
#   TPS   = (1.5, 2.0, 5.0)
#   FRACS = (0.50, 0.30, 0.20)   <- last 20% rides to 5x (or the stop takes it)
# Fractions are of the ORIGINAL position and should total 1.0.

# Stop: -50% from the position's PEAK (trailing), not from entry.
# Why so loose? Tighter stops shook us out of coins that went on to run. A -25%
# stop looks safer and bled more. See Module 08.
KILL           = 0.50

# ── time limits ─────────────────────────────────────────────────────────────
ENTRY_TIMEOUT  = 3600    # give up waiting for an entry after 60 min
MAX_HOLD       = 21600   # force-exit after 6h. These coins peak ~11 min in and
                         #   bleed for hours; there's nothing to wait for.

# ── execution ───────────────────────────────────────────────────────────────
SLIPPAGE_BPS   = 1500    # 1500 = 15%. Memecoins move; a tight cap just means
                         #   your exit fails and you hold the bag.
PRIORITY_LAMPS = 2_000_000   # priority fee ceiling (~0.002 SOL)
GAS_SOL        = 0.0005  # per transaction: network fee + typical priority tip.
                         # Dry run subtracts this too, so its numbers match live.
                         # NOTE: gas is a FIXED cost, so it hurts small trades most.
                         # A full trade is 3 transactions (buy + 2 take-profits), so
                         # at 0.05 SOL that's ~3% of your position gone to fees before
                         # the price does anything. Raising SIZE_SOL dilutes this drag.
