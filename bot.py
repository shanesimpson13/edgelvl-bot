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
from strategy import Session

TG = f"https://api.telegram.org/bot{C.TG_TOKEN}"
LOG = "trades.jsonl"

live_sessions = {}     # mint -> Session (coins currently being worked)
seen_signals = set()   # mints we've already offered you
pending_tap = {}       # mint -> signal dict (offered, awaiting your call)


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
            sig = pending_tap.pop(mint, None)
            if sig and mint not in live_sessions:
                asyncio.create_task(work_coin(s, sig))
    return offset


# ── signal feed ─────────────────────────────────────────────────────────────
async def fetch_signals(s):
    """New signals from your API key."""
    try:
        async with s.get(f"{C.EDGE_API}/api/feed?limit=25",
                         headers={"Authorization": f"Bearer {C.EDGE_API_KEY}"},
                         timeout=aiohttp.ClientTimeout(total=15)) as r:
            data = await r.json()
    except Exception as e:
        print(f"feed error: {e}")
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
                seen_signals.add(mint)
                pending_tap[mint] = sig

                name = sig.get("name", mint[:8])
                mc = sig.get("alert_mc") or 0
                volr = sig.get("volr")
                vtxt = f"volR {volr:.2f}" if isinstance(volr, (int, float)) else "volR n/a"
                flag = ""
                if C.USE_VOLR and isinstance(volr, (int, float)) and volr < C.VOLR_MIN:
                    flag = "\n⚠️ <b>below volR floor</b> — historically 0/13"

                await tg_send(
                    s,
                    f"🔔 <b>{name}</b>\n${mc/1000:,.0f}K MC · {vtxt}{flag}\n<code>{mint}</code>",
                    [[{"text": "🟢 GREENLIGHT", "callback_data": f"go:{mint}"}]])
        except Exception as e:
            print(f"offer error: {e}")
        await asyncio.sleep(C.FEED_POLL_SEC)


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
    await tg_send(s, f"👀 <b>{name}</b> armed ({mode}) — watching for the reclaim entry.")

    t0 = time.time()
    tokens_held = 0
    entry_px = None
    spent = 0.0
    received = 0.0

    try:
        while True:
            # time limits
            elapsed = time.time() - t0
            if sess.state == "WAIT" and elapsed > C.ENTRY_TIMEOUT:
                await tg_send(s, f"⏭️ <b>{name}</b> — no entry in {int(C.ENTRY_TIMEOUT/60)}min, standing down.")
                break
            if sess.state == "POS" and elapsed > C.MAX_HOLD:
                sig_, err = (None, None) if C.DRY_RUN else await J.sell(s, mint, tokens_held)
                await tg_send(s, f"⌛ <b>{name}</b> — max hold reached, closing out.")
                break

            price = await J.get_price(s, mint)
            action = sess.feed(price)

            if action is None:
                await asyncio.sleep(C.POLL_SEC)
                continue

            # ── BUY ──
            if action[0] == "BUY":
                if C.DRY_RUN:
                    entry_px = price
                    tokens_held = int(C.SIZE_SOL / price * 1e6)
                    spent = C.SIZE_SOL
                    sess.on_filled(price)
                    await tg_send(s, f"🎯 <b>{name}</b> BUY (dry) · {C.SIZE_SOL} SOL @ {price:.3e}")
                else:
                    tx, got, err = await J.buy(s, mint, C.SIZE_SOL)
                    if err:
                        await tg_send(s, f"❌ <b>{name}</b> buy failed — {err}. No money spent.")
                        break
                    tokens_held = got
                    entry_px = price
                    spent = C.SIZE_SOL
                    sess.on_filled(price)
                    await tg_send(s, f"🎯 <b>{name}</b> BUY · {C.SIZE_SOL} SOL @ {price:.3e}\n"
                                     f"TP {C.TPS[0]}x/{C.TPS[1]}x · stop -{int(C.KILL*100)}% off peak")

            # ── TAKE PROFIT (a rung of the ladder) ──
            elif action[0] == "SELL":
                frac = action[1]
                label = action[2]
                amount = int(tokens_held * frac)
                if C.DRY_RUN:
                    received += amount / 1e6 * price
                    tokens_held -= amount
                    await tg_send(s, f"💰 <b>{name}</b> {label} (dry) · sold {int(frac*100)}%")
                else:
                    tx, err = await J.sell(s, mint, amount)
                    if err:
                        await tg_send(s, f"⚠️ <b>{name}</b> {label} sell failed — {err}. Still holding.")
                    else:
                        tokens_held = await J.token_balance(s, mint)
                        await tg_send(s, f"💰 <b>{name}</b> {label} · sold {int(frac*100)}% @ {price:.3e}")
                if tokens_held <= 0 or sess.tp_done >= len(C.TPS):
                    sess.on_closed()
                    break

            # ── STOP ──
            elif action[0] == "SELLALL":
                if C.DRY_RUN:
                    received += tokens_held / 1e6 * price
                    tokens_held = 0
                    await tg_send(s, f"🔴 <b>{name}</b> stop (dry) @ {price:.3e}")
                else:
                    tx, err = await J.sell(s, mint, tokens_held)
                    if err:
                        await tg_send(s, f"🚨 <b>{name}</b> STOP SELL FAILED — {err}. "
                                         f"<b>Check your wallet.</b>")
                    else:
                        await tg_send(s, f"🔴 <b>{name}</b> stopped out @ {price:.3e}")
                sess.on_closed()
                break

            await asyncio.sleep(C.POLL_SEC)

    except Exception as e:
        await tg_send(s, f"💥 <b>{name}</b> error: {e}")
    finally:
        live_sessions.pop(mint, None)
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
        asyncio.create_task(offer_signals(s))
        offset = 0
        while True:
            offset = await tg_poll_taps(s, offset)
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
