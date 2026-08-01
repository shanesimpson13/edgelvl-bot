"""
bot.py — the whole system, running.

    5m trending in  →  you GREENLIGHT  →  bot works the entry  →  bot takes profit

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
START_TS = time.time()

live_sessions = {}     # mint -> Session (coins currently being worked)
pending_tap = {}       # mint -> signal dict (offered, awaiting your call)
cancel_mints = set()   # coins you've asked the bot to stop watching

# Persisted across restarts so a crash can't lose a bag you're holding.
open_positions, seen_signals, armed_mints = S.load()   # seen_ kept for state compat


# ── telegram ────────────────────────────────────────────────────────────────
async def tg_send(s, text, buttons=None):
    payload = {"chat_id": C.TG_CHAT, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    try:
        async with s.post(f"{TG}/sendMessage", json=payload) as r:
            d = await r.json()
        # Telegram answers 200 with ok:false for throttling and bad markup.
        # Swallowing that means a message you needed just never arrives.
        if not d.get("ok"):
            print(f"TG SEND FAILED: {d.get('error_code')} {d.get('description')} "
                  f"| text was: {text[:80]}", flush=True)
        return d
    except Exception as e:
        print(f"tg error: {e}", flush=True)
        return {}


async def tg_edit(s, chat_id, message_id, text, buttons=None):
    """Edit a message in place — lets the board and a coin share one message
    instead of burying you in new ones."""
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    try:
        async with s.post(f"{TG}/editMessageText", json=payload) as r:
            return await r.json()
    except Exception as e:
        print(f"tg edit error: {e}")
        return {}


async def tg_poll_taps(s, offset):
    """Watch for your commands and button taps."""
    try:
        async with s.get(f"{TG}/getUpdates",
                         params={"offset": offset, "timeout": 5},
                         timeout=aiohttp.ClientTimeout(total=15)) as r:
            data = await r.json()
    except Exception:
        return offset

    for upd in data.get("result", []):
        offset = upd["update_id"] + 1

        # ── typed commands ──────────────────────────────────────────────
        msg = upd.get("message")
        if msg:
            if str(msg.get("from", {}).get("id")) != str(C.TG_CHAT):
                continue
            text = (msg.get("text") or "").strip().lower().split("@")[0]
            if text in ("/top", "/board", "/start"):
                await show_board(s)
            elif text == "/help":
                await tg_send(s, HELP_TEXT)
            elif text == "/positions":
                await show_positions(s)
            elif text in ("/unarm", "/watching"):
                await show_watching(s)
            continue

        cb = upd.get("callback_query")
        if not cb:
            continue

        # only YOU can arm trades
        if str(cb["from"]["id"]) != str(C.TG_CHAT):
            continue

        data_str = cb.get("data", "")
        chat_id = cb["message"]["chat"]["id"]
        msg_id = cb["message"]["message_id"]

        async with s.post(f"{TG}/answerCallbackQuery",
                          json={"callback_query_id": cb["id"]}):
            pass

        if data_str.startswith("go:"):
            await arm_mint(s, data_str[3:])
        elif data_str.startswith("coin:"):
            await show_coin(s, data_str[5:], chat_id, msg_id)
        elif data_str.startswith("unarm:"):
            await unarm(s, data_str[6:])
            await show_watching(s, chat_id, msg_id)
        elif data_str == "board":
            await show_board(s, chat_id, msg_id)
    return offset


HELP_TEXT = (
    "<b>Commands</b>\n\n"
    "/top — the coins worth a look right now\n"
    "/positions — what the bot is holding\n"
    "/unarm — stop watching a coin\n"
    "/help — this\n\n"
    "Tap a coin on the board to see it, then <b>GREENLIGHT</b> to hand it to the bot."
)


async def fetch_board(s, limit=30, min_vol=None):
    """Solana's 5m-trending coins, ranked by real trailing 5m volume."""
    try:
        async with s.get(f"{C.EDGE_API}/api/trending",
                         params={"limit": limit,
                                 "min_vol": C.MIN_VOL_5M if min_vol is None else min_vol},
                         headers={"Authorization": f"Bearer {C.EDGE_API_KEY}"},
                         timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                return []
            return (await r.json()).get("coins", [])
    except Exception as e:
        print(f"board error: {e}")
        return []


def _live_coins(coins, top=5):
    """Already ranked by 5m volume server-side — just take the head."""
    return coins[:top]


def _age(secs):
    if secs is None:
        return "?"
    if secs < 3600:
        return f"{secs/60:.0f}m"
    if secs < 86400:
        return f"{secs/3600:.0f}h"
    return f"{secs/86400:.0f}d"


async def show_board(s, chat_id=None, msg_id=None):
    """The whole point: a handful of coins, not a scroll of cards."""
    coins = _live_coins(await fetch_board(s))

    if not coins:
        text = ("<b>Nothing trending right now.</b>\n\n"
                "Nothing is clearing the volume floor. That's a normal state — "
                "it means there's nothing worth forcing.")
        buttons = [[{"text": "↻ Refresh", "callback_data": "board"}]]
    else:
        lines = ["<b>Trending · 5m volume</b>\n"]
        buttons = []
        for c in coins:
            sym = c.get("symbol") or c.get("mint", "")[:8]
            volr = c.get("volr")
            # volR under 1 means more was sold than bought over the window.
            dot = "🟢" if (volr is None or volr >= 1) else "🔴"
            vr = f"volR {volr:.2f}" if volr is not None else "volR —"
            pc5 = c.get("pc5")
            mv = f"{pc5:+.0f}%" if isinstance(pc5, (int, float)) else "—"
            lines.append(
                f"{dot} <b>{sym}</b> — {_money(c.get('vol_5m'))} <i>5m vol</i>\n"
                f"     MC {_money(c.get('mcap'))} · {_age(c.get('age_secs'))} old · "
                f"5m {mv} · {vr}")
            buttons.append([{"text": f"{sym}  ·  {_money(c.get('vol_5m'))} vol",
                             "callback_data": f"coin:{c['mint']}"}])
        text = "\n".join(lines)
        buttons.append([{"text": "↻ Refresh", "callback_data": "board"}])

    if msg_id:
        await tg_edit(s, chat_id, msg_id, text, buttons)
    else:
        await tg_send(s, text, buttons)


def trending_card(c):
    """A coin off the trending board. Every number here is a trailing-5m figure."""
    sym = c.get("symbol") or c.get("mint", "")[:8]
    name = c.get("name") or ""
    volr = c.get("volr")
    pc5, pc1h = c.get("pc5"), c.get("pc1h")

    lines = [f"🔥 <b>{sym}</b>" + (f"  ·  {name}" if name and name != sym else ""), ""]
    lines.append(f"<b>{_money(c.get('vol_5m'))}</b> 5m volume")
    lines.append(f"MC {_money(c.get('mcap'))}  ·  ATH {_money(c.get('ath_mcap'))}")

    mom = []
    if isinstance(pc5, (int, float)):
        mom.append(f"5m {pc5:+.0f}%")
    if isinstance(pc1h, (int, float)):
        mom.append(f"1h {pc1h:+.0f}%")
    if mom:
        lines.append("  ·  ".join(mom))

    lines.append(f"Liq {_money(c.get('liq'))}  ·  {int(c.get('holders') or 0):,} holders"
                 f"  ·  {_age(c.get('age_secs'))} old")
    lines.append(f"{int(c.get('swaps_5m') or 0):,} trades  "
                 f"({int(c.get('buys_5m') or 0):,} buy / {int(c.get('sells_5m') or 0):,} sell)")

    if isinstance(volr, (int, float)):
        # Buy volume vs sell volume over the same 5 minutes.
        flag = "  ⚠️ more sold than bought" if volr < C.VOLR_MIN else ""
        lines.append(f"volR <b>{volr:.2f}</b>{flag}")

    lines.append("")
    lines.append(f'<a href="https://gmgn.ai/sol/token/{c["mint"]}">Chart on GMGN</a>')
    return "\n".join(lines)


async def show_coin(s, mint, chat_id, msg_id):
    """One coin, in place, with the greenlight."""
    # The board is filtered by the volume floor; look wider before giving up,
    # since you may be opening a card from a few minutes ago.
    coin = await lookup_coin(s, mint)
    if coin is None:
        await tg_edit(s, chat_id, msg_id,
                      "Couldn't load that coin — it may have dropped off the board.",
                      [[{"text": "← Back", "callback_data": "board"}]])
        return

    pending_tap[mint] = coin
    await tg_edit(s, chat_id, msg_id, trending_card(coin), [
        [{"text": "🟢 GREENLIGHT", "callback_data": f"go:{mint}"}],
        [{"text": "← Back", "callback_data": "board"}],
    ])


async def show_watching(s, chat_id=None, msg_id=None):
    """Coins being watched, each with a button to drop it."""
    watching = [(m, sess) for m, sess in live_sessions.items() if sess.state != "POS"]
    if not watching:
        text = "Not watching anything right now."
        buttons = [[{"text": "📋 Board", "callback_data": "board"}]]
    else:
        lines = ["<b>Watching</b>  ·  tap to stop\n"]
        buttons = []
        for m, sess in watching:
            pk = sess.peak_since_tap()
            lines.append(f"👀 <b>{sess.name}</b>"
                         + (f" — {pk:.2f}x since you armed it" if pk else ""))
            buttons.append([{"text": f"🛑 Unarm {sess.name}", "callback_data": f"unarm:{m}"}])
        text = "\n".join(lines)
        buttons.append([{"text": "📋 Board", "callback_data": "board"}])

    if msg_id:
        await tg_edit(s, chat_id, msg_id, text, buttons)
    else:
        await tg_send(s, text, buttons)


async def show_positions(s):
    if not open_positions:
        await tg_send(s, "Not holding anything.")
        return
    lines = ["<b>Open positions</b>\n"]
    for m, p in open_positions.items():
        lines.append(f"• <b>{p.get('name', m[:8])}</b> — {int(p.get('tokens_raw', 0)):,} tokens")
    await tg_send(s, "\n".join(lines))


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


async def report_status(s, mint, name, state, mc, pnl_frac, mult=None, open_pct=None, band=None):
    """Tell the terminal what the bot just did.

    One-way and best-effort: the bot owns positions and P&L, the terminal only
    mirrors them. A failure here must never affect a trade, so it's swallowed.
    """
    try:
        await s.post(f"{C.EDGE_API}/api/status",
                     json={"mint": mint, "name": name, "state": state,
                           "mc": mc, "pnl": pnl_frac, "dry_run": C.DRY_RUN,
                           "mult": mult, "open_pct": open_pct, "band": band},
                     headers={"Authorization": f"Bearer {C.EDGE_API_KEY}"},
                     timeout=aiohttp.ClientTimeout(total=10))
    except Exception:
        pass


async def poll_web_unarms(s):
    """Coins you dropped in the terminal. Same effect as /unarm in Telegram."""
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
        await tg_send(s, f"⚠️ <b>{sess.name}</b> is already bought — not dropping it.\n"
                         f"The bot keeps managing the exit. Sell it yourself if you want out now.")
        return False

    cancel_mints.add(mint)
    await tg_send(s, f"🛑 <b>{sess.name}</b> unarmed by {source} — no longer watching.")
    return True


async def arm_mint(s, mint):
    """Start working a coin. Both the Telegram tap and the web greenlight land here.

    Every outcome is logged. Failures used to go only to Telegram, which meant a
    greenlight that quietly didn't arm left no trace to diagnose afterwards.
    """
    """Start working a coin. Both the Telegram tap and the web terminal land here.

    Returns True if a session started. The card might be from an earlier run of
    the bot (you restarted, or you're scrolling back), so if we don't recognise
    the mint, go and fetch it rather than silently ignoring you.
    """
    print(f"ARM-REQ {mint[:12]}…", flush=True)

    if mint in live_sessions:
        print(f"ARM-SKIP {mint[:12]}… already working it", flush=True)
        await tg_send(s, "Already working that one.")
        return False

    sig = pending_tap.pop(mint, None)
    if sig is None:
        sig = await lookup_coin(s, mint)
    if sig is None:
        print(f"ARM-FAIL {mint[:12]}… lookup returned nothing", flush=True)
        await tg_send(s, "Couldn't load that coin — it may have aged out "
                         "of the feed. Tap a more recent signal.")
        return False

    armed_mints.add(mint)
    S.save(open_positions, seen_signals, armed_mints)
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
            # Never let one bad coin kill this loop — an uncaught error here
            # takes the task down and every later greenlight silently does
            # nothing, which is the worst possible failure for this.
            try:
                armed = await arm_mint(s, mint)
            except Exception as e:
                print(f"arm error {mint[:8]}: {e}", flush=True)
                armed = False
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
        # Ultra: order -> sign -> execute, with on-chain confirmation inline.
        # When this returns a signature, the transaction has already landed.
        sig, got = await U.ultra_swap(s, U.SOL_MINT, mint,
                                      int(size_sol * 1e9), J.kp, C.JUP_API_KEY)
        if not sig or got <= 0:
            return 0, 0.0, 0.0, "swap_failed"
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
        await tg_send(s, f"🛑 <b>{name}</b> skipped — {blocked}")
        live_sessions.pop(mint, None)
        # This returns before the cleanup below, so drop it here too — otherwise
        # a blocked coin sits in the armed set and re-blocks on every restart.
        armed_mints.discard(mint)
        S.save(open_positions, seen_signals, armed_mints)
        return

    mode = "DRY RUN" if C.DRY_RUN else "🔴 LIVE"
    print(f"ARMED {name} ({mint[:12]}…)", flush=True)
    await report_status(s, mint, name, "watching", None, None)
    await tg_send(
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
                await tg_send(s, f"⏭️ <b>{name}</b> — no entry in {int(entry_timeout/60)}min, standing down.")
                await report_status(s, mint, name, "stood down", None, None)
                break
            if sess.state == "POS" and elapsed > max_hold:
                got, err = await dry_or_live_sell(s, mint, tokens_held)
                if not err:
                    received += got
                    tokens_held = 0
                await tg_send(s, f"⌛ <b>{name}</b> — max hold reached, closed out.")
                break

            price = await J.get_price(s, mint)

            # A dead price feed is indistinguishable from a quiet coin: the
            # strategy just never sees a dip and waits out the clock. Say so
            # rather than looking busy while receiving nothing.
            if price is None:
                dead_polls += 1
                if dead_polls == 30:
                    await tg_send(
                        s, f"⚠️ <b>{name}</b> — no price coming back from Jupiter.\n"
                           f"Nothing can trigger without prices. Usually rate limiting: "
                           f"set <code>JUP_API_KEY</code> (free at portal.jup.ag) or watch "
                           f"fewer coins at once.")
                    print(f"NO PRICE {name}: 30 consecutive failed quotes", flush=True)
            elif dead_polls:
                if dead_polls >= 30:
                    await tg_send(s, f"✅ <b>{name}</b> — price feed recovered.")
                dead_polls = 0

            if mc_factor is None and price and arm_mcap:
                mc_factor = arm_mcap / price      # first quote pairs with the board's mcap

            # Publish the live entry band: the dip it needs, and the reclaim
            # that would fire. Only while waiting, and only every 15s — this is
            # our own API, but there's no reason to chatter.
            if (sess.state == "WAIT" and price and mc_factor
                    and time.time() - last_band > 15):
                last_band = time.time()
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
                        await tg_send(s, f"❌ <b>{name}</b> buy failed — {err}. No money spent.")
                        break
                    tokens_held, spent, entry_px = got, cost, fill_px
                    tokens_original = got        # the ladder is fractions of THIS
                    sess.on_filled(fill_px)
                    open_positions[mint] = {"name": name, "tokens_raw": tokens_held,
                                            "entry_px": fill_px, "spent_sol": spent,
                                            "opened": time.time()}
                    S.save(open_positions, seen_signals, armed_mints)
                    tag = " (dry)" if C.DRY_RUN else ""
                    await tg_send(s, f"🎯 <b>{name}</b> BUY{tag} · {size_sol} SOL\n"
                                     f"Entry at <b>{MC(fill_px)}</b> MC\n"
                                     f"TP {MC(fill_px*sess.tps[0])} / {MC(fill_px*sess.tps[1])} · "
                                     f"stop -{int(sess.kill*100)}% off peak")
                    await report_status(s, mint, name, "bought", MC(fill_px), None)

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
                        await tg_send(s, f"⚠️ <b>{name}</b> {label} sell failed — {err}. Still holding.")
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
                        await tg_send(
                            s, f"💰 <b>{name}</b> TP{rung}{tag} · sold {sold_pct}% at "
                               f"<b>{MC(price)}</b> MC ({mult}x) for {got:.4f} SOL"
                               + (f"\n{open_pct}% still riding to {sess.tps[rung]}x." if not is_last else ""))
                        await report_status(s, mint, name, f"tp{rung}", MC(price),
                                            (received - spent) / max(spent, 1e-9),
                                            mult=mult, open_pct=open_pct)
                    if tokens_held <= 0 or is_last:
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
                        await tg_send(s, f"🔴 <b>{name}</b> stopped out{tag} at <b>{MC(price)}</b> MC "
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
        await tg_send(s, f"💥 <b>{name}</b> error: {e}")
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
        # Puts /top in Telegram's own command menu, so it's discoverable
        # without reading any documentation.
        try:
            await s.post(f"{TG}/setMyCommands", json={"commands": [
                {"command": "top", "description": "Coins worth a look right now"},
                {"command": "positions", "description": "What the bot is holding"},
                {"command": "unarm", "description": "Stop watching a coin"},
                {"command": "help", "description": "How this works"},
            ]})
        except Exception:
            pass

        await tg_send(s, f"⚡ <b>Bot online</b> — {mode}\n"
                         f"{C.SIZE_SOL} SOL per trade · TP {C.TPS[0]}x/{C.TPS[1]}x\n\n"
                         f"Send /top when you're ready to trade.",
                      [[{"text": "📋 Show the board", "callback_data": "board"}]])

        # ── did we come back holding something? ─────────────────────────────
        # If the bot died mid-trade, those positions are still in your wallet
        # with no take-profit and no stop. Check the wallet, tell the user, and
        # never pretend a bag doesn't exist just because we forgot about it.
        if open_positions and not C.DRY_RUN:
            notes = await S.reconcile(s, open_positions, J.token_balance)
            for n in notes:
                await tg_send(s, f"🔄 {n}")
            S.save(open_positions, seen_signals, armed_mints)

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
                await tg_send(s, f"🔄 <b>Picked back up after the restart:</b> "
                                 f"{', '.join(recovered)}")

        asyncio.create_task(poll_web_greenlights(s))
        asyncio.create_task(poll_web_unarms(s))
        offset = 0
        while True:
            offset = await tg_poll_taps(s, offset)
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
