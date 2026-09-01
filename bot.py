"""
bot.py — the whole system, running.

    5m trending in  →  you GREENLIGHT  →  bot works the entry  →  bot takes profit

Nothing trades without your tap. Run it with DRY_RUN=1 until you've seen it work.

    python bot.py
"""
import asyncio
import json
import re
import os
import time
import statistics
from datetime import datetime, timezone

import aiohttp

import config as C
import gas as GAS
import jupiter as J
import ultra as U
import state as S
import copywatch as CW
from strategy import Session

LOG = "trades.jsonl"
START_TS = time.time()

# Keyed by (user, mint). `user` is None for the wallet configured in the
# environment, and _key() then collapses to the bare mint — so the existing
# state file loads unchanged and a single-user deployment behaves exactly as
# it did. Only a multi-user run produces composite keys.
live_sessions = {}     # key(user, mint) -> Session


def _key(user, mint):
    """State key. Bare mint for the environment's own wallet, composite
    otherwise — JSON has no tuple keys, and the state file has to survive."""
    return mint if user is None else f"{user}|{mint}"


def _users_enabled():
    """Multi-user only when explicitly switched on, so this cannot start
    trading other people's wallets because a config file drifted."""
    return bool(C.MULTI_USER and C.BOT_ADMIN_KEY)
pending_tap = {}       # mint -> signal dict (offered, awaiting your call)
cancel_mints = set()   # coins you've asked the bot to stop watching

# A cancel SELLS first and queues the drop straight after, so the tokens can
# still be in the wallet when the drop is read: the swap has been sent, not
# settled. These bound how long we keep looking before believing it.
_DROP_RECHECKS   = 4      # extra balance reads inside one drop attempt
_DROP_RECHECK_SEC = 2.0   # spacing between them
_DROP_GIVE_UP    = 6      # polls to keep asking before saying so out loud
_drop_tries = {}          # (user, mint) -> attempts so far


def _looks_empty(err):
    """A sell that failed because there is nothing to sell."""
    e = str(err or "").lower()
    return "insufficient funds" in e or "insufficient balance" in e

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




def _as_user(user, body=None):
    """Headers (and body) for a write that belongs to `user`.

    The credential is always the bot's own; naming a user is what redirects the
    write to their account, so their settings drive their trade and the result
    lands on their terminal instead of ours. Unnamed means our own account,
    which is every write in the single-user default.
    """
    hdrs = {"Authorization": f"Bearer {C.BOT_ADMIN_KEY}"}
    if user is not None and _users_enabled() and body is not None:
        body = {**body, "user": user}
    return hdrs, body


async def fetch_settings(s, user=None, preset=None):
    """Your settings from edgelvl.app. Falls back to the shipped defaults.

    Read when a session STARTS, never mid-trade — see the note in strategy.py.

    `preset` names a saved strategy, chosen on the coin page for this coin. The
    server layers it over the account's own settings, so a coin greenlit under
    one strategy keeps those numbers even if the account's are edited a minute
    later — which is the behaviour that already existed, now made deliberate
    and visible rather than an accident of when you happened to press the button.
    """
    try:
        hdrs, _ = _as_user(user)
        params = {"user": user} if (user is not None and _users_enabled()) else None
        # "" is a real answer -- the account's own settings, named on the coin
        # page -- so it has to reach the server. Only None means "nothing was
        # chosen", which is what lets the default strategy apply.
        if preset is not None:
            params = dict(params or {})
            params["preset"] = preset
        async with s.get(f"{C.EDGE_API}/api/settings", headers=hdrs, params=params,
                         timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                return {}
            return (await r.json()).get("settings") or {}
    except Exception as e:
        print(f"settings error: {e}", flush=True)
        return {}


def stop_px_of(sess):
    """The price the stop will ACTUALLY fire at, right now.

    The same expression `Session._pos` uses to decide, rather than a second
    one written next to it. Three places published a stop computed from the
    PEAK regardless of the setting, so an account on a fixed stop was shown
    "50% off the peak" while the bot used 50% off entry. On UNDEADS that read
    $236K against a real stop of $176.5K, and price sitting under the screen
    number for minutes was correct behaviour against a number that was never
    the stop.
    """
    if sess.entry is None:
        return None
    ref = sess.ppeak if sess.trail_stop else sess.entry
    if not ref:
        return None
    floor = ref * (1 - sess.kill)
    if sess.be_after_tp1 and sess.tp_done:
        floor = max(floor, sess.entry)
    return floor


def _band(sess, MC):
    """The market caps that would actually fire a buy.

    Two different questions, depending on whether a dip is on record.

    Before one: the only line that matters is the dip trigger. Price has to
    trade at or below hi*(1-DIP) before a low starts being recorded at all.

    After one: that line stops mattering entirely. The reclaim is measured off
    the LOW, not off the threshold — a coin that fell 45% buys 5% off its own
    bottom, not 20% off the high. This used to quote the trigger anyway, which
    told you a coin needed a 1.38x when it needed a 1.05x. That is the
    difference between a setup about to fire and one that looks hopeless, and
    it is the reason the panel is worth having at all.

    What is left is the reclaim, held up from underneath by the deadcat guard,
    which refuses anything more than DEADCAT below the peak no matter how good
    the bounce looks:

        max(low*BOUNCE, peak*(1-DEADCAT))  <=  price  <  peak*PICO
    """
    if not sess.hi or not sess.peak:
        return None
    if sess.low:
        # Both bounds are real gates. Quoting only the reclaim would promise a
        # buy that the deadcat check then refuses, which is the same class of
        # lie in the other direction.
        floor = max(sess.low * sess.bounce, sess.peak * (1 - sess.deadcat))
    else:
        floor = sess.hi * (1 - sess.dip)
    ceiling = sess.peak * sess.pico
    return {
        "hi": MC(sess.hi),
        # The reclaim is measured off this, so without it on screen the
        # distance to the window looks arbitrary.
        "low": MC(sess.low) if sess.low else None,
        "buy_from": MC(floor),
        "buy_to": MC(ceiling),
        "reachable": floor < ceiling,
        "dipped": sess.low is not None,
        "warmup_left": max(0, sess.warmup - sess.n),
        # What is refusing the buy right now, if anything. Only meaningful once
        # a dip has been recorded — before that the coin simply hasn't set up.
        "holding_for": sess.hold_reason,
    }



async def report_status(s, user, mint, name, state, mc, pnl_frac, mult=None,
                        open_pct=None, band=None, targets=None):
    """Tell the terminal what the bot just did.

    One-way and best-effort: the bot owns positions and P&L, the terminal only
    mirrors them. A failure here must never affect a trade, so it's swallowed.
    """
    try:
        hdrs, body = _as_user(user, {
            "mint": mint, "name": name, "state": state,
            "mc": mc, "pnl": pnl_frac, "dry_run": C.DRY_RUN,
            "mult": mult, "open_pct": open_pct, "band": band,
            "targets": targets})
        await s.post(f"{C.EDGE_API}/api/status", json=body, headers=hdrs,
                     timeout=aiohttp.ClientTimeout(total=10))
    except Exception:
        pass


async def poll_web_unarms(s):
    """Coins you dropped in the terminal."""
    hdrs = {"Authorization": f"Bearer {C.BOT_ADMIN_KEY}"}
    while True:
        pending = []            # (user, mint), user None for our own wallet
        try:
            if _users_enabled():
                bot_hdrs = {"Authorization": f"Bearer {C.BOT_ADMIN_KEY}"}
                async with s.get(f"{C.EDGE_API}/api/unarms/all", headers=bot_hdrs,
                                 timeout=aiohttp.ClientTimeout(total=15)) as r:
                    users = (await r.json()).get("users", []) if r.status == 200 else []
                for u in users:
                    for m in u.get("mints", []):
                        pending.append((u["identity"], m))
            else:
                async with s.get(f"{C.EDGE_API}/api/unarms", headers=hdrs,
                                 timeout=aiohttp.ClientTimeout(total=15)) as r:
                    mints = (await r.json()).get("mints", []) if r.status == 200 else []
                pending = [(None, m) for m in mints]
        except Exception:
            pending = []

        for user, mint in pending:
            key = (user, mint)
            tries = _drop_tries.get(key, 0) + 1
            last = tries >= _DROP_GIVE_UP
            took = False
            try:
                took = await unarm(s, mint, source="the terminal", user=user,
                                   final=last)
            except Exception as e:
                print(f"unarm error {mint[:8]}: {e}", flush=True)
            # Cleared only once it TOOK. This acked either way, so a drop
            # refused because the sale had not settled yet was thrown away
            # instead of retried — one early read and the cancel was gone for
            # good, with the session still managing an exit for a bag that had
            # already left and the sweep that would have caught it skipping
            # any coin that still has a session. Left queued, we ask again.
            if not took and not last:
                _drop_tries[key] = tries
                continue
            _drop_tries.pop(key, None)
            try:
                await _ack(s, "unarms", mint, user)
            except Exception:
                pass
        await asyncio.sleep(C.GREENLIGHT_POLL_SEC)


async def unarm(s, mint, source="you", user=None, final=False):
    """Stop watching a coin.

    Only ever cancels a coin we have NOT bought. If there's a position open,
    refuse — dropping the session would leave a bag in the wallet with no
    take-profit and no stop, which is the worst state this bot can be in.
    """
    k = _key(user, mint)
    sess = live_sessions.get(k)
    if sess is None:
        # No session — usually because a restart dropped it. Clearing our own
        # state silently left the terminal showing "watching" forever, which is
        # exactly what a dead Unarm button looks like. Say so.
        # Read before the pop: the arm is the only place the name still lives
        # once the session is gone.
        known = ((open_positions.get(k) or {}).get("name")
                 or (armed_mints.get(k) or {}).get("name") or "")
        armed_mints.pop(k, None)
        S.save(open_positions, seen_signals, armed_mints)
        await report_status(s, user, mint, known, "unarmed", None, None)
        return False

    sold_out = False
    if sess.state == "POS" or k in open_positions:
        # The WALLET decides, not our own record of it.
        #
        # This refused outright, which is correct only while the tokens are
        # actually there. Selling from the profile tab queues exactly this
        # drop — so the one action that should stop the bot was the one it
        # ignored, and it went on managing an exit for a bag that had already
        # left the wallet, holding a feed slot and a stop for nothing.
        held, w = None, None
        try:
            pos = open_positions.get(k) or {}
            wid = pos.get("wallet_id")
            w = (await J.privy.PrivyWallet.load(s, wid)
                 if wid and not C.DRY_RUN else None)
            held = await J.token_balance(s, mint, wallet=w)
        except Exception as e:
            print(f"DROP BALANCE CHECK {mint[:10]}…: {e}", flush=True)

        # One read decided this, taken seconds after the sale was sent — so a
        # cancel that was working looked like a coin you still held. Look
        # again for a few seconds before believing it.
        for _ in range(_DROP_RECHECKS):
            if held == 0:
                break
            await asyncio.sleep(_DROP_RECHECK_SEC)
            try:
                held = await J.token_balance(s, mint, wallet=w)
            except Exception as e:
                print(f"DROP RECHECK {mint[:10]}: {e}", flush=True)
                held = None

        if held is None or held > 0:
            # Still there. Silent until we are done asking: the caller keeps
            # the request queued, so this reaches you only if it stays true.
            if final and held is None:
                # Not the same as still holding it, and saying so would be a
                # guess dressed as a fact.
                await note(s, f"⚠️ <b>{sess.name}</b> — the wallet could not be read, "
                              f"so it is still being managed. Try again in a moment.")
            elif final:
                await note(s, f"⚠️ <b>{sess.name}</b> is still in your wallet — not "
                              f"dropping it.\nThe bot keeps managing the exit. Sell it "
                              f"first if you want out now.")
            return False
        # Empty. It has been sold elsewhere, so fall through: the session loop
        # reads the real proceeds off chain and journals them as such.
        sold_out = True

    cancel_mints.add(k)
    if not sold_out:
        await note(s, f"🛑 <b>{sess.name}</b> unarmed by {source} — no longer watching.")
    return True


def is_paused():
    """True while updates are in progress. Checked, not cached — the whole
    point is that flipping it takes effect immediately."""
    try:
        return os.path.exists(C.PAUSE_FILE)
    except Exception:
        return False


async def arm_mint(s, mint, user=None, wallet_id=None, fee_bps=None, opts=None):
    """Start working a coin. Every greenlight from the terminal lands here.

    Every outcome is logged, including the failures — a greenlight that quietly
    didn't arm used to leave no trace to diagnose afterwards.

    Returns True if a session started. The card might be from an earlier run of
    the bot (you restarted, or you're scrolling back), so if we don't recognise
    the mint, go and fetch it rather than silently ignoring you.
    """
    if is_paused():
        # Refused, and said out loud. Silently swallowing a greenlight during a
        # deploy would look exactly like the bot being broken.
        print(f"ARM-PAUSED {mint[:12]}… — updates in progress", flush=True)
        await note(s, "🔧 <b>Briefly paused for an update.</b> Nothing new is "
                      "being taken — anything already open is still managed. "
                      "Greenlight again in a minute.")
        return False

    who = "" if user is None else f" for {user[:18]}…"
    o = opts or {}
    extra = "".join([
        f" size={o['size_sol']}" if o.get("size_sol") is not None else "",
        f" preset={o['preset']}" if o.get("preset") else "",
    ])
    print(f"ARM-REQ {mint[:12]}…{who}{extra}", flush=True)

    # Two customers may work the same coin at once; that is not a conflict.
    # Only the SAME customer arming it twice is.
    if _key(user, mint) in live_sessions:
        print(f"ARM-SKIP {mint[:12]}…{who} already working it", flush=True)
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

    # The wallet to trade this with. None means the environment's own, which
    # is what a single-user run always uses.
    sig["user"] = user
    sig["wallet_id"] = wallet_id
    # Set by us on the account record, not by the customer. None = default rate.
    sig["fee_bps"] = fee_bps
    # Chosen on the coin page for THIS coin. Absent means "use my account
    # settings", which is what every greenlight did before presets existed.
    opts = opts or {}
    sig["size_sol"] = opts.get("size_sol")
    sig["preset"] = opts.get("preset")
    # Stored WITH everything chosen for THIS coin, not just its owner.
    #
    # It used to keep the wallet alone, so a restart re-armed the coin and then
    # rebuilt every other choice from the account defaults. DOGGYSTYLE was
    # armed at 0.1 SOL, restarted through, and bought 0.01 — a tenth of the
    # position, on a coin that went on to run. The size is the visible half;
    # `preset` is the worse one, because losing it silently swaps the STRATEGY
    # the coin is traded under.
    #
    # None is preserved as None, which means "use my account settings" — the
    # same thing it means at arm time. Entries written by an older build simply
    # have no key here and fall back exactly as they do today.
    armed_mints[_key(user, mint)] = {
        "user": user, "wallet_id": wallet_id,
        "size_sol": sig.get("size_sol"),
        "preset": sig.get("preset"),
        "fee_bps": fee_bps,
        # Carried so a drop can still say WHICH coin. Without it the status row
        # has no name and the board prints a raw mint prefix, which tells you
        # nothing about what you just dropped.
        "name": sig.get("name"),
    }
    S.save(open_positions, seen_signals, armed_mints)
    asyncio.create_task(work_coin(s, sig))
    return True


async def _ack(s, kind, mint, user):
    """Clear one queued mint.

    Acking as ourselves is right when we are the only account. Serving several,
    the queue we armed from belongs to someone else, so the ack has to name them
    — otherwise the coin is handed back on every poll and we re-arm it forever.
    """
    hdrs, body = _as_user(user, {"mint": mint})
    async with s.post(f"{C.EDGE_API}/api/{kind}/ack", json=body, headers=hdrs,
                      timeout=aiohttp.ClientTimeout(total=15)):
        pass


async def poll_web_greenlights(s):
    """Coins you greenlit in the terminal at edgelvl.app.

    The terminal never touches your wallet — it just queues the mint against your
    licence key. This is the bot picking that up and arming it.
    """
    hdrs = {"Authorization": f"Bearer {C.BOT_ADMIN_KEY}"}
    while True:
        # (user, mint, wallet_id) — user is None for our own configured wallet,
        # which keeps the single-user path byte-identical to what it was.
        pending = []
        try:
            if _users_enabled():
                bot_hdrs = {"Authorization": f"Bearer {C.BOT_ADMIN_KEY}"}
                async with s.get(f"{C.EDGE_API}/api/greenlights/all", headers=bot_hdrs,
                                 timeout=aiohttp.ClientTimeout(total=15)) as r:
                    users = (await r.json()).get("users", []) if r.status == 200 else []
                for u in users:
                    if u.get("skip") or not u.get("wallet_id"):
                        # No wallet means it cannot be traded. Say so once
                        # rather than dropping it silently, which is precisely
                        # how a second user's greenlight used to vanish.
                        print(f"greenlight for {str(u.get('identity'))[:18]}… "
                              f"skipped: {u.get('skip') or 'no wallet'}", flush=True)
                        continue
                    opts_by_mint = u.get("opts") or {}
                    for m in u.get("mints", []):
                        pending.append((u["identity"], m, u["wallet_id"],
                                        u.get("fee_bps"), opts_by_mint.get(m)))
            else:
                async with s.get(f"{C.EDGE_API}/api/greenlights", headers=hdrs,
                                 timeout=aiohttp.ClientTimeout(total=15)) as r:
                    mints = (await r.json()).get("mints", []) if r.status == 200 else []
                # The single-tenant endpoint has no per-coin options to give.
                pending = [(None, m, None, None, None) for m in mints]
        except Exception:
            pending = []

        for user, mint, wallet_id, fee_bps, opts in pending:
            # Never let one bad coin kill this loop — an uncaught error here
            # takes the task down and every later greenlight silently does
            # nothing, which is the worst possible failure for this.
            try:
                armed = await arm_mint(s, mint, user=user, wallet_id=wallet_id,
                                       fee_bps=fee_bps, opts=opts)
            except Exception as e:
                print(f"arm error {mint[:8]}: {e}", flush=True)
                armed = False
            if armed:
                await note(s, "🖥 Greenlit from the terminal.")
            # Ack either way — a mint we can't arm shouldn't be handed back forever.
            try:
                await _ack(s, "greenlights", mint, user)
            except Exception:
                pass

        await asyncio.sleep(C.GREENLIGHT_POLL_SEC)


# Matches the coin page's own poll. The endpoint is single-flighted with a 3s
# cache, so asking this often costs at most one upstream GMGN call per 3s per
# armed coin — and nothing at all while someone has that coin open, because
# their page keeps the entry warm. Reading the same number on the same clock as
# the page is what stops the two ever disagreeing.
MC_CALIB_SEC = 2.0

# Above this, the order moves the price enough to be worth saying out loud.
# 1% is roughly where a thin coin starts costing more than the swap fee.
IMPACT_WARN_PCT = 1.0


async def live_coin(s, mint):
    """This coin's market cap right now, from GMGN.

    Single-flighted and 3s-cached upstream, and the coin page polls it already,
    so calling it costs no extra GMGN request. Returns None rather than a stale
    number — a wrong anchor is worse than no anchor, because it silently scales
    every figure the session reports.
    """
    try:
        async with s.get(f"{C.EDGE_API}/api/coin/{mint}/live",
                         timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                return None
            d = await r.json()
    except Exception:
        return None
    src = d.get("coin") or d
    mc = src.get("market_cap") or src.get("mcap")
    try:
        mc = float(mc)
    except (TypeError, ValueError):
        mc = None
    # volr rides along in the same response — buy volume over sell volume, from
    # swaps as they land rather than from the board's ~45s rebuild.
    volr = src.get("volr")
    if not isinstance(volr, (int, float)):
        volr = None
    return {"mcap": mc if (mc and mc > 0) else None, "volr": volr}


async def lookup_coin(s, mint):
    """Find one coin by mint.

    Resolves against the last 30 minutes of the board, not just what's on it
    right now — a coin near the bottom can drop off between you greenlighting it
    and this call, and refusing a coin you were just looking at would be wrong.
    """
    try:
        async with s.get(f"{C.EDGE_API}/api/coin/{mint}",
                         headers={"Authorization": f"Bearer {C.BOT_ADMIN_KEY}"},
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


async def price_impact_pct(s, mint, size_sol):
    """How far this order moves the price, per Jupiter. None if unknown.

    Comes back on the quote we would make anyway, so it costs nothing.
    """
    try:
        q = await J.quote(s, J.WSOL, mint, int(size_sol * 1e9))
        v = q.get("priceImpactPct")
        return None if v is None else float(v) * 100
    except Exception:
        return None


def fee_opts(cfg):
    """Slippage and priority ceiling from YOUR settings, in the units Jupiter
    wants. Falls back to the shipped defaults for anything unset."""
    cfg = cfg or {}
    slip = cfg.get("slippage")
    prio = cfg.get("priority_fee")
    return {
        "slippage_bps": int(float(slip) * 10_000) if slip else None,
        "priority_lamps": int(float(prio) * 1e9) if prio else None,
    }


async def dry_or_live_buy(s, mint, size_sol=None, fees=None, wallet=None):
    """Returns (tokens_received, sol_spent, fill_price, error)."""
    size_sol = C.SIZE_SOL if size_sol is None else size_sol
    if not C.DRY_RUN:
        # No gas check: the sponsor pays the network fee and funds token-account
        # creation, so a customer holding 100% wrapped SOL can still trade. The
        # old top-up existed only because they could not.
        # Routed by wallet type: a local key goes through Jupiter Ultra, a Privy
        # wallet through the swap API so we control the build. Either way this
        # returns only once the swap has actually landed.
        # Both legs carry the fee now. On an ExactIn buy Jupiter takes it from
        # the INPUT mint, so it arrives as wSOL in the same account the sells
        # pay into — verified by on-chain simulation, not by the docs.
        got, err, gas = await J.execute_buy(s, mint, size_sol, wallet=wallet, **(fees or {}))
        if err or got <= 0:
            return 0, 0.0, 0.0, err or "swap_failed"
        # What the chain charged, not what we guessed it might. Falls back to the
        # estimate only when the transaction could not be read.
        cost = size_sol + (C.GAS_SOL if gas is None else gas)
        return got, cost, cost / (got / 1e6), None

    q = await J.quote(s, J.WSOL, mint, int(size_sol * 1e9))
    got = int(q.get("outAmount", 0) or 0)
    if got <= 0:
        return 0, 0.0, 0.0, "no_route"
    cost = size_sol + C.GAS_SOL            # gas is real money in dry run too
    return got, cost, cost / (got / 1e6), None


async def _sweep_wrapped(s, wallet, name):
    """Return anything Jupiter left wrapped to spendable SOL.

    Called after a sell because that is when it appears. Failure is logged and
    swallowed: the money is safe either way, it is simply in the wrong form,
    and this must never be able to affect the trade that just completed.
    """
    try:
        got, err = await J.unwrap_wsol(s, wallet=wallet)
        if err:
            print(f"UNWRAP {name}: {err}", flush=True)
        elif got:
            print(f"UNWRAP {name}: {got:.6f} SOL returned to spendable balance",
                  flush=True)
    except Exception as e:
        print(f"UNWRAP {name}: {type(e).__name__}", flush=True)


async def dry_or_live_sell(s, mint, raw_amount, fees=None, wallet=None):
    """Returns (sol_received_net_of_gas, error)."""
    if raw_amount <= 0:
        return 0.0, "nothing_to_sell"

    if not C.DRY_RUN:
        out, err, gas = await J.execute_sell(s, mint, raw_amount, wallet=wallet, **(fees or {}))
        if err:
            return 0.0, err
        return max(0.0, out - (C.GAS_SOL if gas is None else gas)), None

    q = await J.quote(s, mint, J.WSOL, int(raw_amount))
    out = int(q.get("outAmount", 0) or 0)
    if out <= 0:
        return 0.0, "no_route"
    return max(0.0, out / 1e9 - C.GAS_SOL), None


# ── working a single coin ───────────────────────────────────────────────────
async def work_coin(s, sig, resume=None):
    """From your tap until we're flat. One task per coin.

    `resume` re-enters an existing position after a restart: the strategy is
    seeded straight into POS with the entry, the trailing peak and the ladder
    progress it had, so the stop and take-profit continue from where they were
    rather than starting over.
    """
    mint = sig["mint"]
    name = sig.get("name", mint[:8])
    volr = sig.get("volr")
    # 5m transaction count -> swaps per second, which sets how long the strategy
    # watches before it's allowed to enter. Fast coins get a short warmup.
    swaps_5m = sig.get("swaps_5m")
    sps = (swaps_5m / 300.0) if isinstance(swaps_5m, (int, float)) and swaps_5m > 0 else None
    user = sig.get("user")
    wallet_id = sig.get("wallet_id")
    k = _key(user, mint)

    # Resolve the signer once, here, and hand it to every call below. A session
    # that reached for the module-level wallet would trade the wrong account.
    wallet = None
    if wallet_id and not C.DRY_RUN:
        try:
            wallet = await J.privy.PrivyWallet.load(s, wallet_id)
        except Exception as e:
            print(f"ARM-FAIL {name}: could not load wallet {wallet_id[:10]}…: {e}",
                  flush=True)
            armed_mints.pop(k, None)
            S.save(open_positions, seen_signals, armed_mints)
            return

    cfg = await fetch_settings(s, user, preset=sig.get("preset"))
    fees = fee_opts(cfg)          # your slippage + priority ceiling
    # The platform fee is OURS, so it comes off the signal, not off settings.
    # `is not None` because 0 means exempt and must not read as "unset".
    if sig.get("fee_bps") is not None:
        fees["fee_bps"] = sig["fee_bps"]
    sess = Session(mint, name,
                   volr=volr if isinstance(volr, (int, float)) else None,
                   swaps_per_sec=sps, cfg=cfg)

    # size and the clocks are the bot's, not the strategy's
    #
    # A per-coin amount beats the strategy's, because it was chosen for this
    # coin specifically and seconds ago. `is not None` rather than `or`: it is a
    # number, and a falsy one should fail validation upstream, not silently fall
    # back to a size the user didn't pick.
    size_sol = float(cfg.get("size_sol", C.SIZE_SOL))
    if sig.get("size_sol") is not None:
        size_sol = float(sig["size_sol"])
    entry_timeout = float(cfg.get("entry_timeout", C.ENTRY_TIMEOUT))
    max_hold = float(cfg.get("max_hold", C.MAX_HOLD))
    live_sessions[k] = sess

    blocked = sess.blocked_reason()
    if blocked:
        print(f"ARM-BLOCKED {name}: {blocked}", flush=True)
        await note(s, f"🛑 <b>{name}</b> skipped — {blocked}")
        live_sessions.pop(k, None)
        # This returns before the cleanup below, so drop it here too — otherwise
        # a blocked coin sits in the armed set and re-blocks on every restart.
        armed_mints.pop(k, None)
        S.save(open_positions, seen_signals, armed_mints)
        return

    mode = "DRY RUN" if C.DRY_RUN else "🔴 LIVE"
    if resume:
        # Already holding. Saying "it buys after a dip" here would describe a
        # session hunting an entry, which is the opposite of what this is.
        print(f"RESUMED {name} ({mint[:12]}…) holding "
              f"{int(resume.get('tokens_raw') or 0):,}", flush=True)
        await report_status(s, user, mint, name, "bought", None, None)
    else:
        print(f"ARMED {name} ({mint[:12]}…)", flush=True)
        await report_status(s, user, mint, name, "watching", None, None)
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

    # Re-entering a position we already hold. Seed the strategy into POS with
    # the trailing peak and ladder progress it had, so the stop continues from
    # the real high rather than resetting to the entry price.
    if resume:
        # Resumed positions hold real money exactly like fresh ones; without
        # this, a restart would silently drop them back into the slow lane.
        J.hold(mint)
        sess.state = "POS"
        sess.entry = float(resume.get("entry_px") or 0) or None
        sess.ppeak = float(resume.get("ppeak") or 0) or sess.entry
        sess.tp_done = int(resume.get("tp_done") or 0)
    last_band = 0.0

    # Market cap, never raw price — a price like 1.885e-07 tells you nothing.
    # The board's market cap and our first quote are from the same moment, so
    # their ratio converts any later price to a market cap. Self-calibrating:
    # no hardcoded token supply, no hardcoded SOL price to go stale.
    mc_factor = None
    mc_stale_since = None      # when the live anchor stopped answering
    last_calib = 0.0
    arm_mcap = sig.get("mcap") or 0
    # Recent calibrations, for the median below. Short on purpose: long enough
    # to reject a single bad quote, short enough to follow a real move.
    calib_hist = []
    factor_locked = False       # set at the fill; see _freeze_entry

    def MC(px):
        if not px:
            return "—"
        return _money(px * mc_factor) if mc_factor else f"{px:.3e}"

    def _lock_factor():
        """Stop recalibrating once we hold something.

        From here the entry, the rungs, the stop and the live cap all come from
        one number, so the percentage between any two of them is the real one.
        """
        nonlocal factor_locked
        factor_locked = True

    def _freeze_entry():
        """Fix the entry cap and the rungs, once, as soon as they can be real.

        Everything derived from a fill that already happened must stop moving —
        but it has to be right before it is frozen, and on a resumed position
        the calibration arrives a moment later than the position does.
        """
        nonlocal entry_mc, tp_mcs
        if entry_px and mc_factor and not tp_mcs:
            _lock_factor()
            # ONE number: the fill. What actually landed on chain, including
            # the impact of our own order.
            #
            # This briefly showed the market cap alongside it, on the reasoning
            # that the market number is the one you can find on the chart. That
            # was worse. Every other figure here — both rungs and the stop — is
            # measured off the fill, because a 1.5x has to be 1.5x on what you
            # actually paid. Leading with a number that nothing else is derived
            # from meant the entry and the take-profit no longer had the
            # relationship the setting claims.
            entry_mc = MC(entry_px)
            tp_mcs = [MC(entry_px * t) for t in sess.tps]
    tokens_held = 0
    # What the ladder actually did, in order. The terminal status only carried
    # the LAST thing that happened, so a trade that banked 50% at 2.12x and
    # then stopped the rest read as "Stopped out · +50%" — which looks like a
    # contradiction and buries the half that worked.
    rungs_hit = []
    entry_px = None            # the fill that landed on chain
    entry_mc = "—"
    tp_mcs = []
    if resume:
        tokens_held = int(resume.get("tokens_raw") or 0)
        entry_px = sess.entry
        # Deliberately NOT computed here: on resume the live anchor has not
        # been fetched yet, so MC() would fall back to a raw price and that
        # wrong value would then be frozen. _freeze_entry() below does it at
        # the first moment there is a calibration to do it with.
        tokens_original = int(resume.get("tokens_original") or tokens_held)
    spent = 0.0
    received = 0.0
    if resume:
        spent = float(resume.get("spent_sol") or 0)
        received = float(resume.get("received") or 0)
    stop_tries = 0      # failed stop-loss sells, widens slippage each retry
    tp_tries   = 0      # same, for a take-profit rung that reverts
    pending = None      # action queued last poll — fills on THIS one (1s latency, like reality)
    # How this session ended, if something specific already said so. The
    # cleanup below reports "stood down" otherwise, and without this it
    # overwrote every precise answer with the vaguest one.
    ended_as = None

    try:
        while True:
            # When this iteration is due to END, fixed before any work starts.
            #
            # The loop used to finish with sleep(POLL_SEC), which makes the
            # period POLL_SEC *plus* however long the work took — the quote,
            # the market-cap anchor, the status publish, the state write. With
            # two coins that measured 1.5-1.7s between prices on a feed
            # advertised as one second, and the stop is only ever checked when
            # a price lands: a dip that lasts a second can pass through the
            # gap without ever being seen. Cucumba traded at $24K against a
            # $27K stop and was not sold.
            #
            # Sleeping to a deadline instead makes the work fit INSIDE the
            # second rather than being added to it.
            due = time.monotonic() + C.POLL_SEC

            # you asked us to drop this one (only ever possible pre-entry)
            if k in cancel_mints:
                cancel_mints.discard(k)
                if sess.state == "POS":
                    # Dropped while holding: the tokens have been sold from the
                    # terminal. Read what they actually fetched rather than
                    # recording a stop that fired into an empty wallet.
                    try:
                        got, _ = await J.exit_proceeds(s, mint, since=int(t0))
                    except Exception as e:
                        print(f"EXIT LOOKUP FAILED {name}: {e}", flush=True)
                        got = None
                    if got is None:
                        got = 0.0
                        await note(s, f"⚠️ <b>{name}</b> sold from the terminal, "
                                      f"but the proceeds could not be read from "
                                      f"chain. The journal figure may be wrong.")
                    received += got
                    tokens_held = 0
                    ended_as = "sold elsewhere"
                    await note(s, f"🔻 <b>{name}</b> sold from the terminal for "
                                  f"{got:.4f} SOL · {(received - spent):+.4f} SOL")
                    await report_status(
                        s, user, mint, name, "sold elsewhere", None,
                        (received - spent) / spent if spent else None)
                else:
                    ended_as = "unarmed"
                    await report_status(s, user, mint, name, "unarmed", None, None)
                break

            # time limits
            elapsed = time.time() - t0
            if sess.state == "WAIT" and elapsed > entry_timeout:
                await note(s, f"⏭️ <b>{name}</b> — no entry in {int(entry_timeout/60)}min, standing down.")
                ended_as = "stood down"
                await report_status(s, user, mint, name, "stood down", None, None)
                break
            if sess.state == "POS" and elapsed > max_hold:
                got, err = await dry_or_live_sell(s, mint, tokens_held, fees, wallet=wallet)
                await _sweep_wrapped(s, wallet, name)
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

            # Anchor to a LIVE market cap, on the same clock as the coin page.
            #
            # This used to pair the first quote with sig["mcap"], which is the
            # BOARD row — served from the last 30 minutes of board snapshots.
            # Any staleness in that one number scales every market cap the
            # session prints, for the whole session: CHALK reported a $170.8K
            # entry when the fill was worth ~$137K.
            #
            # Re-calibrating rather than doing it once also stops the quote
            # feed and GMGN drifting apart over a long hold.
            if price and time.time() - last_calib > MC_CALIB_SEC:
                fresh = await live_coin(s, mint)
                # Still GMGN's, and rightly: buy/sell balance is theirs to
                # measure. The market cap is not — see below.
                if fresh and fresh.get("volr") is not None:
                    sess.volr = fresh["volr"]
                # supply x SOL/USD, both read directly. No ratio of two feeds,
                # so it does not wobble, and our figures stop depending on when
                # GMGN last refreshed. Frozen once in a position so entry, the
                # rungs and the live cap stay a consistent set.
                scale = await J.mc_scale(s, mint)
                if scale and not factor_locked:
                    mc_factor = scale
                if scale or fresh:
                    last_calib = time.time()
                    mc_stale_since = None
                else:
                    # The live anchor is gone. Say so ONCE, loudly. Silently
                    # riding a fixed factor is what made every market cap wrong
                    # for two and a half days while nothing looked broken.
                    if mc_stale_since is None:
                        mc_stale_since = time.time()
                        print(f"MC ANCHOR STALE {name}: live market cap "
                              f"unavailable — figures on screen are derived "
                              f"from the arm-time cap and will drift.",
                              flush=True)
                if not fresh and mc_factor is None and arm_mcap:
                    # No live figure yet — the board's is better than printing
                    # a raw 1.8e-06 at someone. Replaced on the next success.
                    mc_factor = arm_mcap / price

            # Publish the live entry band on its own clock while waiting. This is the
            # number that tells you when a buy actually fires, so it tracks the
            # 1s price loop rather than the 15s board.
            # Holding: same 2s clock, live mark. Without this the terminal's
            # last word on an open position is the fill price.
            if (sess.state == "POS" and price and mc_factor and entry_px
                    and time.time() - last_band > C.STATUS_PUBLISH_SEC):
                last_band = time.time()
                # Refresh what a restart would need. Cheap, and the alternative
                # is resuming with a stale stop.
                if k in open_positions:
                    open_positions[k].update(
                        ppeak=sess.ppeak, tp_done=sess.tp_done,
                        received=received, tokens_raw=tokens_held)
                    S.save(open_positions, seen_signals, armed_mints)
                _freeze_entry()
                # Which rung we are past, not just "in position". This
                # published "bought" unconditionally, so a TP1 status survived
                # about a second before the next heartbeat overwrote it — the
                # rung fired, the board said nothing, and a part-sold position
                # was indistinguishable from an untouched one.
                held_state = f"tp{sess.tp_done}" if sess.tp_done else "bought"
                held_open = (round(tokens_held / max(tokens_original, 1) * 100)
                             if sess.tp_done and tokens_original else None)
                held_mult = (sess.tps[sess.tp_done - 1]
                             if sess.tp_done and sess.tp_done <= len(sess.tps) else None)
                # Whatever will actually fire — trailing, fixed, or lifted to
                # break-even. Never the formula for a mode that is not on.
                stop_px = stop_px_of(sess)
                await report_status(
                    s, user, mint, name, held_state, MC(price),
                    (price / entry_px) - 1,          # unrealised, mark to market
                    mult=held_mult, open_pct=held_open,
                    # entry and the rungs are frozen; the stop rides the peak
                    # and is genuinely a live number.
                    targets={"entry": entry_mc,
                             "tps": tp_mcs,
                             "next": (tp_mcs[sess.tp_done]
                                      if sess.tp_done < len(tp_mcs) else None),
                             "stop": MC(stop_px) if stop_px else None})

            if (sess.state == "WAIT" and price and mc_factor
                    and time.time() - last_band > C.STATUS_PUBLISH_SEC):
                last_band = time.time()
                # Buy/sell balance is refreshed on the 2s live-anchor call
                # above, not here. It used to be re-read from the board every
                # 15s, which meant the gate decided on a figure up to a minute
                # old while the coin page showed a one-second one. Two sources
                # for one field is how they end up disagreeing.
                await report_status(
                    s, user, mint, name, "watching", MC(price), None,
                    band=_band(sess, MC))

            # ── fill whatever was decided on the PREVIOUS poll ──────────────
            # Real trades don't fill at the price that triggered them. By the time
            # your transaction lands the market has moved. Dry run models that the
            # same way live does: decide now, fill on the next tick, at a real
            # Jupiter quote for the real size (fees + price impact included).
            if pending is not None:
                act, pending = pending, None

                if act[0] == "BUY":
                    got, cost, fill_px, err = await dry_or_live_buy(s, mint, size_sol, fees, wallet=wallet)
                    if err:
                        # Only ever reached when nothing was broadcast or the
                        # chain rejected it. A swap that landed comes back
                        # without an error even if we couldn't measure the fill
                        # — see the note in jupiter.buy().
                        await note(s, f"❌ <b>{name}</b> buy failed — {err}. Nothing was bought.")
                        ended_as = "buy failed"
                        await report_status(s, user, mint, name, "buy failed",
                                            MC(price), None)
                        break
                    tokens_held, spent, entry_px = got, cost, fill_px
                    # Fixed at the fill and never recomputed: the entry cap and
                    # the rungs measured off it describe something that already
                    # happened. Recomputing them against a drifting calibration
                    # made a completed buy appear to change price.
                    #
                    # What the market cap ACTUALLY was when this filled. Read
                    # now, not derived from a calibration taken before it, so
                    # the entry figure matches what GMGN showed at the buy.
                    # The cap implied by the price WE ACTUALLY PAID, computed
                    # from supply and the SOL price rather than borrowed. This
                    # is the number GMGN's trade row shows, because it carries
                    # our own price impact rather than quoting spot beside it.
                    scale = await J.mc_scale(s, mint)
                    if scale:
                        mc_factor = scale
                    if fill_px and mc_factor:
                        print(f"ENTRY MC {name}: {fill_px * mc_factor:,.0f} "
                              f"from the fill price on chain", flush=True)
                    entry_mc, tp_mcs = "—", []
                    _freeze_entry()
                    tokens_original = got        # the ladder is fractions of THIS
                    # From here this coin's price is a stop check, not one feed
                    # among many. It goes to the front of the Jupiter queue.
                    J.hold(mint)
                    sess.on_filled(fill_px)
                    open_positions[k] = {"name": name, "tokens_raw": tokens_held,
                                            "entry_px": fill_px, "spent_sol": spent,
                                            "opened": time.time(),
                                            # the trailing stop rides this; without
                                            # it a restart resets the stop to entry
                                            "ppeak": sess.ppeak,
                                            "tp_done": sess.tp_done,
                                            "received": received,
                                            "tokens_original": tokens_original,
                                            "user": user, "wallet_id": wallet_id,
                                            # The strategy this trade is being
                                            # run under. The arm carried it and
                                            # the resume did not, so a restart
                                            # re-resolved the coin through the
                                            # DEFAULT strategy and silently
                                            # changed its take-profit and its
                                            # stop mid-trade. It belongs on the
                                            # position, which outlives the arm.
                                            "preset": sig.get("preset"),
                                            "fee_bps": sig.get("fee_bps")}
                    S.save(open_positions, seen_signals, armed_mints)
                    tag = " (dry)" if C.DRY_RUN else ""
                    # The ladder is ONE rung when TP2 is switched off. Reading
                    # tps[1] unconditionally threw here — after the buy had
                    # already landed — which killed the session and orphaned a
                    # live position. Build the list from whatever rungs exist.
                    impact = await price_impact_pct(s, mint, size_sol)
                    imp_txt = ""
                    if impact is not None and impact >= IMPACT_WARN_PCT:
                        imp_txt = f"\n⚠️ Price impact {impact:.1f}% — thin liquidity."
                    elif impact is not None:
                        imp_txt = f"\nPrice impact {impact:.2f}%."
                    tp_txt = " / ".join(MC(fill_px * t) for t in sess.tps)
                    await note(s, f"🎯 <b>{name}</b> BUY{tag} · {size_sol} SOL\n"
                                     f"Entry at <b>{MC(fill_px)}</b> MC{imp_txt}\n"
                                     f"TP {tp_txt} · "
                                     f"stop -{int(sess.kill*100)}% off "
                                     f"{'peak' if sess.trail_stop else 'entry'}"
                                     f" ({MC(stop_px_of(sess))})")
                    await report_status(
                        s, user, mint, name, "bought", MC(fill_px), None,
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
                    # The same widening the stop uses. A rung fires exactly
                    # when the book is moving, which is also when a quote goes
                    # stale mid-flight, so the first attempt reverts on slippage
                    # more often than not.
                    tp_fees = dict(fees or {})
                    if tp_tries:
                        base = tp_fees.get("slippage_bps") or 1500
                        # Capped where the stop caps it. Past 25% an exit stops
                        # being a take-profit.
                        tp_fees["slippage_bps"] = min(base * (1 + tp_tries), 2500)
                    got, err = await dry_or_live_sell(s, mint, amount, tp_fees, wallet=wallet)
                    await _sweep_wrapped(s, wallet, name)
                    if err:
                        # The rung counted itself as fired before we tried to
                        # sell. Give it back — otherwise one revert silently
                        # retires the only take-profit this position had, and
                        # the ladder never fires again however high it goes.
                        # It re-arms on the next poll while price is still above
                        # the rung, and stops asking if price falls back under
                        # it, where the stop takes over.
                        sess.tp_done = max(0, sess.tp_done - 1)
                        tp_tries += 1
                        # Once, then occasionally: a message a second is noise.
                        if tp_tries == 1 or tp_tries % 60 == 0:
                            await note(s, f"⚠️ <b>{name}</b> {label} sell failed — {err}. "
                                          f"Attempt {tp_tries}; still holding and "
                                          f"retrying with wider slippage.")
                    else:
                        tp_tries = 0
                        received += got
                        tokens_held -= amount
                        if k in open_positions:
                            open_positions[k]["tokens_raw"] = tokens_held
                            S.save(open_positions, seen_signals, armed_mints)
                        tag = " (dry)" if C.DRY_RUN else ""
                        sold_pct = round(amount / max(tokens_original, 1) * 100)
                        open_pct = round(tokens_held / max(tokens_original, 1) * 100)
                        mult = sess.tps[rung - 1] if rung else None
                        # The FILL, not the trigger. The rung fires on the
                        # polled price, which is right — but what actually
                        # executed is a slipped price a moment later, and
                        # printing the trigger overstates every exit.
                        fill_out = (got / (amount / 1e6)) if amount else None
                        if fill_out and entry_px:
                            rungs_hit.append({"rung": rung, "pct": sold_pct,
                                              "mult": round(fill_out / entry_px, 2),
                                              "mc": MC(fill_out)})
                        await note(
                            s, f"💰 <b>{name}</b> TP{rung}{tag} · sold {sold_pct}% at "
                               f"<b>{MC(fill_out)}</b> MC "
                               f"({(fill_out / entry_px):.2f}x) for {got:.4f} SOL"
                               + (f"\n{open_pct}% still riding to {sess.tps[rung]}x." if not is_last else ""))
                        await report_status(
                            s, user, mint, name, f"tp{rung}", MC(fill_out),
                            (received - spent) / max(spent, 1e-9),
                            mult=mult, open_pct=open_pct,
                            # entry and the remaining rung, so a part-sold
                            # position still says where the rest gets out
                            targets={"entry": entry_mc, "tps": tp_mcs,
                                     "next": (tp_mcs[rung] if rung < len(tp_mcs) else None),
                                     "stop": (MC(stop_px_of(sess))
                                              if stop_px_of(sess) else None)})
                    # NEVER on a sell that failed. This closed on is_last
                    # alone, so a reverted last rung ended the session with the
                    # whole position still in the wallet — no take-profit, no
                    # stop, nothing watching it — and journalled the entire
                    # stake as a loss that had not happened. ALPHA reverted on
                    # slippage at 1.73x and was written off at -0.1000 SOL while
                    # 138,108 tokens sat in the wallet.
                    if tokens_held <= 0 or (is_last and not err):
                        sess.on_closed()
                        break

                elif act[0] == "SELLALL":
                    # The stop fires precisely when the book is moving, which is
                    # also when a quote goes stale mid-flight — so the first
                    # attempt reverts on slippage more often than not. Widen it
                    # on each retry rather than accepting the first refusal.
                    sell_fees = dict(fees or {})
                    if stop_tries:
                        base = sell_fees.get("slippage_bps") or 1500
                        # Capped at 25%. Uncapped escalation eventually fills at
                        # any price, which is not a stop-loss, it is a donation.
                        # If 25% cannot clear it the position stays put and keeps
                        # asking, which is the honest outcome for a book this thin.
                        sell_fees["slippage_bps"] = min(base * (1 + stop_tries), 2500)
                    got, err = await dry_or_live_sell(s, mint, tokens_held, sell_fees,
                                                      wallet=wallet)
                    await _sweep_wrapped(s, wallet, name)
                    if err:
                        # Deliberately NOT closing here. The sell reverted, so
                        # the tokens are still ours; closing left a bag with no
                        # stop and journalled a 100% loss that never happened.
                        # Falling through re-runs sess.feed(), which keeps
                        # returning SELLALL while the price is under the stop,
                        # so the next poll tries again with wider slippage.
                        # It can also fail because there is nothing to sell —
                        # sold from the terminal, or from a wallet app. The
                        # retry assumes the opposite, so this span 600+ times
                        # on a position that no longer existed. Ask the wallet,
                        # then hand it to the drop path, which reads the real
                        # proceeds off chain and journals them.
                        if _looks_empty(err):
                            try:
                                left = await J.token_balance(s, mint, wallet=wallet)
                            except Exception:
                                left = None
                            if left == 0:
                                print(f"SELL FAILED BUT WALLET EMPTY {name}: "
                                      f"treating as gone", flush=True)
                                cancel_mints.add(k)
                                continue
                        stop_tries += 1
                        # Once, then occasionally. A message a second is noise,
                        # and noise is how the one that matters gets ignored.
                        if stop_tries == 1 or stop_tries % 60 == 0:
                            await note(s, f"🚨 <b>{name}</b> STOP SELL FAILED — {err}. "
                                             f"Attempt {stop_tries}; still holding and "
                                             f"retrying with wider slippage.")
                    else:
                        received += got
                        tokens_held = 0
                        tag = " (dry)" if C.DRY_RUN else ""
                        await note(s, f"🔴 <b>{name}</b> stopped out{tag} at <b>{MC(price)}</b> MC "
                                         f"for {got:.4f} SOL"
                                         + (f" after {stop_tries} failed attempts" if stop_tries else ""))
                        await report_status(s, user, mint, name, "stopped", MC(price),
                                            (received - spent) / max(spent, 1e-9),
                                            targets={"entry": entry_mc,
                                                     "rungs": rungs_hit} if rungs_hit
                                                    else {"entry": entry_mc})
                        sess.on_closed()
                        break

            # ── decide (fills next poll) ───────────────────────────────────
            action = sess.feed(price)
            if action is not None:
                pending = action

            # Whatever is left of the second. Never negative, and when the
            # work has already overrun it yields control without sleeping so
            # the next price is fetched immediately rather than a second late.
            left = due - time.monotonic()
            if left > 0:
                await asyncio.sleep(left)
            else:
                await asyncio.sleep(0)
                if -left > C.POLL_SEC and sess.state == "POS":
                    # Only while holding, because only then does a missed poll
                    # mean an unchecked stop.
                    print(f"POLL OVERRUN {name}: iteration took "
                          f"{C.POLL_SEC - left:.1f}s against a {C.POLL_SEC:.0f}s "
                          f"budget — the stop went unchecked for that long.",
                          flush=True)

    except Exception as e:
        import traceback
        print(f"work_coin {name} ERROR: {e}\n{traceback.format_exc()}", flush=True)
        if tokens_held > 0:
            # Holding something. Never walk away from it because of a crash —
            # that leaves a bag with no take-profit and no stop, and nothing
            # watching. Record it so the restart reconciliation finds it.
            open_positions[k] = {"name": name, "tokens_raw": tokens_held,
                                    "entry_px": entry_px, "spent_sol": spent,
                                    "opened": time.time(), "orphaned_by": str(e)[:120]}
            S.save(open_positions, seen_signals, armed_mints)
            print(f"ORPHAN GUARD {name}: crashed while holding {tokens_held} "
                  f"tokens — position recorded, sell it from the terminal.", flush=True)
            await note(s, f"🚨 <b>{name}</b> crashed while holding a position — "
                          f"{e}. The position is saved, NOT being managed. "
                          f"Sell it from the wallet panel.")
        else:
            await note(s, f"💥 <b>{name}</b> error: {e}")
    finally:
        print(f"DONE {name}: entered={entry_px is not None} "
              f"peak_after_tap={sess.peak_since_tap()}", flush=True)
        # Give the queue lane back. In the finally block on purpose: a session
        # that died on an exception would otherwise leave the mint marked as
        # held forever, and every later coin would queue behind a position
        # that no longer exists.
        if not any(kk != k and kk.endswith(mint) for kk in open_positions):
            J.release(mint)
        live_sessions.pop(k, None)
        # Only forget the position if we're actually flat. The orphan guard
        # above may have just recorded one on purpose.
        if tokens_held <= 0:
            open_positions.pop(k, None)
        # The session is over however it ended — bought out, stopped, timed out
        # or dropped. Leaving it armed would re-arm a finished coin on the next
        # restart, which is how you end up watching yesterday's trade.
        armed_mints.pop(k, None)
        S.save(open_positions, seen_signals, armed_mints)
        # Each step guarded separately. These run in `finally`, so an
        # exception here escapes as "Task exception was never retrieved" and
        # the trade silently never reaches the journal — which is exactly what
        # a missing sess.tap_ts did to every finished session for a day.
        # A session that ends without entering has, until now, left the
        # terminal on "watching" forever. Whatever happened, the panel must
        # stop claiming a live dip window. band=None clears it.
        if entry_px is None and ended_as is None:
            try:
                await report_status(s, user, mint, name, "stood down", None, None)
            except Exception:
                pass

        row = None
        try:
            row = record(sess, entry_px, spent, received)
        except Exception as e:
            print(f"JOURNAL WRITE FAILED {name}: {type(e).__name__}: {e}", flush=True)
            await note(s, f"⚠️ <b>{name}</b> finished but could not be written to "
                          f"your journal — {type(e).__name__}. The trade itself is "
                          f"unaffected; the record is missing.")
        if row is not None:
            try:
                await push_trade(s, row, user)
            except Exception as e:
                print(f"JOURNAL PUSH FAILED {name}: {e}", flush=True)
        try:
            await report(s, sess, entry_px, spent, received)
        except Exception as e:
            print(f"REPORT FAILED {name}: {e}", flush=True)


async def push_trade(s, row, user=None):
    """Send a finished session to your journal. Best-effort — the local
    trades.jsonl is the record of truth, this is for reading it back."""
    try:
        hdrs, body = _as_user(user, row)
        await s.post(f"{C.EDGE_API}/api/trades", json=body, headers=hdrs,
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
        # tap_ts and tap_px are kept as raw facts about the greenlight. What is
        # deliberately NOT kept is a "peak after tap": every attempt to rebuild
        # one from candles disagreed with the trade it described, and a journal
        # figure that moves after the fact is worse than none.
        "tap_ts": round(sess.tap_ts, 3) if sess.tap_ts else None,
        "tap_px": sess.raw_tap,
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
async def ensure_gas(s, why="", wallet=None):
    """Top up the native SOL of the wallet that is about to trade.

    `wallet` is required in practice: without it this falls back to the
    environment wallet, which for a customer's session is the WRONG account —
    their deposit stays unwrapped and their buy fails for lack of funds they
    actually have.

    Called before a buy rather than on a timer, because that's the moment it
    matters and the moment we know a trade is about to need it. A failure here
    is reported and the trade is skipped — a buy with no gas doesn't fail
    loudly, it fails as a transaction that never lands, which the bot used to
    record as a real position.
    """
    w = wallet if wallet is not None else J.WALLET
    if w is None or C.DRY_RUN or w.kind != "privy":
        return True
    try:
        # A plain-SOL deposit is idle money. Sweep it into tradeable balance
        # first, so a buy isn't skipped for lack of funds the wallet already
        # holds. Cheap to check and a no-op when there's nothing idle.
        wsig, werr = await GAS.wrap_idle(s, w)
        if wsig:
            print(f"wrapped an idle deposit{why} · {wsig}", flush=True)
        elif werr and werr != "nothing_idle":
            print(f"WRAP FAILED{why}: {werr}", flush=True)

        if not await GAS.needs_gas(s, w.address):
            return True
        sig, err = await GAS.top_up(s, w)
        if err == "not_needed":
            return True
        if err:
            print(f"GAS TOP-UP FAILED{why}: {err}", flush=True)
            return False
        print(f"gas topped up{why} · {sig}", flush=True)
        return True
    except Exception as e:
        print(f"GAS TOP-UP ERROR{why}: {type(e).__name__}: {e}", flush=True)
        return False


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

    bal = await J.trading_balance(s)
    if bal is None:
        return (f"⚠️ <b>No WSOL account yet</b> — nothing can be bought.\n"
                f"Deposit SOL to <code>{w.address}</code> and wrap it, "
                f"then restart.")
    if bal < C.SIZE_SOL:
        return (f"⚠️ <b>Trading balance too low</b> — {bal:.4f} WSOL, "
                f"but trades are {C.SIZE_SOL} SOL.\n"
                f"Top up <code>{w.address}</code>.")
    print(f"trading balance: {bal:.4f} SOL", flush=True)
    return None


# How often the wallet is checked against our records for positions nothing is
# actively trading. Frequent enough that a cancel shows up while you are still
# looking at the app; slow enough to be a handful of balance reads an hour.
RECONCILE_SEC = 120


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
        async def reconcile_sweep():
            """The wallet against our records, for anything nothing is trading.

            This ran once, at startup. So a coin you sold yourself -- or
            cancelled from the terminal -- stayed "in position" in the app, out
            of the journal, and out of your P&L until the next deploy, which
            might be days away. The money had moved and nothing said so.

            Positions with a live session are skipped on purpose. That session
            is mid-trade and records its own exit; a sweep landing between its
            sell and its bookkeeping would journal the same trade twice.
            """
            if C.DRY_RUN:
                return
            watched = {k: v for k, v in open_positions.items()
                       if k not in live_sessions}
            if not watched:
                return

            async def _pos_balance(http, pkey):
                """Balance for a position key, on ITS OWN wallet.

                Positions are keyed "user|mint" once there is more than one
                customer. Handing that straight to a mint lookup matches
                nothing, and checking our own wallet answers the wrong question
                for somebody else's trade. Returning None when the wallet cannot
                be loaded is deliberate: reconcile leaves a position alone
                rather than closing what it failed to verify.
                """
                pos = open_positions.get(pkey) or {}
                mint_only = pkey.split("|", 1)[1] if "|" in pkey else pkey
                wid = pos.get("wallet_id")
                w = None
                if wid and not C.DRY_RUN:
                    try:
                        w = await J.privy.PrivyWallet.load(http, wid)
                    except Exception as e:
                        print(f"reconcile: could not load wallet {str(wid)[:10]}… "
                              f"({e}) — leaving {mint_only[:8]} open", flush=True)
                        return None
                return await J.token_balance(http, mint_only, wallet=w)

            notes, closed = await S.reconcile(s, watched, _pos_balance)
            # reconcile pops from the dict it was handed, which is a subset.
            for pkey, _ in closed:
                open_positions.pop(pkey, None)
            for n in notes:
                await note(s, f"🔄 {n}")
            # A coin sold outside the bot stops being watched, too.
            #
            # A session that ends normally clears its own arm in `finally`;
            # reconcile closes a position without one ever running, so the arm
            # outlived the trade and the next line re-armed it. Selling by hand
            # and immediately being put back on the hunt for a fresh entry in
            # the same coin is not what "I sold it" means.
            for pkey, _ in closed:
                armed_mints.pop(pkey, None)
            S.save(open_positions, seen_signals, armed_mints)

            # Someone sold these by hand. Record them, with the proceeds read
            # off the chain rather than guessed — a trade that leaves no row
            # silently improves every number computed from the journal.
            cancelled = set()
            if closed:
                try:
                    # Built here: bot_hdrs elsewhere is local to the pollers.
                    hdrs_all = {"Authorization": f"Bearer {C.BOT_ADMIN_KEY}"}
                    async with s.get(f"{C.EDGE_API}/api/cancels/all",
                                     headers=hdrs_all,
                                     timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status == 200:
                            for u in (await r.json()).get("users") or []:
                                for m in u.get("mints") or []:
                                    cancelled.add((u.get("identity"), m))
                except Exception as e:
                    # Only the wording depends on this. A trade is still
                    # recorded, still with proceeds read from the chain.
                    print(f"CANCEL LOOKUP FAILED: {e}", flush=True)

            for pkey, pos in closed:
                # pkey is _key(user, mint): "user|mint" for a customer, a bare
                # mint for our own wallet. Everything downstream needs the mint
                # and the user separately, not the composite.
                if "|" in pkey:
                    pos_user, mint = pkey.split("|", 1)
                else:
                    pos_user, mint = None, pkey
                try:
                    got, _ = await J.exit_proceeds(
                        s, mint, since=int(pos.get("opened") or 0))
                except Exception as e:
                    print(f"EXIT LOOKUP FAILED {mint[:12]}…: {e}", flush=True)
                    got = None
                if got is None:
                    # Could not read the chain. Say so and write NOTHING — a
                    # guessed zero here becomes a total loss in the record.
                    name = pos.get("name", mint[:8])
                    print(f"EXIT UNKNOWN {name}: chain unreadable, not "
                          f"journalling a figure we do not have", flush=True)
                    await note(s, f"⚠️ <b>{name}</b> left the wallet but its "
                                  f"proceeds could not be read. Nothing was "
                                  f"written to your journal — check the trade.")
                    continue
                spent = float(pos.get("spent_sol") or 0)
                name = pos.get("name", mint[:8])
                # You pressed Cancel in the terminal, or the coin left the
                # wallet some other way. Same money, different event, and
                # calling the first one "sold outside the bot" reads as though
                # something happened without you.
                was_cancel = (pos_user, mint) in cancelled
                row = {
                    "name": name, "mint": mint, "dry_run": False,
                    "entered": True,
                    "entry_px": pos.get("entry_px"),
                    "spent_sol": spent,
                    "received_sol": round(got, 6),
                    "pnl_sol": round(got - spent, 6),
                    "tap_ts": pos.get("opened"),
                    "note": ("order cancelled — proceeds read from chain"
                             if was_cancel else
                             "exited outside the bot — proceeds read from chain"),
                }
                try:
                    await push_trade(s, row, pos_user)
                except Exception as e:
                    print(f"JOURNAL PUSH FAILED {name}: {e}", flush=True)
                # The board mirrors this status. Without it the terminal keeps
                # showing "in position" for a coin that is already gone.
                try:
                    await report_status(
                        s, pos_user, mint, name,
                        "cancelled" if was_cancel else "sold elsewhere", None,
                        (got - spent) / spent if spent else None)
                except Exception as e:
                    print(f"STATUS PUSH FAILED {name}: {e}", flush=True)
                if spent:
                    what = "order cancelled" if was_cancel else "sold outside the bot"
                    await note(s, f"📓 <b>{name}</b> logged: {what} for "
                                  f"{got:.4f} SOL · {(got - spent):+.4f} SOL")
                # Clear the marker so a later position in the same coin is not
                # described by an old cancel.
                if was_cancel:
                    try:
                        await _ack(s, "cancels", mint, pos_user)
                    except Exception as e:
                        print(f"CANCEL ACK FAILED {name}: {e}", flush=True)

        await reconcile_sweep()

        if open_positions:
            # Hand them back. Abandoning a live position on every deploy was the
            # worst failure in the system precisely because nothing looked
            # broken — the trade simply stopped having a stop.
            resumed, failed = [], []
            for pkey, pos in list(open_positions.items()):
                if "|" in pkey:
                    pos_user, mint = pkey.split("|", 1)
                else:
                    pos_user, mint = None, pkey
                if pkey in live_sessions:
                    continue
                sig = await lookup_coin(s, mint)
                if not sig:
                    # No feed for it. Say so rather than pretend it is covered.
                    failed.append(pos.get("name", mint[:8]))
                    continue
                sig = dict(sig)
                sig["user"] = pos_user or pos.get("user")
                sig["wallet_id"] = pos.get("wallet_id")
                # Rebuilt from lookup_coin, which knows nothing about how the
                # trade was set up. Without these the coin came back resolved
                # through the account's DEFAULT strategy: a position bought at
                # 1.5x take-profit on a trailing stop resumed at 2x on a fixed
                # one, with nothing said. The arm is the fallback for positions
                # opened before the preset was stored on them.
                armed_rec = armed_mints.get(pkey) or {}
                sig["preset"] = pos.get("preset", armed_rec.get("preset"))
                sig["fee_bps"] = pos.get("fee_bps", armed_rec.get("fee_bps"))
                asyncio.create_task(work_coin(s, sig, resume=pos))
                resumed.append(pos.get("name", mint[:8]))

            if resumed:
                await note(s, f"🔁 <b>Back under management after the restart:</b> "
                              f"{', '.join(resumed)} — stop and take-profit "
                              f"continue from where they were.")
            if failed:
                await note(
                    s,
                    f"⚠️ <b>Could not resume {len(failed)} position(s)</b>: "
                    f"{', '.join(failed)}.\n"
                    f"No price feed for them, so there is <b>no take-profit and "
                    f"no stop</b>. Sell them yourself, or greenlight the coin "
                    f"again once it is back on the board.")

        # ── coins that were armed when we stopped ───────────────────────────
        # Their sessions were in memory and died with the process. Pick them
        # back up rather than leaving you thinking a coin is being watched.
        if armed_mints:
            recovered = []
            dropped = []
            for pkey in list(armed_mints):
                who = armed_mints.get(pkey) or {}
                # The key is "user|mint" once there is more than one customer.
                # Handing the whole thing to a mint lookup matches nothing,
                # which is how every armed coin used to vanish on restart.
                if "|" in pkey:
                    pos_user, mint = pkey.split("|", 1)
                else:
                    pos_user, mint = None, pkey
                if pkey in live_sessions or pkey in open_positions:
                    continue                      # a position already has it
                sig = await lookup_coin(s, mint)
                wallet_id = who.get("wallet_id")
                if not sig or (pos_user and not wallet_id):
                    # Either the feed lost it, or it predates this state format
                    # and we cannot tell which wallet to watch it for. Drop it
                    # AND say so — a coin silently unwatched while the screen
                    # says otherwise is the failure this whole block exists for.
                    armed_mints.pop(pkey, None)
                    dropped.append(mint[:8])
                    await report_status(
                        s, pos_user, mint,
                        (open_positions.get(pkey) or armed_mints.get(pkey) or {}).get("name") or "",
                        "unarmed", None, None)
                    continue
                sig = dict(sig)
                sig["user"] = pos_user or who.get("user")
                sig["wallet_id"] = wallet_id
                # Put back what was chosen on the coin page. Without these the
                # coin comes back under the account defaults, which is a
                # different size and possibly a different strategy from the one
                # that was greenlit.
                sig["size_sol"] = who.get("size_sol")
                sig["preset"] = who.get("preset")
                sig["fee_bps"] = who.get("fee_bps")
                asyncio.create_task(work_coin(s, sig))
                recovered.append(sig.get("name", mint[:8]))
            S.save(open_positions, seen_signals, armed_mints)
            if recovered:
                await note(s, f"🔄 <b>Picked back up after the restart:</b> "
                                 f"{', '.join(recovered)}")
            if dropped:
                await note(s, f"⚠️ <b>Stopped watching after the restart:</b> "
                              f"{', '.join(dropped)} — greenlight again if you "
                              f"still want them.")

        asyncio.create_task(poll_web_greenlights(s))
        # Following a bundler is a second source of greenlights, not a second
        # strategy: it decides WHICH coin and hands it to arm_mint exactly as
        # the terminal does. Everything after that — the dip, the reclaim, the
        # size, the exits — is the same code path as a coin you tapped
        # yourself. Off unless COPY_ENABLED is set.
        asyncio.create_task(CW.run(s, arm_mint, lambda: len(live_sessions)))
        async def reconcile_loop():
            while True:
                await asyncio.sleep(RECONCILE_SEC)
                try:
                    await reconcile_sweep()
                except Exception as e:
                    print(f"RECONCILE SWEEP FAILED: {e}", flush=True)
        asyncio.create_task(reconcile_loop())
        asyncio.create_task(poll_web_unarms(s))
        # Nothing to poll for input any more — greenlights arrive from the
        # terminal. Stay alive so the watcher tasks keep running.
        while True:
            await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
