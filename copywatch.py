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
import uuid

import aiohttp

import config as C
import jupiter as J

WSOL = "So11111111111111111111111111111111111111112"
# pump.fun's program, the same one the executor builds against. A launch bundle
# is a swap through this, so its presence is what makes a transaction relevant.
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
GMGN_API = "https://openapi.gmgn.ai"
# When GMGN says we are banned, when it lifts. Shared IP, shared cooldown.
_gmgn_quiet_until = [0.0]
# Signatures already examined. Without this the watcher would re-fetch every
# transaction in the window on every poll, which is how you get rate-limited off
# your own RPC.
_sig_seen = set()

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


async def _gmgn_buys(s, wallet):
    """This wallet's recent buys, as GMGN already parsed them.

    Better than reading the chain ourselves for one reason: every fact we need
    is a FIELD rather than an inference. event_type says it is a buy,
    launchpad_platform says it came from pump.fun, token.address is the mint.
    Our own parser matched a program id and computed balance deltas, which
    worked on the coin we tested and would have failed silently on a bundle
    routed any other way.

    Returns None — not [] — when GMGN cannot answer, so the caller can fall
    back rather than treat an outage as "this wallet did nothing". A watcher
    that has gone blind looks exactly like a quiet operator, which is the
    failure that takes longest to notice.
    """
    if not C.WATCH_GMGN_KEY or not C.COPY_USE_GMGN:
        return None
    # GMGN bans the IP, not the key, and the board collectors share that IP.
    # Knocking while banned appears to extend it, and GMGN tells us exactly
    # when it ends — so wait it out rather than add a third caller hammering
    # through someone else's cooldown.
    if time.time() < _gmgn_quiet_until[0]:
        return None
    try:
        params = {"chain": "sol", "wallet_address": wallet, "type": "buy",
                  "limit": "20", "timestamp": str(int(time.time())),
                  "client_id": str(uuid.uuid4())}
        async with s.get(f"{GMGN_API}/v1/user/wallet_activity", params=params,
                         headers={"X-APIKEY": C.WATCH_GMGN_KEY,
                                  "User-Agent": "Mozilla/5.0"},
                         timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status in (429, 403):
                try:
                    reset = float((await r.json()).get("reset_at") or 0)
                except Exception:
                    reset = 0
                # Ask when it lifts rather than guess with backoff.
                _gmgn_quiet_until[0] = reset if reset > time.time() else time.time() + 120
                print(f"copywatch: gmgn {r.status} — quiet for "
                      f"{(_gmgn_quiet_until[0]-time.time())/60:.1f} min, "
                      f"reading the chain meanwhile", flush=True)
                return None
            if r.status != 200:
                print(f"copywatch: gmgn activity http {r.status} for "
                      f"{wallet[:8]}…", flush=True)
                return None
            d = await r.json()
    except Exception as e:
        print(f"copywatch: gmgn activity {wallet[:8]}…: {e}", flush=True)
        return None
    if d.get("code") not in (0, None):
        print(f"copywatch: gmgn activity {d.get('error')}", flush=True)
        return None

    inner = d.get("data") or d
    if isinstance(inner.get("data"), dict):
        inner = inner["data"]
    out = []
    for a in inner.get("activities") or []:
        if (a.get("event_type") or a.get("type")) != "buy":
            continue
        # Stated by GMGN rather than matched by us. A launch bundle is a
        # pump.fun buy; anything else this wallet does is not our signal.
        plat = (a.get("launchpad_platform") or "").lower()
        if "pump" not in plat:
            continue
        # The size floor, in dollars because that is what this route reports.
        # Undersized buys cannot complete the curve, so their coins never
        # migrate and never signal anyway.
        try:
            usd = float(a.get("cost_usd") or 0)
        except (TypeError, ValueError):
            usd = 0.0
        if usd < C.COPY_MIN_BUNDLE_USD:
            continue
        mint = (a.get("token") or {}).get("address")
        ts = a.get("timestamp")
        if mint and ts:
            out.append((mint, int(ts)))
    return out


async def _bundle_buys(s, wallet):
    """Mints this wallet has just bundle-bought on the pump.fun curve.

    GMGN first, the chain second. The fallback is not decoration: if GMGN
    rate-limits or the key lapses, reading signatures ourselves keeps the
    watcher working instead of quietly finding nothing.
    """
    if C.COPY_USE_GMGN:
        got = await _gmgn_buys(s, wallet)
        if got is not None:
            return got
        # Only worth saying when GMGN was meant to answer and did not.
        print(f"copywatch: falling back to chain reads for {wallet[:8]}…", flush=True)
    return await _chain_buys(s, wallet)


async def _chain_buys(s, wallet):
    """Fallback: the same question asked of the chain directly.

    Reads recent signatures and looks for a pump.fun swap where the wallet
    spent real size, computing what it paid from balance deltas.
    """
    now = time.time()
    try:
        # 100, not 25. Twenty-five spans ~43 min at this operator's current
        # rate, but they run 16-48 launches a day -- in a burst that window
        # shrinks below COPY_MAX_SIGNAL_AGE and bundles fall off the end
        # SILENTLY. One call either way; the limit is free headroom.
        r = await J.rpc(s, "getSignaturesForAddress",
                        [wallet, {"limit": 100}])
        sigs = (r or {}).get("result") or []
    except Exception as e:
        print(f"copywatch: signatures for {wallet[:8]}…: {e}", flush=True)
        return []

    out = []
    for row in sigs:
        sig = row.get("signature")
        if not sig or sig in _sig_seen:
            continue
        bt = row.get("blockTime") or 0
        # Older than the window we would ever act on: mark it seen and never
        # fetch it. This is what keeps a busy wallet cheap.
        if bt and now - bt > C.COPY_MAX_SIGNAL_AGE:
            _sig_seen.add(sig)
            continue
        if row.get("err"):
            _sig_seen.add(sig)
            continue

        try:
            t = await J.rpc(s, "getTransaction",
                            [sig, {"encoding": "jsonParsed",
                                   "maxSupportedTransactionVersion": 0}])
            tx = (t or {}).get("result")
        except Exception:
            continue                      # leave it unseen and retry next poll
        if not tx:
            _sig_seen.add(sig)
            continue
        _sig_seen.add(sig)

        meta = tx.get("meta") or {}
        if meta.get("err"):
            continue
        msg = (tx.get("transaction") or {}).get("message") or {}
        keys = [k.get("pubkey") if isinstance(k, dict) else k
                for k in (msg.get("accountKeys") or [])]

        # Did it go through pump.fun at all? Loaded addresses and inner
        # instructions both count — a bundle is rarely a bare top-level call.
        touched = PUMP_FUN_PROGRAM in keys or PUMP_FUN_PROGRAM in json.dumps(
            meta.get("innerInstructions") or [])
        if not touched:
            continue

        # What the wallet actually paid: native movement plus any wrapped leg.
        try:
            idx = keys.index(wallet)
        except ValueError:
            continue
        pre = (meta.get("preBalances") or [])
        post = (meta.get("postBalances") or [])
        if idx >= len(pre) or idx >= len(post):
            continue
        spent = (pre[idx] - post[idx]) / 1e9
        pre_w = {tb.get("accountIndex"): tb for tb in (meta.get("preTokenBalances") or [])
                 if tb.get("mint") == WSOL and tb.get("owner") == wallet}
        post_w = {tb.get("accountIndex"): tb for tb in (meta.get("postTokenBalances") or [])
                  if tb.get("mint") == WSOL and tb.get("owner") == wallet}
        for i, tb in pre_w.items():
            a = float((tb.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            b = float(((post_w.get(i) or {}).get("uiTokenAmount") or {}).get("uiAmount") or 0)
            spent += a - b

        if spent < C.COPY_MIN_BUNDLE_SOL:
            continue

        # The mint it received: a non-WSOL balance the wallet did not hold before.
        got = None
        had = {tb.get("mint") for tb in (meta.get("preTokenBalances") or [])
               if tb.get("owner") == wallet}
        for tb in (meta.get("postTokenBalances") or []):
            m = tb.get("mint")
            if not m or m == WSOL or tb.get("owner") != wallet:
                continue
            amt = float((tb.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            if amt > 0 and m not in had:
                got = m
                break
        if got:
            out.append((got, bt or int(now)))

    # Unbounded growth would be a slow leak on a long-running process.
    if len(_sig_seen) > 4000:
        for x in list(_sig_seen)[:2000]:
            _sig_seen.discard(x)
    return out


async def _coin_record(s, mint):
    """The coin, shaped like a board row, for a coin the board has never seen.

    copytrade catches coins AT MIGRATION -- below TRENDING_PRE_MIN_MC, so the
    board has no row to look up and the arm was refused outright. Rather than
    teach the API to fetch (it is a file read by design, and making a public
    route hit GMGN hands anyone our rate limit), the watcher supplies what it
    already has the key to ask for.

    Returns None if GMGN cannot answer, so the caller can still fall back to
    the board lookup instead of arming against a half-built record.
    """
    if not C.WATCH_GMGN_KEY:
        return None
    if time.time() < _gmgn_quiet_until[0]:
        return None
    try:
        params = {"chain": "sol", "address": mint,
                  "timestamp": str(int(time.time())),
                  "client_id": str(uuid.uuid4())}
        async with s.get(f"{GMGN_API}/v1/token/info", params=params,
                         headers={"X-APIKEY": C.WATCH_GMGN_KEY,
                                  "User-Agent": "Mozilla/5.0"},
                         timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                return None
            t = ((await r.json()) or {}).get("data") or {}
    except Exception as e:
        print(f"copywatch: token info for {mint[:8]}…: {e}", flush=True)
        return None
    if not t:
        return None

    px = t.get("price") or {}

    def _f(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    supply = _f(t.get("circulating_supply")) or _f(t.get("total_supply"))
    price = _f(px.get("price")) or _f(t.get("price"))
    buys = int(_f(px.get("buys_5m")))
    sells = int(_f(px.get("sells_5m")))
    bvol, svol = _f(px.get("buy_volume_5m")), _f(px.get("sell_volume_5m"))
    # A brand-new coin legitimately has no 5m history. Report what is there and
    # let the strategy decide -- do not invent momentum it has not shown.
    return {
        "mint": mint,
        "name": t.get("symbol") or t.get("name") or mint[:8],
        "symbol": t.get("symbol") or "",
        "logo": t.get("logo") or "",
        "mcap": round(price * supply, 2),
        "price": price,
        "liq": _f(t.get("liquidity")),
        "vol_5m": _f(px.get("volume_5m")),
        "buys_5m": buys,
        "sells_5m": sells,
        "swaps_5m": buys + sells,
        "buy_vol_5m": bvol,
        "sell_vol_5m": svol,
        "volr": round(bvol / svol, 3) if svol else 0.0,
        "holders": int(_f(t.get("holder_count"))),
        "top10_pct": round(_f(t.get("top_10_holder_rate")) * 100, 1),
        "launchpad": t.get("launchpad_platform") or t.get("launchpad") or "",
        # So a row that came from here is identifiable downstream.
        "source": "copywatch",
    }


async def _has_pool(s, mint):
    """True once the coin can actually be bought — i.e. it migrated.

    Asked as a Jupiter quote rather than by looking for a pool account, because
    a route is the thing that matters and it is a strictly stronger test: a pool
    that exists but cannot be routed is not a pool we can trade, and the buy
    that follows goes through this same router anyway. If Jupiter will not
    price it, the bot could not have filled it either.

    Quoted for a nominal amount. We are asking whether a route EXISTS, not what
    it costs — the real size is quoted again at the buy.
    """
    try:
        q = await J.quote(s, J.WSOL, mint, 10_000_000)      # 0.01 SOL, a probe
    except Exception:
        return False                        # a refusal is not a migration
    return bool(q and q.get("outAmount") and int(q["outAmount"]) > 0)


async def run(s, arm, live_count):
    """Watch the followed wallets.

    `arm` is bot.arm_mint and `live_count` returns how many sessions are open
    right now. Both are passed in so this module never imports the bot and can
    be tested on its own.
    """
    global _rules
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
                # Supplied, not looked up: the board has no row for a coin
                # this new. None falls back to the board lookup, which is the
                # right behaviour for a coin that HAS aged onto it.
                record = await _coin_record(s, mint)
                if record is None:
                    print(f"copywatch: {mint[:12]}… no token info — "
                          f"falling back to the board lookup", flush=True)
                try:
                    ok = await arm(s, mint, opts={"size_sol": size, "preset": preset},
                                   sig=record)
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
