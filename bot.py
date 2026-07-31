"""
bot.py — the whole system, running.

    signals in  →  you tap GREENLIGHT  →  bot works the entry  →  bot takes profit

Nothing trades without your tap. Run it with DRY_RUN=1 until you've seen it work.

    python bot.py
"""
import asyncio
import json
import time
from datetime import datetime, timezone

import aiohttp

import config as C
import jupiter as J
import ultra as U
import state as S
from strategy import Session

TG = f"https://api.telegram.org/bot{C.TG_TOKEN}"
LOG = "trades.jsonl"
START_TS = time.time()   # ignore alerts that fired before the bot started

live_sessions = {}     # mint -> Session (coins currently being worked)
pending_tap = {}       # mint -> signal dict (offered, awaiting your call)

# Persisted across restarts so a crash can't lose a bag you're holding.
open_positions, seen_signals = S.load()


# ── telegram ────────────────────────────────────────────────────────────────
async def tg_send(s, text, buttons=None):
    payload = {"chat_id": C.TG_CHAT, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    try:
        async with s.post(f"{TG}/sendMessage", json=payload) as r:
            return await r.json()
    except Exception as e:
        print(f"tg error: {e}")
        return {}


async def tg_poll_taps(s, offset):
    """Watch for your GREENLIGHT taps."""
    try:
        async with s.get(f"{TG}/getUpdates",
                         params={"offset": offset, "timeout": 5},
                         timeout=aiohttp.ClientTimeout(total=15)) as r:
            data = await r.json()
    except Exception:
        return offset

    for upd in data.get("result", []):
        offset = upd["update_id"] + 1
        cb = upd.get("callback_query")
        if not cb:
            continue

        # only YOU can arm trades
        if str(cb["from"]["id"]) != str(C.TG_CHAT):
            continue

        data_str = cb.get("data", "")
        async with s.post(f"{TG}/answerCallbackQuery",
                          json={"callback_query_id": cb["id"]}):
            pass

        if data_str.startswith("go:"):
            mint = data_str[3:]
            await arm_mint(s, mint)
    return offset


async def arm_mint(s, mint):
    """Start working a coin. Both the Telegram tap and the web terminal land here.

    Returns True if a session started. The card might be from an earlier run of
    the bot (you restarted, or you're scrolling back), so if we don't recognise
    the mint, go and fetch it rather than silently ignoring you.
    """
    if mint in live_sessions:
        await tg_send(s, "Already working that one.")
        return False

    sig = pending_tap.pop(mint, None)
    if sig is None:
        sig = await lookup_signal(s, mint)
    if sig is None:
        await tg_send(s, "Couldn't load that coin — it may have aged out "
                         "of the feed. Tap a more recent signal.")
        return False

    asyncio.create_task(work_coin(s, sig))
    return True


async def poll_web_greenlights(s):
    """Coins you greenlit in the terminal at edgelvl.app.

    The terminal never touches your wallet — it just queues the mint against your
    licence key. This is the bot picking that up and arming it, exactly as if you
    had tapped the button in Telegram. Everything after this is identical.
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
            armed = await arm_mint(s, mint)
            if armed:
                await tg_send(s, "🖥 Greenlit from the terminal.")
            # Ack either way — a mint we can't arm shouldn't be handed back forever.
            try:
                async with s.post(f"{C.EDGE_API}/api/greenlights/ack",
                                  json={"mint": mint}, headers=hdrs,
                                  timeout=aiohttp.ClientTimeout(total=15)):
                    pass
            except Exception:
                pass

        await asyncio.sleep(C.GREENLIGHT_POLL_SEC)


async def lookup_signal(s, mint):
    """Find one alert by mint. Used when you tap a card this run didn't send."""
    for sig in await fetch_signals(s, limit=200):
        if sig.get("mint") == mint:
            return sig
    return None


# ── signal feed ─────────────────────────────────────────────────────────────
async def fetch_signals(s, limit=25):
    """Live signals. /api/journal is the members endpoint — no delay.

    (The public /api/feed is deliberately 1 hour behind; it's for the website.
    Trading off it would mean buying coins that already finished moving.)
    """
    try:
        async with s.get(f"{C.EDGE_API}/api/journal",
                         params={"limit": limit},
                         headers={"Authorization": f"Bearer {C.EDGE_API_KEY}"},
                         timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status == 401:
                print("API key rejected (401) — check EDGE_API_KEY / subscription active")
                return []
            data = await r.json()
    except Exception as e:
        print(f"feed error: {e}")
        return []
    if data.get("delayed"):
        print("WARNING: feed reports delayed data — not tradeable")
        return []
    return data.get("alerts", [])


async def offer_signals(s):
    """Push new signals to Telegram with a GREENLIGHT button."""
    while True:
        try:
            for sig in await fetch_signals(s):
                mint = sig.get("mint")
                if not mint or mint in seen_signals:
                    continue
                if (sig.get("alert_time") or 0) < START_TS:
                    seen_signals.add(mint)      # pre-existing backlog: mark, don't offer
                    continue
                seen_signals.add(mint)
                pending_tap[mint] = sig
                S.save(open_positions, seen_signals)

                await tg_send(s, signal_card(sig),
                              [[{"text": "🟢 GREENLIGHT", "callback_data": f"go:{mint}"}]])
        except Exception as e:
            print(f"offer error: {e}")
        await asyncio.sleep(C.FEED_POLL_SEC)



def _money(v):
    """$1.2K / $185.9K / $2.4M — readable at a glance."""
    v = float(v or 0)
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v/1_000:.1f}K"
    return f"${v:,.0f}"


def signal_card(sig):
    """The message you actually decide from.

    Everything here answers a Module 04 question: is it moving, is there room
    left, is it liquid enough, and where's the chart.
    """
    name = sig.get("name") or sig.get("mint", "")[:8]
    mint = sig.get("mint", "")
    mc = sig.get("alert_mcap") or 0
    ath = sig.get("max_mcap") or 0
    now = sig.get("current_mcap") or 0
    liq = sig.get("liquidity") or 0
    holders = int(sig.get("holders") or 0)
    growth = sig.get("holder_growth_pct")
    pc5, pc1h = sig.get("pc5"), sig.get("pc1h")
    age_min = (time.time() - (sig.get("alert_time") or time.time())) / 60

    lines = [f"🔔 <b>{name}</b>  ·  alerted {age_min:.0f}m ago", ""]

    mc_line = f"MC {_money(mc)}"
    if now and abs(now - mc) / max(mc, 1) > 0.05:      # only if it actually moved
        mc_line += f" → now {_money(now)}"
    if ath and ath > mc * 1.05:
        mc_line += f"  ·  ATH {_money(ath)}"
    lines.append(mc_line)

    # momentum — the "is this chart alive" read
    mom = []
    if isinstance(pc5, (int, float)):
        mom.append(f"5m {pc5:+.0f}%")
    if isinstance(pc1h, (int, float)):
        mom.append(f"1h {pc1h:+.0f}%")
    if mom:
        lines.append("  ·  ".join(mom))

    hold = f"{holders:,} holders"
    if isinstance(growth, (int, float)):
        hold += f" ({growth:+.1f}%)"
    lines.append(f"Liq {_money(liq)}  ·  {hold}")

    volr = sig.get("volr")
    if isinstance(volr, (int, float)):
        lines.append(f"volR {volr:.2f}" + ("  ⚠️ below floor" if volr < C.VOLR_MIN else ""))

    lines += ["", f'<a href="https://gmgn.ai/sol/token/{mint}">chart ↗</a>', f"<code>{mint}</code>"]
    return "\n".join(lines)


# ── fills ───────────────────────────────────────────────────────────────────
# Dry run and live take the SAME path up to the point of signing. Both price the
# trade with a real Jupiter quote at the real size, so fees, routing and price
# impact are identical. The only thing dry run skips is broadcasting it.
# That means dry-run P&L is directly comparable to live P&L — not a rosy version.

async def dry_or_live_buy(s, mint):
    """Returns (tokens_received, sol_spent, fill_price, error)."""
    if not C.DRY_RUN:
        # Ultra: order -> sign -> execute, with on-chain confirmation inline.
        # When this returns a signature, the transaction has already landed.
        sig, got = await U.ultra_swap(s, U.SOL_MINT, mint,
                                      int(C.SIZE_SOL * 1e9), J.kp, C.JUP_API_KEY)
        if not sig or got <= 0:
            return 0, 0.0, 0.0, "swap_failed"
        cost = C.SIZE_SOL + C.GAS_SOL
        return got, cost, cost / (got / 1e6), None

    q = await J.quote(s, J.WSOL, mint, int(C.SIZE_SOL * 1e9))
    got = int(q.get("outAmount", 0) or 0)
    if got <= 0:
        return 0, 0.0, 0.0, "no_route"
    cost = C.SIZE_SOL + C.GAS_SOL          # gas is real money in dry run too
    return got, cost, cost / (got / 1e6), None


async def dry_or_live_sell(s, mint, raw_amount):
    """Returns (sol_received_net_of_gas, error)."""
    if raw_amount <= 0:
        return 0.0, "nothing_to_sell"

    if not C.DRY_RUN:
        sig, out = await U.ultra_swap(s, mint, U.SOL_MINT,
                                      int(raw_amount), J.kp, C.JUP_API_KEY)
        if not sig:
            return 0.0, "swap_failed"
        return max(0.0, out / 1e9 - C.GAS_SOL), None

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
    sess = Session(mint, name, volr=volr if isinstance(volr, (int, float)) else None)
    live_sessions[mint] = sess

    blocked = sess.blocked_reason()
    if blocked:
        await tg_send(s, f"🛑 <b>{name}</b> skipped — {blocked}")
        live_sessions.pop(mint, None)
        return

    mode = "DRY RUN" if C.DRY_RUN else "🔴 LIVE"
    print(f"ARMED {name} ({mint[:12]}…)", flush=True)
    await tg_send(
        s,
        f"👀 <b>{name}</b> armed · {mode}\n"
        f"Watching every second. It buys only after a <b>-{int(C.DIP*100)}% dip</b> "
        f"then a <b>+{int((C.BOUNCE-1)*100)}% bounce</b> — never the top.\n"
        f"<i>~{int(C.WARMUP*C.POLL_SEC/60)} min warmup first. "
        f"Stands down after {int(C.ENTRY_TIMEOUT/60)} min if no setup appears.</i>")

    t0 = time.time()
    tokens_held = 0
    entry_px = None
    spent = 0.0
    received = 0.0
    pending = None      # action queued last poll — fills on THIS one (1s latency, like reality)

    try:
        while True:
            # time limits
            elapsed = time.time() - t0
            if sess.state == "WAIT" and elapsed > C.ENTRY_TIMEOUT:
                await tg_send(s, f"⏭️ <b>{name}</b> — no entry in {int(C.ENTRY_TIMEOUT/60)}min, standing down.")
                break
            if sess.state == "POS" and elapsed > C.MAX_HOLD:
                got, err = await dry_or_live_sell(s, mint, tokens_held)
                if not err:
                    received += got
                    tokens_held = 0
                await tg_send(s, f"⌛ <b>{name}</b> — max hold reached, closed out.")
                break

            price = await J.get_price(s, mint)

            # ── fill whatever was decided on the PREVIOUS poll ──────────────
            # Real trades don't fill at the price that triggered them. By the time
            # your transaction lands the market has moved. Dry run models that the
            # same way live does: decide now, fill on the next tick, at a real
            # Jupiter quote for the real size (fees + price impact included).
            if pending is not None:
                act, pending = pending, None

                if act[0] == "BUY":
                    got, cost, fill_px, err = await dry_or_live_buy(s, mint)
                    if err:
                        await tg_send(s, f"❌ <b>{name}</b> buy failed — {err}. No money spent.")
                        break
                    tokens_held, spent, entry_px = got, cost, fill_px
                    sess.on_filled(fill_px)
                    open_positions[mint] = {"name": name, "tokens_raw": tokens_held,
                                            "entry_px": fill_px, "spent_sol": spent,
                                            "opened": time.time()}
                    S.save(open_positions, seen_signals)
                    tag = " (dry)" if C.DRY_RUN else ""
                    await tg_send(s, f"🎯 <b>{name}</b> BUY{tag} · {C.SIZE_SOL} SOL @ {fill_px:.3e}\n"
                                     f"TP {C.TPS[0]}x/{C.TPS[1]}x · stop -{int(C.KILL*100)}% off peak")

                elif act[0] == "SELL":
                    frac, label = act[1], act[2]
                    amount = int(tokens_held * frac)
                    got, err = await dry_or_live_sell(s, mint, amount)
                    if err:
                        await tg_send(s, f"⚠️ <b>{name}</b> {label} sell failed — {err}. Still holding.")
                    else:
                        received += got
                        tokens_held -= amount
                        if mint in open_positions:
                            open_positions[mint]["tokens_raw"] = tokens_held
                            S.save(open_positions, seen_signals)
                        tag = " (dry)" if C.DRY_RUN else ""
                        await tg_send(s, f"💰 <b>{name}</b> {label}{tag} · sold {int(frac*100)}% "
                                         f"for {got:.4f} SOL")
                    if tokens_held <= 0 or sess.tp_done >= len(C.TPS):
                        sess.on_closed()
                        break

                elif act[0] == "SELLALL":
                    got, err = await dry_or_live_sell(s, mint, tokens_held)
                    if err:
                        await tg_send(s, f"🚨 <b>{name}</b> STOP SELL FAILED — {err}. "
                                         f"<b>Check your wallet.</b>")
                    else:
                        received += got
                        tokens_held = 0
                        tag = " (dry)" if C.DRY_RUN else ""
                        await tg_send(s, f"🔴 <b>{name}</b> stopped out{tag} for {got:.4f} SOL")
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
        await tg_send(s, f"💥 <b>{name}</b> error: {e}")
    finally:
        print(f"DONE {name}: entered={entry_px is not None} "
              f"peak_after_tap={sess.peak_since_tap()}", flush=True)
        live_sessions.pop(mint, None)
        open_positions.pop(mint, None)
        S.save(open_positions, seen_signals)
        record(sess, entry_px, spent, received)
        await report(s, sess, entry_px, spent, received)


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


async def report(s, sess, entry_px, spent, received):
    peak = sess.peak_since_tap()
    peak_txt = f"{peak:.2f}x" if peak else "n/a"
    if entry_px is None:
        await tg_send(s, f"📊 <b>{sess.name}</b> — no trade taken. "
                         f"It ran <b>{peak_txt}</b> from your tap.")
        return
    pnl = received - spent
    emoji = "✅" if pnl > 0 else "🔴"
    tag = "(dry)" if C.DRY_RUN else ""
    await tg_send(s, f"{emoji} <b>{sess.name}</b> done {tag} · "
                     f"{pnl:+.4f} SOL · peak after tap {peak_txt}")


# ── main ────────────────────────────────────────────────────────────────────
async def main():
    mode = "DRY RUN — no real money" if C.DRY_RUN else "🔴 LIVE — REAL MONEY"
    print(f"edgelvl bot up · {mode} · {C.SIZE_SOL} SOL/trade · poll {C.POLL_SEC}s")

    async with aiohttp.ClientSession() as s:
        await tg_send(s, f"⚡ <b>Bot online</b> — {mode}\n"
                         f"{C.SIZE_SOL} SOL per trade · TP {C.TPS[0]}x/{C.TPS[1]}x")

        # ── did we come back holding something? ─────────────────────────────
        # If the bot died mid-trade, those positions are still in your wallet
        # with no take-profit and no stop. Check the wallet, tell the user, and
        # never pretend a bag doesn't exist just because we forgot about it.
        if open_positions and not C.DRY_RUN:
            notes = await S.reconcile(s, open_positions, J.token_balance)
            for n in notes:
                await tg_send(s, f"🔄 {n}")
            S.save(open_positions, seen_signals)

        if open_positions:
            lines = "\n".join(
                f"• <b>{p.get('name', m[:8])}</b> — {int(p.get('tokens_raw', 0)):,} tokens"
                for m, p in open_positions.items())
            await tg_send(
                s,
                f"⚠️ <b>You still hold {len(open_positions)} position(s)</b> from before the restart:\n"
                f"{lines}\n\n"
                f"The bot is <b>not</b> managing these — no take-profit, no stop. "
                f"Sell them yourself, or greenlight the coin again to hand it back to the bot.")

        asyncio.create_task(offer_signals(s))
        asyncio.create_task(poll_web_greenlights(s))
        offset = 0
        while True:
            offset = await tg_poll_taps(s, offset)
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
