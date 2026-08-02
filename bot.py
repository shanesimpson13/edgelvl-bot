"""
bot.py — the whole system, running.

    5m trending in  →  you GREENLIGHT  →  bot works the entry  →  bot takes profit

Nothing trades without your tap. Run it with DRY_RUN=1 until you've seen it work.

    python bot.py
"""
import asyncio
import json
import re
import time
from datetime import datetime, timezone

import aiohttp

import config as C
import jupiter as J
import ultra as U
import state as S
from strategy import Session

LOG = "trades.jsonl"
START_TS = time.time()

live_sessions = {}     # mint -> Session (coins currently being worked)
pending_tap = {}       # mint -> signal dict (offered, awaiting your call)
cancel_mints = set()   # coins you've asked the bot to stop watching

# Persisted across restarts so a crash can't lose a bag you're holding.
open_positions, seen_signals, armed_mints = S.load()   # seen_ kept for state compat


# ── saying what happened ────────────────────────────────────────────────────
async def note(s, text, buttons=None):
    """Say what just happened.

    These used to be Telegram messages. The terminal is the only surface now, so
    they go to the log — where a restart, a stood-down coin or a failed sell is
    still recoverable after the fact. `buttons` is accepted and ignored so the
    call sites didn't all have to change.
    """
    plain = re.sub(r"<[^>]+>", "", text).replace(chr(10), " | ")
    print("· " + plain, flush=True)







def _age(secs):
    if secs is None:
        return "?"
    if secs < 3600:
        return f"{secs/60:.0f}m"
    if secs < 86400:
        return f"{secs/3600:.0f}h"
    return f"{secs/86400:.0f}d"




async def fetch_settings(s):
    """Your settings from edgelvl.app. Falls back to the shipped defaults.

    Read when a session STARTS, never mid-trade — see the note in strategy.py.
    """
    try:
        async with s.get(f"{C.EDGE_API}/api/settings",
                         headers={"Authorization": f"Bearer {C.EDGE_API_KEY}"},
                         timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                return {}
            return (await r.json()).get("settings") or {}
    except Exception as e:
        print(f"settings error: {e}", flush=True)
        return {}


def _band(sess, MC):
    """The market caps that would actually fire a buy.

    Not just "dip then reclaim". The dip branch swallows every price at or below
    hi*(1-DIP) and returns before the reclaim is even considered, so a bounce off
    a deep low does nothing until price climbs back above that line. The window
    that really fires is:

        max(hi*(1-DIP), low*BOUNCE)   <   price   <   peak*PICO

    which on a coin that fell hard is a long way above where it is now.
    """
    if not sess.hi or not sess.peak:
        return None
    floor = sess.hi * (1 - sess.dip)
    if sess.low:
        floor = max(floor, sess.low * sess.bounce)
    ceiling = sess.peak * sess.pico
    return {
        "hi": MC(sess.hi),
        "buy_from": MC(floor),
        "buy_to": MC(ceiling),
        "reachable": floor < ceiling,
        "dipped": sess.low is not None,
        "warmup_left": max(0, sess.warmup - sess.n),
    }


async def report_status(s, mint, name, state, mc, pnl_frac, mult=None,
                        open_pct=None, band=None, targets=None):
    """Tell the terminal what the bot just did.

    One-way and best-effort: the bot owns positions and P&L, the terminal only
    mirrors them. A failure here must never affect a trade, so it's swallowed.
    """
    try:
        await s.post(f"{C.EDGE_API}/api/status",
                     json={"mint": mint, "name": name, "state": state,
                           "mc": mc, "pnl": pnl_frac, "dry_run": C.DRY_RUN,
                           "mult": mult, "open_pct": open_pct, "band": band,
                           "targets": targets},
                     headers={"Authorization": f"Bearer {C.EDGE_API_KEY}"},
                     timeout=aiohttp.ClientTimeout(total=10))
    except Exception:
        pass


async def poll_web_unarms(s):
    """Coins you dropped in the terminal."""
    hdrs = {"Authorization": f"Bearer {C.EDGE_API_KEY}"}
    while True:
        try:
            async with s.get(f"{C.EDGE_API}/api/unarms", headers=hdrs,
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                mints = (await r.json()).get("mints", []) if r.status == 200 else []
        except Exception:
            mints = []
        for mint in mints:
            try:
                await unarm(s, mint, source="the terminal")
            except Exception as e:
                print(f"unarm error {mint[:8]}: {e}", flush=True)
            try:
                async with s.post(f"{C.EDGE_API}/api/unarms/ack",
                                  json={"mint": mint}, headers=hdrs,
                                  timeout=aiohttp.ClientTimeout(total=15)):
                    pass
            except Exception:
                pass
        await asyncio.sleep(C.GREENLIGHT_POLL_SEC)


async def unarm(s, mint, source="you"):
    """Stop watching a coin.

    Only ever cancels a coin we have NOT bought. If there's a position open,
    refuse — dropping the session would leave a bag in the wallet with no
    take-profit and no stop, which is the worst state this bot can be in.
    """
    sess = live_sessions.get(mint)
    if sess is None:
        armed_mints.discard(mint)
        S.save(open_positions, seen_signals, armed_mints)
        return False

    if sess.state == "POS" or mint in open_positions:
        await note(s, f"⚠️ <b>{sess.name}</b> is already bought — not dropping it.\n"
                         f"The bot keeps managing the exit. Sell it yourself if you want out now.")
        return False

    cancel_mints.add(mint)
    await note(s, f"🛑 <b>{sess.name}</b> unarmed by {source} — no longer watching.")
    return True


async def arm_mint(s, mint):
    """Start working a coin. Every greenlight from the terminal lands here.

    Every outcome is logged, including the failures — a greenlight that quietly
    didn't arm used to leave no trace to diagnose afterwards.

    Returns True if a session started. The card might be from an earlier run of
    the bot (you restarted, or you're scrolling back), so if we don't recognise
    the mint, go and fetch it rather than silently ignoring you.
    """
    print(f"ARM-REQ {mint[:12]}…", flush=True)

    if mint in live_sessions:
        print(f"ARM-SKIP {mint[:12]}… already working it", flush=True)
        await note(s, "Already working that one.")
        return False

    sig = pending_tap.pop(mint, None)
    if sig is None:
        sig = await lookup_coin(s, mint)
    if sig is None:
        print(f"ARM-FAIL {mint[:12]}… lookup returned nothing", flush=True)
        await note(s, "Couldn't load that coin — it may have aged out "
                         "of the feed. Tap a more recent signal.")
        return False

    armed_mints.add(mint)
    S.save(open_positions, seen_signals, armed_mints)
    asyncio.create_task(work_coin(s, sig))
    return True


async def poll_web_greenlights(s):
    """Coins you greenlit in the terminal at edgelvl.app.

    The terminal never touches your wallet — it just queues the mint against your
    licence key. This is the bot picking that up and arming it.
    """
    hdrs = {"Authorization": f"Bearer {C.EDGE_API_KEY}"}
    while True:
        try:
            async with s.get(f"{C.EDGE_API}/api/greenlights", headers=hdrs,
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                mints = (await r.json()).get("mints", []) if r.status == 200 else []
        except Exception:
            mints = []

        for mint in mints:
            # Never let one bad coin kill this loop — an uncaught error here
            # takes the task down and every later greenlight silently does
            # nothing, which is the worst possible failure for this.
            try:
                armed = await arm_mint(s, mint)
            except Exception as e:
                print(f"arm error {mint[:8]}: {e}", flush=True)
                armed = False
            if armed:
                await note(s, "🖥 Greenlit from the terminal.")
            # Ack either way — a mint we can't arm shouldn't be handed back forever.
            try:
                async with s.post(f"{C.EDGE_API}/api/greenlights/ack",
                                  json={"mint": mint}, headers=hdrs,
                                  timeout=aiohttp.ClientTimeout(total=15)):
                    pass
            except Exception:
                pass

        await asyncio.sleep(C.GREENLIGHT_POLL_SEC)


async def lookup_coin(s, mint):
    """Find one coin by mint.

    Resolves against the last 30 minutes of the board, not just what's on it
    right now — a coin near the bottom can drop off between you greenlighting it
    and this call, and refusing a coin you were just looking at would be wrong.
    """
    try:
        async with s.get(f"{C.EDGE_API}/api/coin/{mint}",
                         headers={"Authorization": f"Bearer {C.EDGE_API_KEY}"},
                         timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                return None
            c = (await r.json()).get("coin") or {}
    except Exception as e:
        print(f"lookup error: {e}")
        return None

    if not c.get("mint"):
        return None
    # symbol is what the board shows; fall back to the long name
    return {**c, "mint": mint, "name": c.get("symbol") or c.get("name") or mint[:8]}


def _money(v):
    """$1.2K / $185.9K / $2.4M — readable at a glance."""
    v = float(v or 0)
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v/1_000:.1f}K"
    return f"${v:,.0f}"


async def dry_or_live_buy(s, mint, size_sol=None):
    """Returns (tokens_received, sol_spent, fill_price, error)."""
    size_sol = C.SIZE_SOL if size_sol is None else size_sol
    if not C.DRY_RUN:
        # Routed by wallet type: a local key goes through Jupiter Ultra, a Privy
        # wallet through the swap API so we control the build. Either way this
        # returns only once the swap has actually landed.
        got, err = await J.execute_buy(s, mint, size_sol)
        if err or got <= 0:
            return 0, 0.0, 0.0, err or "swap_failed"
        cost = size_sol + C.GAS_SOL
        return got, cost, cost / (got / 1e6), None

    q = await J.quote(s, J.WSOL, mint, int(size_sol * 1e9))
    got = int(q.get("outAmount", 0) or 0)
    if got <= 0:
        return 0, 0.0, 0.0, "no_route"
    cost = size_sol + C.GAS_SOL            # gas is real money in dry run too
    return got, cost, cost / (got / 1e6), None


async def dry_or_live_sell(s, mint, raw_amount):
    """Returns (sol_received_net_of_gas, error)."""
    if raw_amount <= 0:
        return 0.0, "nothing_to_sell"

    if not C.DRY_RUN:
        out, err = await J.execute_sell(s, mint, raw_amount)
        if err:
            return 0.0, err
        return max(0.0, out - C.GAS_SOL), None

    q = await J.quote(s, mint, J.WSOL, int(raw_amount))
    out = int(q.get("outAmount", 0) or 0)
    if out <= 0:
        return 0.0, "no_route"
    return max(0.0, out / 1e9 - C.GAS_SOL), None


# ── working a single coin ───────────────────────────────────────────────────
async def work_coin(s, sig):
    """From your tap until we're flat. One task per coin."""
    mint = sig["mint"]
    name = sig.get("name", mint[:8])
    volr = sig.get("volr")
    # 5m transaction count -> swaps per second, which sets how long the strategy
    # watches before it's allowed to enter. Fast coins get a short warmup.
    swaps_5m = sig.get("swaps_5m")
    sps = (swaps_5m / 300.0) if isinstance(swaps_5m, (int, float)) and swaps_5m > 0 else None
    cfg = await fetch_settings(s)
    sess = Session(mint, name,
                   volr=volr if isinstance(volr, (int, float)) else None,
                   swaps_per_sec=sps, cfg=cfg)

    # size and the clocks are the bot's, not the strategy's
    size_sol = float(cfg.get("size_sol", C.SIZE_SOL))
    entry_timeout = float(cfg.get("entry_timeout", C.ENTRY_TIMEOUT))
    max_hold = float(cfg.get("max_hold", C.MAX_HOLD))
    live_sessions[mint] = sess

    blocked = sess.blocked_reason()
    if blocked:
        print(f"ARM-BLOCKED {name}: {blocked}", flush=True)
        await note(s, f"🛑 <b>{name}</b> skipped — {blocked}")
        live_sessions.pop(mint, None)
        # This returns before the cleanup below, so drop it here too — otherwise
        # a blocked coin sits in the armed set and re-blocks on every restart.
        armed_mints.discard(mint)
        S.save(open_positions, seen_signals, armed_mints)
        return

    mode = "DRY RUN" if C.DRY_RUN else "🔴 LIVE"
    print(f"ARMED {name} ({mint[:12]}…)", flush=True)
    await report_status(s, mint, name, "watching", None, None)
    await note(
        s,
        f"👀 <b>{name}</b> armed · {mode}\n"
        f"Watching every second. It buys only after a <b>-{int(sess.dip*100)}% dip</b> "
        f"then a <b>+{int((sess.bounce-1)*100)}% bounce</b> — never the top.\n"
        f"<i>~{int(sess.warmup*C.POLL_SEC)}s warmup first. "
        f"Stands down after {int(entry_timeout/60)} min if no setup appears.</i>")

    t0 = time.time()
    dead_polls = 0      # consecutive polls with no price back
    tokens_original = 0
    last_band = 0.0

    # Market cap, never raw price — a price like 1.885e-07 tells you nothing.
    # The board's market cap and our first quote are from the same moment, so
    # their ratio converts any later price to a market cap. Self-calibrating:
    # no hardcoded token supply, no hardcoded SOL price to go stale.
    mc_factor = None
    arm_mcap = sig.get("mcap") or 0

    def MC(px):
        if not px:
            return "—"
        return _money(px * mc_factor) if mc_factor else f"{px:.3e}"
    tokens_held = 0
    entry_px = None
    spent = 0.0
    received = 0.0
    pending = None      # action queued last poll — fills on THIS one (1s latency, like reality)

    try:
        while True:
            # you asked us to drop this one (only ever possible pre-entry)
            if mint in cancel_mints:
                cancel_mints.discard(mint)
                await report_status(s, mint, name, "unarmed", None, None)
                break

            # time limits
            elapsed = time.time() - t0
            if sess.state == "WAIT" and elapsed > entry_timeout:
                await note(s, f"⏭️ <b>{name}</b> — no entry in {int(entry_timeout/60)}min, standing down.")
                await report_status(s, mint, name, "stood down", None, None)
                break
            if sess.state == "POS" and elapsed > max_hold:
                got, err = await dry_or_live_sell(s, mint, tokens_held)
                if not err:
                    received += got
                    tokens_held = 0
                await note(s, f"⌛ <b>{name}</b> — max hold reached, closed out.")
                break

            price = await J.get_price(s, mint)

            # A dead price feed is indistinguishable from a quiet coin: the
            # strategy just never sees a dip and waits out the clock. Say so
            # rather than looking busy while receiving nothing.
            if price is None:
                dead_polls += 1
                if dead_polls == 30:
                    await note(
                        s, f"⚠️ <b>{name}</b> — no price coming back from Jupiter.\n"
                           f"Nothing can trigger without prices. Usually rate limiting: "
                           f"set <code>JUP_API_KEY</code> (free at portal.jup.ag) or watch "
                           f"fewer coins at once.")
                    print(f"NO PRICE {name}: 30 consecutive failed quotes", flush=True)
            elif dead_polls:
                if dead_polls >= 30:
                    await note(s, f"✅ <b>{name}</b> — price feed recovered.")
                dead_polls = 0

            if mc_factor is None and price and arm_mcap:
                mc_factor = arm_mcap / price      # first quote pairs with the board's mcap

            # Publish the live entry band: the dip it needs, and the reclaim
            # that would fire. Only while waiting, and only every 15s — this is
            # our own API, but there's no reason to chatter.
            if (sess.state == "WAIT" and price and mc_factor
                    and time.time() - last_band > 15):
                last_band = time.time()
                # Buy/sell balance is checked AT THE BUY, so it has to be the
                # current reading — the one from when you greenlit is stale by
                # the time a setup appears.
                fresh = await lookup_coin(s, mint)
                if fresh and isinstance(fresh.get("volr"), (int, float)):
                    sess.volr = fresh["volr"]
                await report_status(
                    s, mint, name, "watching", MC(price), None,
                    band=_band(sess, MC))

            # ── fill whatever was decided on the PREVIOUS poll ──────────────
            # Real trades don't fill at the price that triggered them. By the time
            # your transaction lands the market has moved. Dry run models that the
            # same way live does: decide now, fill on the next tick, at a real
            # Jupiter quote for the real size (fees + price impact included).
            if pending is not None:
                act, pending = pending, None

                if act[0] == "BUY":
                    got, cost, fill_px, err = await dry_or_live_buy(s, mint, size_sol)
                    if err:
                        await note(s, f"❌ <b>{name}</b> buy failed — {err}. No money spent.")
                        break
                    tokens_held, spent, entry_px = got, cost, fill_px
                    tokens_original = got        # the ladder is fractions of THIS
                    sess.on_filled(fill_px)
                    open_positions[mint] = {"name": name, "tokens_raw": tokens_held,
                                            "entry_px": fill_px, "spent_sol": spent,
                                            "opened": time.time()}
                    S.save(open_positions, seen_signals, armed_mints)
                    tag = " (dry)" if C.DRY_RUN else ""
                    await note(s, f"🎯 <b>{name}</b> BUY{tag} · {size_sol} SOL\n"
                                     f"Entry at <b>{MC(fill_px)}</b> MC\n"
                                     f"TP {MC(fill_px*sess.tps[0])} / {MC(fill_px*sess.tps[1])} · "
                                     f"stop -{int(sess.kill*100)}% off peak")
                    await report_status(
                        s, mint, name, "bought", MC(fill_px), None,
                        targets={"entry": MC(fill_px),
                                 "tps": [MC(fill_px * t) for t in sess.tps],
                                 "stop": MC(fill_px * (1 - sess.kill))})

                elif act[0] == "SELL":
                    frac, label = act[1], act[2]
                    rung = sess.tp_done                  # already incremented
                    is_last = rung >= len(sess.tps)
                    # Fractions are of the ORIGINAL position, not what's left —
                    # taking frac of the remainder would sell 30% of 30% on the
                    # second rung and quietly strand the rest. The last rung
                    # sells everything still held, which also clears dust.
                    amount = tokens_held if is_last else min(int(tokens_original * frac), tokens_held)
                    got, err = await dry_or_live_sell(s, mint, amount)
                    if err:
                        await note(s, f"⚠️ <b>{name}</b> {label} sell failed — {err}. Still holding.")
                    else:
                        received += got
                        tokens_held -= amount
                        if mint in open_positions:
                            open_positions[mint]["tokens_raw"] = tokens_held
                            S.save(open_positions, seen_signals, armed_mints)
                        tag = " (dry)" if C.DRY_RUN else ""
                        sold_pct = round(amount / max(tokens_original, 1) * 100)
                        open_pct = round(tokens_held / max(tokens_original, 1) * 100)
                        mult = sess.tps[rung - 1] if rung else None
                        await note(
                            s, f"💰 <b>{name}</b> TP{rung}{tag} · sold {sold_pct}% at "
                               f"<b>{MC(price)}</b> MC ({mult}x) for {got:.4f} SOL"
                               + (f"\n{open_pct}% still riding to {sess.tps[rung]}x." if not is_last else ""))
                        await report_status(
                            s, mint, name, f"tp{rung}", MC(price),
                            (received - spent) / max(spent, 1e-9),
                            mult=mult, open_pct=open_pct,
                            # entry and the remaining rung, so a part-sold
                            # position still says where the rest gets out
                            targets={"entry": MC(entry_px),
                                     "tps": [MC(entry_px * t) for t in sess.tps],
                                     "next": MC(entry_px * sess.tps[rung]) if rung < len(sess.tps) else None,
                                     "stop": MC(sess.ppeak * (1 - sess.kill)) if sess.ppeak else None})
                    if tokens_held <= 0 or is_last:
                        sess.on_closed()
                        break

                elif act[0] == "SELLALL":
                    got, err = await dry_or_live_sell(s, mint, tokens_held)
                    if err:
                        await note(s, f"🚨 <b>{name}</b> STOP SELL FAILED — {err}. "
                                         f"<b>Check your wallet.</b>")
                    else:
                        received += got
                        tokens_held = 0
                        tag = " (dry)" if C.DRY_RUN else ""
                        await note(s, f"🔴 <b>{name}</b> stopped out{tag} at <b>{MC(price)}</b> MC "
                                         f"for {got:.4f} SOL")
                        await report_status(s, mint, name, "stopped", MC(price),
                                            (received - spent) / max(spent, 1e-9))
                    sess.on_closed()
                    break

            # ── decide (fills next poll) ───────────────────────────────────
            action = sess.feed(price)
            if action is not None:
                pending = action

            await asyncio.sleep(C.POLL_SEC)

    except Exception as e:
        import traceback
        print(f"work_coin {name} ERROR: {e}\n{traceback.format_exc()}", flush=True)
        await note(s, f"💥 <b>{name}</b> error: {e}")
    finally:
        print(f"DONE {name}: entered={entry_px is not None} "
              f"peak_after_tap={sess.peak_since_tap()}", flush=True)
        live_sessions.pop(mint, None)
        open_positions.pop(mint, None)
        # The session is over however it ended — bought out, stopped, timed out
        # or dropped. Leaving it armed would re-arm a finished coin on the next
        # restart, which is how you end up watching yesterday's trade.
        armed_mints.discard(mint)
        S.save(open_positions, seen_signals, armed_mints)
        row = record(sess, entry_px, spent, received)
        await push_trade(s, row)
        await report(s, sess, entry_px, spent, received)


async def push_trade(s, row):
    """Send a finished session to your journal. Best-effort — the local
    trades.jsonl is the record of truth, this is for reading it back."""
    try:
        await s.post(f"{C.EDGE_API}/api/trades", json=row,
                     headers={"Authorization": f"Bearer {C.EDGE_API_KEY}"},
                     timeout=aiohttp.ClientTimeout(total=10))
    except Exception:
        pass


def record(sess, entry_px, spent, received):
    """Every trade, on disk. This file is what Modules 05-07 tune against."""
    peak_mult = sess.peak_since_tap()
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "name": sess.name,
        "mint": sess.mint,
        "dry_run": C.DRY_RUN,
        "volr": sess.volr,
        "entered": entry_px is not None,
        "entry_px": entry_px,
        "spent_sol": round(spent, 6),
        "received_sol": round(received, 6),
        "pnl_sol": round(received - spent, 6) if entry_px else 0.0,
        # SELECTION metric: how far it ran after your tap, regardless of the strategy
        "peak_after_tap": round(peak_mult, 3) if peak_mult else None,
        "config": {"DIP": C.DIP, "BOUNCE": C.BOUNCE, "TPS": list(C.TPS),
                   "FRACS": list(C.FRACS), "KILL": C.KILL},
    }
    with open(LOG, "a") as f:
        f.write(json.dumps(row) + "\n")
    return row


async def report(s, sess, entry_px, spent, received):
    peak = sess.peak_since_tap()
    peak_txt = f"{peak:.2f}x" if peak else "n/a"
    if entry_px is None:
        await note(s, f"📊 <b>{sess.name}</b> — no trade taken. "
                         f"It ran <b>{peak_txt}</b> from your tap.")
        return
    pnl = received - spent
    emoji = "✅" if pnl > 0 else "🔴"
    tag = "(dry)" if C.DRY_RUN else ""
    await note(s, f"{emoji} <b>{sess.name}</b> done {tag} · "
                     f"{pnl:+.4f} SOL · peak after tap {peak_txt}")


# ── main ────────────────────────────────────────────────────────────────────
async def wallet_check(s):
    """Say what's signing, and refuse to look ready when we aren't.

    The failure this exists to prevent: bot LIVE, wallet holds native SOL, and
    every single buy fails with no_route because a Privy wallet can only spend
    WSOL. That looks exactly like a quiet market, and you'd never know.
    """
    w = J.WALLET
    if w is None:
        if not C.DRY_RUN:
            print("LIVE with no wallet configured — nothing can trade.", flush=True)
            return "⚠️ <b>No wallet configured</b> — set PRIVY_WALLET_ID or PRIVATE_KEY."
        return None

    kind = "Privy (bot holds no key)" if w.kind == "privy" else "local key"
    print(f"wallet: {w.address} · {kind}", flush=True)
    if C.DRY_RUN or w.kind != "privy":
        return None

    bal = await J.wsol_balance(s)
    if bal is None:
        return (f"⚠️ <b>No WSOL account yet</b> — nothing can be bought.\n"
                f"Deposit SOL to <code>{w.address}</code> and wrap it, "
                f"then restart.")
    if bal < C.SIZE_SOL:
        return (f"⚠️ <b>Trading balance too low</b> — {bal:.4f} WSOL, "
                f"but trades are {C.SIZE_SOL} SOL.\n"
                f"Top up <code>{w.address}</code>.")
    print(f"trading balance: {bal:.4f} WSOL", flush=True)
    return None


async def main():
    mode = "DRY RUN — no real money" if C.DRY_RUN else "🔴 LIVE — REAL MONEY"
    print(f"edgelvl bot up · {mode} · {C.SIZE_SOL} SOL/trade · poll {C.POLL_SEC}s")

    async with aiohttp.ClientSession() as s:
        # Checked before announcing anything, reported after — so the "online"
        # message is never the last word when the wallet can't actually trade.
        warning = await wallet_check(s)
        await note(s, f"Bot online — {mode} · {C.SIZE_SOL} SOL per trade · "
                      f"TP {C.TPS[0]}x/{C.TPS[1]}x · greenlight from the terminal")

        if warning:
            await note(s, warning)

        # ── did we come back holding something? ─────────────────────────────
        # If the bot died mid-trade, those positions are still in your wallet
        # with no take-profit and no stop. Check the wallet, tell the user, and
        # never pretend a bag doesn't exist just because we forgot about it.
        if open_positions and not C.DRY_RUN:
            notes = await S.reconcile(s, open_positions, J.token_balance)
            for n in notes:
                await note(s, f"🔄 {n}")
            S.save(open_positions, seen_signals, armed_mints)

        if open_positions:
            lines = "\n".join(
                f"• <b>{p.get('name', m[:8])}</b> — {int(p.get('tokens_raw', 0)):,} tokens"
                for m, p in open_positions.items())
            await note(
                s,
                f"⚠️ <b>You still hold {len(open_positions)} position(s)</b> from before the restart:\n"
                f"{lines}\n\n"
                f"The bot is <b>not</b> managing these — no take-profit, no stop. "
                f"Sell them yourself, or greenlight the coin again to hand it back to the bot.")

        # ── coins that were armed when we stopped ───────────────────────────
        # Their sessions were in memory and died with the process. Pick them
        # back up rather than leaving you thinking a coin is being watched.
        if armed_mints:
            recovered = []
            for mint in list(armed_mints):
                sig = await lookup_coin(s, mint)
                if sig and mint not in live_sessions:
                    asyncio.create_task(work_coin(s, sig))
                    recovered.append(sig.get("name", mint[:8]))
                else:
                    armed_mints.discard(mint)      # gone from both feeds
            S.save(open_positions, seen_signals, armed_mints)
            if recovered:
                await note(s, f"🔄 <b>Picked back up after the restart:</b> "
                                 f"{', '.join(recovered)}")

        asyncio.create_task(poll_web_greenlights(s))
        asyncio.create_task(poll_web_unarms(s))
        # Nothing to poll for input any more — greenlights arrive from the
        # terminal. Stay alive so the watcher tasks keep running.
        while True:
            await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
