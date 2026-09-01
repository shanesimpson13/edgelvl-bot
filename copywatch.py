"""
copywatch.py — follow a bundler's launches and greenlight the ones that migrate.

WHY THIS SHAPE

The operators we follow launch a coin, bundle-buy it in the creation block, and
walk away. Most of those coins never complete the bonding curve: measured over a
random 110 of one operator's last 30 days, 26.4% migrated and the rest died on
the curve with ~$6k of liquidity and no pool at all. A coin with no pool cannot
be bought or sold, so following the BUY would fire on four duds for every real
signal.

So the buy is not the trigger. The MIGRATION is. That single choice throws away
about three quarters of the operator's output before it reaches the bot, and
everything it throws away was untradeable. It also buys us a wait: on the spray
operator, migration lands a median 1.6 minutes after the bundle buy (p90 4.8
min, all inside 30), and a coin only gets there because real buyers completed
the curve. We are letting the crowd's money run our filter.

Where the entry lands is then the bot's business, not ours. We hand the mint to
arm_mint() exactly as a terminal greenlight does, and the dip-and-reclaim wait
puts the fill where it should be. That matters more than it sounds: reading
1,500 real wallets' fills across two of these coins, buying in the creation
block was NEGATIVE (45-50% win) and buying after ten minutes was ruinous (8-11%
win). The profitable band was roughly 30s to 5min in, which is where waiting for
a dip and a reclaim naturally puts you. The delay is the edge, not a cost.

WHAT THIS FILE DELIBERATELY DOES NOT DO

It does not size, price, enter or exit. It decides one thing — is this coin
worth handing to the bot — and hands it over. Every rail below is a refusal, not
a strategy.
"""
import asyncio
import json
import os
import time

import aiohttp

import config as C

WSOL = "So11111111111111111111111111111111111111112"
HELIUS = "https://api.helius.xyz/v0"

# Mints we have already acted on, ever. Persisted, because a restart that
# re-armed everything it had already traded would double up on open positions.
_seen_path = os.path.join(os.path.dirname(C.STATE_FILE) or "state", "copy_seen.json")
_seen = set()
# The follow list, refreshed from the terminal every cycle.
_rules = []
# mint -> {ts, rule, wallet}. Candidates waiting to migrate. Each remembers
# WHICH row found it, because size, strategy and daily cap are per wallet.
_candidates = {}
# Armed today, per rule. (day, {rule_id: count})
_armed_today = [None, {}]


def _load_seen():
    global _seen
    try:
        with open(_seen_path) as f:
            _seen = set(json.load(f))
    except Exception:
        _seen = set()


def _save_seen():
    try:
        os.makedirs(os.path.dirname(_seen_path), exist_ok=True)
        # Bounded: this is a dedupe guard, not a history. Keeping every mint we
        # ever saw would grow without limit for no benefit.
        with open(_seen_path, "w") as f:
            json.dump(sorted(_seen)[-4000:], f)
    except Exception:
        pass


async def _fetch_rules(s):
    """The follow list, as set in the terminal.

    The source of truth moved out of env vars: those are read once at import,
    so changing who you follow meant an ssh session and a restart. Refetched
    each cycle instead, which also means switching a wallet off in the UI stops
    it within one poll rather than at the next deploy.

    Returns None on failure, and the caller keeps the list it already had —
    going blind because the API blinked is worse than acting on a stale list.
    """
    try:
        hdrs = {"Authorization": f"Bearer {C.BOT_ADMIN_KEY}"}
        async with s.get(f"{C.EDGE_API}/api/copy", headers=hdrs,
                         timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                return None
            return await r.json()
    except Exception:
        return None


async def _push_status(s):
    """What the watcher is doing, for the terminal to show.

    Best-effort and swallowed: a status that fails to send must never stop a
    coin being armed.
    """
    try:
        watching = [{"mint": m, "since": v["ts"], "wallet": v.get("wallet", "")}
                    for m, v in _candidates.items()]
        hdrs = {"Authorization": f"Bearer {C.BOT_ADMIN_KEY}"}
        await s.post(f"{C.EDGE_API}/api/copy/status", headers=hdrs,
                     json={"watching": watching, "armed_today": _armed_today[1]},
                     timeout=aiohttp.ClientTimeout(total=10))
    except Exception:
        pass


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def _budget_left(rule):
    """Trades still allowed today FOR THIS WALLET.

    Per rule rather than per box: following two wallets at different sizes
    should not mean one can spend the other's budget. The cap is a circuit
    breaker, not a target.
    """
    if _armed_today[0] != _today():
        _armed_today[0] = _today()
        _armed_today[1] = {}
    used = _armed_today[1].get(rule["id"], 0)
    return int(rule.get("max_per_day", 8)) - used


async def _get(s, url, tries=3):
    for a in range(tries):
        try:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status == 200:
                    return await r.json()
        except Exception:
            pass
        await asyncio.sleep(1 + 2 * a)
    return None


async def _bundle_buys(s, wallet):
    """Mints this wallet has just bundle-bought on the pump.fun curve.

    A bundle buy is a pump.fun swap where the wallet SPENDS real size. The size
    floor is what separates a launch bundle from the dust: undersized buys can't
    complete the curve, so their coins never migrate and never signal anyway —
    the floor just saves us tracking them for half an hour first.
    """
    d = await _get(s, f"{HELIUS}/addresses/{wallet}/transactions"
                      f"?api-key={C.HELIUS_API_KEY}&limit=25")
    out = []
    for t in d or []:
        if t.get("transactionError") or t.get("type") != "SWAP":
            continue
        if t.get("source") != "PUMP_FUN":
            continue
        # What the wallet actually paid, native plus any wrapped SOL leg.
        spent = 0.0
        for ad in t.get("accountData", []):
            if ad.get("account") == wallet:
                spent += ad.get("nativeBalanceChange", 0) / 1e9
            for tb in (ad.get("tokenBalanceChanges") or []):
                if tb.get("userAccount") == wallet and tb.get("mint") == WSOL:
                    ra = tb["rawTokenAmount"]
                    spent += int(ra["tokenAmount"]) / (10 ** int(ra["decimals"]))
        if -spent < C.COPY_MIN_BUNDLE_SOL:
            continue
        for x in t.get("tokenTransfers", []):
            m = x.get("mint")
            if m and m != WSOL:
                out.append((m, t.get("timestamp", int(time.time()))))
                break
    return out


async def _has_pool(s, mint):
    """True once the coin has an AMM pool — i.e. it actually migrated.

    Checked on-chain rather than from a launchpad progress field because the
    pool is the thing that matters: it is what makes the coin buyable and
    sellable. No pool, no trade, whatever any status flag says.
    """
    d = await _get(s, f"{HELIUS}/addresses/{mint}/transactions"
                      f"?api-key={C.HELIUS_API_KEY}&limit=30")
    for t in d or []:
        if t.get("transactionError"):
            continue
        if t.get("source") in ("PUMP_AMM", "METEORA_DAMM_V2", "RAYDIUM"):
            return True
    return False


async def run(s, arm, live_count):
    """Watch the followed wallets.

    `arm` is bot.arm_mint and `live_count` returns how many sessions are open
    right now. Both are passed in so this module never imports the bot and can
    be tested on its own.
    """
    global _rules
    if not C.HELIUS_API_KEY:
        print("copywatch: no HELIUS_API_KEY — not watching", flush=True)
        return

    _load_seen()
    mode = "LIVE" if not C.DRY_RUN else "DRY"
    # It starts regardless of whether anything is switched on. The follow list
    # comes from the terminal now, so a watcher that refused to start on an
    # empty list could never be turned on without a restart.
    print(f"copywatch {mode}: reading the follow list from the terminal",
          flush=True)
    max_concurrent = C.COPY_MAX_CONCURRENT

    while True:
        try:
            now = time.time()

            d = await _fetch_rules(s)
            if d is not None:
                _rules = [r for r in (d.get("rules") or []) if r.get("enabled")
                          and r.get("wallet")]
                max_concurrent = int(d.get("max_concurrent") or C.COPY_MAX_CONCURRENT)
            if not _rules:
                await _push_status(s)
                await asyncio.sleep(C.COPY_POLL_SEC)
                continue

            # 1 — new bundle buys become candidates, tagged with the row that
            #     found them so the arm uses that row's size and strategy.
            for rule in _rules:
                w = rule["wallet"]
                for mint, ts in await _bundle_buys(s, w):
                    if mint in _seen or mint in _candidates:
                        continue
                    # Ignore anything that was already old when we found it. A
                    # coin whose migration window closed while the bot was down
                    # is not a signal, it is history — and arming it would buy
                    # the part of the curve that loses money.
                    if now - ts > C.COPY_MAX_SIGNAL_AGE:
                        _seen.add(mint)
                        continue
                    _candidates[mint] = {"ts": ts, "rule": rule["id"], "wallet": w}
                    print(f"copywatch: {mint[:12]}… bundled by {w[:8]}…, "
                          f"waiting for migration", flush=True)

            # 2 — candidates that migrated get armed; the rest time out.
            for mint, cand in list(_candidates.items()):
                ts = cand["ts"]
                rule = next((r for r in _rules if r["id"] == cand["rule"]), None)
                if rule is None:
                    # The row was switched off or deleted while this waited.
                    # Dropping it is the point: turning a wallet off should
                    # stop it arming, not just stop it finding new coins.
                    del _candidates[mint]
                    print(f"copywatch: {mint[:12]}… dropped, its wallet is no "
                          f"longer followed", flush=True)
                    continue
                if now - ts > C.COPY_MIGRATION_TIMEOUT:
                    del _candidates[mint]
                    _seen.add(mint)
                    # Deliberately not "never migrated": it may well have, late.
                    # What we know is that the window we trade closed.
                    print(f"copywatch: {mint[:12]}… window closed after "
                          f"{int(now - ts)}s, dropped", flush=True)
                    continue
                if not await _has_pool(s, mint):
                    continue

                del _candidates[mint]
                _seen.add(mint)
                _save_seen()

                # Checked AGAIN here, not just at ingest. Migration lands a
                # median 1.6 min after the bundle and p90 under 5 — but a
                # candidate can sit for half an hour and then graduate, and
                # entering that late is the losing side of this trade: of 1,500
                # real wallets across two of these coins, those buying more
                # than ten minutes in won 8-11% of the time.
                age = now - ts
                if age > C.COPY_MAX_ARM_AGE:
                    print(f"copywatch: {mint[:12]}… migrated but {int(age)}s "
                          f"after the bundle — too late, skipped", flush=True)
                    continue

                # Rails. Each one is a reason to refuse, checked out loud so a
                # quiet day is distinguishable from a broken watcher.
                if _budget_left(rule) <= 0:
                    print(f"copywatch: {mint[:12]}… migrated but {rule['wallet'][:8]}…"
                          f" has spent its daily cap ({rule.get('max_per_day')})",
                          flush=True)
                    continue
                open_now = live_count()
                if open_now >= max_concurrent:
                    print(f"copywatch: {mint[:12]}… migrated but {open_now} "
                          f"already open (max {max_concurrent})", flush=True)
                    continue

                # Belt and braces on size. COPY_SIZE_SOL is the knob you tune;
                # COPY_MAX_SIZE_SOL is the one that stops a typo becoming a
                # position. A live bot should not be able to bet more than you
                # decided when you were calm.
                size = min(float(rule.get("size_sol") or C.COPY_SIZE_SOL),
                           C.COPY_MAX_SIZE_SOL)
                preset = rule.get("preset") or None
                print(f"copywatch: {mint[:12]}… MIGRATED → arming {size} SOL"
                      f"{' on ' + preset if preset else ''}", flush=True)
                try:
                    ok = await arm(s, mint, opts={"size_sol": size, "preset": preset})
                except Exception as e:
                    print(f"copywatch: arm error {mint[:8]}: {e}", flush=True)
                    ok = False
                if ok:
                    _budget_left(rule)          # rolls the day over if needed
                    _armed_today[1][rule["id"]] = _armed_today[1].get(rule["id"], 0) + 1

            await _push_status(s)
            await asyncio.sleep(C.COPY_POLL_SEC)
        except Exception as e:
            # One bad cycle must never take the watcher down: a dead task looks
            # exactly like a quiet operator.
            print(f"copywatch loop error: {e}", flush=True)
            await asyncio.sleep(C.COPY_POLL_SEC)
