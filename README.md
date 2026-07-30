# edgelvl-bot — the buyer-facing trading bot

Reference implementation shipped in the EdgeLvl playbook (Notion module "03 · Build the Bot").

    config.py     every tunable threshold
    jupiter.py    price (1s quote polling) + execution, both via Jupiter
    strategy.py   pure decision engine (no network) — testable/replayable
    bot.py        Telegram greenlight + main loop
    test_strategy.py   logic tests (run: python3 test_strategy.py)

Design notes:
- NO gRPC firehose. Price = Jupiter quote polling @1s (buyer-affordable, wick-immune).
- volR cannot be derived from price — it ships in the signal payload from the API.
- Entry: -15% dip off rolling high -> +4% bounce reclaim, gated by warmup/slope/deadcat/pico.
- Exit: 70% @1.5x, 30% @2x, trailing stop -50% off position peak.
- Logs peak_after_tap per trade = the SELECTION metric (independent of strategy).
