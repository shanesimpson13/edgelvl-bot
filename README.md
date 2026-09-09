# EdgeLvl Bot

The semi-automated trading system from the [EdgeLvl](https://edgelvl.app) playbook, on Solana and Robinhood Chain.

**Signals arrive in Telegram → you tap 🟢 GREENLIGHT → the bot handles entry timing and exits.**
Nothing trades without your tap.

## Files

| File | What it does |
|---|---|
| `config.py` | every threshold you can tune, each with a note on *why* |
| `strategy.py` | the decision engine — pure logic, no network, fully testable |
| `jupiter.py` | price polling (1s) + position valuation via Jupiter Lite |
| `ultra.py` | live execution via Jupiter Ultra (inline on-chain confirmation + retries) |
| `state.py` | position persistence + wallet reconciliation — survives a crash |
| `rh.py` | the same executor for Robinhood Chain, swapping through LI.FI |
| `copywatch.py` | follows chosen wallets and greenlights what they buy |
| `bot.py` | Telegram greenlight + the main loop |
| `test_strategy.py` | logic tests: `python3 test_strategy.py` |

## The strategy

- **Entry (reclaim):** wait for a −15% dip off the rolling high, then buy the +4% recovery.
  Never buys the vertical top. Gated by warmup, trend slope, dead-cat and pico-top filters.
- **Exit (ladder):** sell 70% at 1.5x, 30% at 2x. Trailing stop at −50% off the position peak.
- Every threshold lives in `config.py`. Moonbag rungs supported.

## Two chains, one strategy

A coin's address decides which executor runs it: `0x…` is Robinhood Chain and
goes through LI.FI, anything else is Solana and goes through Jupiter. There is
no chain setting to get wrong. The strategy, the settings and the journal are
identical on both — only the venue and the currency differ.

Robinhood books are thinner and the differences are real, so the bot handles
them explicitly: it refuses to arm a coin whose *exit* does not route (about
one in twelve quotes a buy and no sell at all), and it retries a buy that is
too large at the size that actually fits. Sizes are written in SOL and
converted by dollar value, so one setting means the same order on both chains.

**Status:** the Robinhood path is built and its signing is verified end to end,
but no swap has been broadcast on chain yet. Treat it as untested with real
money.

## Dry run is a real simulation

`DRY_RUN=1` prices every trade with a **real Jupiter quote at your real size** (fees, price
impact and routing included), fills **one poll after** the trigger so the price has moved like
it does in reality, and subtracts gas. The only thing it skips is broadcasting the transaction.

A round trip on a flat price comes out **negative** — because that's what a flat round trip
actually costs. A simulator that reports zero there is lying to you.

## Quick start

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # add your API key + Telegram details
source .env && python bot.py
```

Dry run needs **no wallet and no RPC** — just your API key and a Telegram bot.
Those are only required when you go live.

## Selection vs strategy

Every finished coin logs `peak_after_tap` to `trades.jsonl` — how far it ran **after your
greenlight**, independent of whether the strategy captured it. That separates *your picking*
from *the bot's execution*, which is the only way to know which one needs work.

---

Not financial advice. Memecoins are high-risk; most participants lose money.
