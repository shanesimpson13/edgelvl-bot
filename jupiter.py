"""
jupiter.py — price + execution. Both through Jupiter, which routes across every
Solana venue and always takes the best price. Free, no API key.

Price comes from the same quote endpoint we trade through, so the number the
strategy sees is the number you'd actually get filled at — impact included.
"""
import asyncio
import base64
import time
from collections import deque

import aiohttp
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

import config as C
import privy

JUP = C.JUP_HOST
WSOL = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ATA_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

# Signing is only needed for live trading. In dry run there's no wallet at all.
#
# Two backends, same interface: a local keypair (self-hosted — you hold your own
# key) or Privy (hosted terminal — nobody holds the customer's key, and the
# wallet's policy makes a transfer impossible rather than merely disallowed).
kp = Keypair.from_base58_string(C.PRIVATE_KEY) if C.PRIVATE_KEY else None

# The DEFAULT signer, from the environment. Every function below takes an
# optional `wallet` and falls back to this, so a single-user deployment behaves
# exactly as before while a multi-user one passes the customer's wallet in.
#
# This matters because one Privy auth key signs for EVERY wallet the bot is a
# signer on — adding a customer means knowing their wallet_id, not holding a
# new secret. So the only thing that ever needed to vary per user was which
# wallet object gets handed to these functions.
WALLET = privy.build(kp)
ME = WALLET.address if WALLET else None


def _w(wallet):
    """The wallet to act as: the one passed, or the environment's."""
    return wallet if wallet is not None else WALLET


def ata(owner, mint):
    """The associated token account address for (owner, mint).

    Deterministic, so the WSOL account holding a trading balance can always be
    derived rather than stored.
    """
    o = owner if isinstance(owner, Pubkey) else Pubkey.from_string(owner)
    m = mint if isinstance(mint, Pubkey) else Pubkey.from_string(mint)
    addr, _ = Pubkey.find_program_address(
        [bytes(o), bytes(TOKEN_PROGRAM), bytes(m)], ATA_PROGRAM)
    return str(addr)

# ── request pacing ──────────────────────────────────────────────────────────
# Every watched coin polls once a second, so N coins means N requests a second.
# Jupiter's free tier is roughly 1/sec in TOTAL, so two coins is already over it
# — and a 429 comes back as an empty quote, which the strategy reads as "no
# price" and silently does nothing forever. Pace every request through one gate.
_gate = asyncio.Lock()
_last_req = 0.0
_rate_limited_until = 0.0
_warned = False

# Mints we are HOLDING. Their price is not one feed among many: a stop is only
# checked when a price lands, so a starved feed is a stop that does not exist
# for as long as it is starved. VISUALIZE sat 3.5s stale one second after its
# own buy — its own order's quotes had queued ahead of it — and its stop fired
# 30% below where it should have.
#
# The queue was a plain Lock, which is strictly first-come. Six watched coins
# and a buy in flight are all it takes to put a held position behind eight
# requests it does not care about.
_held = set()
_urgent_waiting = 0


def hold(mint):
    """This mint now has money in it — its price jumps the queue."""
    _held.add(mint)


def release(mint):
    _held.discard(mint)


async def _paced_get(s, url, headers=None, urgent=False):
    """One shared throttle for every Jupiter call, plus loud rate-limit handling.

    `urgent` does not raise the total rate — a 429 costs a 20 second blackout
    across every call, which is far worse than the starvation it would fix. It
    reorders instead: while anything urgent is queued, ordinary traffic steps
    aside and retries. Same requests per second, spent on the coin that has
    money in it.
    """
    global _last_req, _rate_limited_until, _warned, _urgent_waiting

    if urgent:
        _urgent_waiting += 1
    try:
        while True:
            async with _gate:
                if urgent or not _urgent_waiting:
                    gap = 1.0 / max(C.JUP_MAX_RPS, 0.1)
                    wait = _last_req + gap - time.monotonic()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    _last_req = time.monotonic()
                    break
            # Yield OUTSIDE the lock, or standing aside would block the very
            # request we are standing aside for.
            await asyncio.sleep(0.02)
    finally:
        if urgent:
            _urgent_waiting -= 1

    if time.monotonic() < _rate_limited_until:
        return {}, "cooling"

    try:
        async with s.get(url, headers=headers or {},
                         timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 429:
                _rate_limited_until = time.monotonic() + C.JUP_COOLDOWN
                if not _warned:
                    _warned = True
                    print("JUPITER RATE LIMITED (429) — prices are not updating. "
                          "Set JUP_API_KEY (free at portal.jup.ag) or watch fewer "
                          "coins at once.", flush=True)
                return {}, "429"
            if r.status != 200:
                return {}, f"http-{r.status}"
            return await r.json(), None
    except Exception as e:
        return {}, type(e).__name__


def _headers():
    return {"x-api-key": C.JUP_API_KEY} if C.JUP_API_KEY else {}


async def rpc(s, method, params):
    """Minimal Solana JSON-RPC call."""
    async with s.post(C.RPC_URL, json={"jsonrpc": "2.0", "id": 1,
                                       "method": method, "params": params}) as r:
        return await r.json()


async def quote(s, in_mint, out_mint, amount, slippage_bps=None, urgent=False):
    """A price for this swap, without committing to it.

    v2 has no quote endpoint. /order with the `taker` OMITTED is one: it returns
    the routed amount and no transaction. Omitting the taker also means a probe
    doesn't need the wallet to hold the funds, so we can price a coin before
    deciding to buy it — with a taker, /order answers "Insufficient funds".

    Routers are excluded here for the same reason they are when trading: pricing
    against liquidity we won't actually route through would quote a fill we
    can't get. The price the strategy sees stays the price it can trade.

    NOT Price API v3, which was measured refreshing every ~5-6s. This endpoint
    moves every second, and the entry depends on seeing the dip.
    """
    import ultra as U
    url = (f"{U.SWAP_BASE}/order?inputMint={in_mint}&outputMint={out_mint}"
           f"&amount={int(amount)}&excludeRouters={U.EXCLUDE_ROUTERS}"
           f"&slippageBps={slippage_bps or C.SLIPPAGE_BPS}")
    q, err = await _paced_get(s, url, _headers(), urgent=urgent)
    return q or {}


# Price is per COIN, not per customer, so two people watching the same coin
# should cost one quote rather than two. Same single-flight shape the API uses
# for its market data: the first caller starts the fetch, everyone else awaits
# the same task. Without this, N sessions on one coin is N quotes a second and
# multi-user is unaffordable before it is even useful.
_px_cache = {}          # mint -> (fetched_at, price)
# How far apart a mint's fetches actually landed. The strategy assumes ~1s; if
# the shared throttle stretches that, its dip and reclaim windows are being
# evaluated on stale data and nothing else would say so.
_px_last = {}           # mint -> monotonic time of last real fetch
_starve_warned = 0.0
STARVE_AT = 1.5         # a held stop is only checked when a price lands
# Actual measured intervals between price fetches, reported on a timer.
#
# Twice now the real poll rate has drifted away from the advertised one and
# stayed there — first the loop sleeping a second ON TOP of its own work, then
# the cache quietly swallowing every other poll. Both were invisible because
# nothing ever stated the rate it was ACHIEVING, only the rate it intended.
# A warning that fires on the worst case cannot tell you the typical one.
_gaps = deque(maxlen=400)
_gap_report = 0.0
GAP_REPORT_SEC = 300.0
STARVE_QUIET = 30.0     # was 120s, which hid all but a handful of events
_px_inflight = {}       # mint -> Task
# Comfortably under the poll, NOT just under it.
#
# 0.9 against a 1.0s poll looks right and is a trap. A fetch takes 50-150ms, so
# the cache is stamped at t+0.15 and the next poll at t+1.0 finds it 0.85s old
# — inside the window — and serves the cache instead of fetching. The real
# interval then doubles to 2.0s, and the stop is only ever checked when a price
# lands. It hid while the loop ran raggedly at ~1.6s, because that always
# exceeded 0.9; pacing the loop to a true 1.0s is what exposed it.
#
# This does not slow the sharing it exists for: two sessions watching the same
# coin within half a second still get one fetch between them.
PRICE_TTL = 0.5


async def get_price(s, mint):
    """SOL per token. Shared across every session watching this coin."""
    hit = _px_cache.get(mint)
    if hit and time.time() - hit[0] < PRICE_TTL:
        return hit[1]

    task = _px_inflight.get(mint)
    if task is None or task.done():
        global _starve_warned
        now = time.monotonic()
        prev = _px_last.get(mint)
        _px_last[mint] = now
        if prev is not None:
            _gaps.append(now - prev)
        if prev is not None and now - prev > STARVE_AT and now - _starve_warned > STARVE_QUIET:
            _starve_warned = now
            print(f"FEED DEGRADED: {mint[:10]}… last priced {now - prev:.1f}s ago "
                  f"(want ~{C.POLL_SEC:.0f}s). {len(_px_last)} coin(s) sharing "
                  f"{C.JUP_MAX_RPS:.0f} req/s — raise JUP_MAX_RPS or the Jupiter plan.",
                  flush=True)
        global _gap_report
        if len(_gaps) >= 30 and now - _gap_report > GAP_REPORT_SEC:
            _gap_report = now
            g = sorted(_gaps)
            over = sum(1 for x in g if x > 1.25) / len(g)
            print(f"POLL RATE: median {g[len(g)//2]:.2f}s  "
                  f"p90 {g[int(len(g)*0.9)]:.2f}s  max {g[-1]:.2f}s  "
                  f"over-1.25s {over:.1%}  (n={len(g)}, target {C.POLL_SEC:.1f}s)",
                  flush=True)
        task = asyncio.create_task(_fetch_price(s, mint))
        _px_inflight[mint] = task
    try:
        return await asyncio.shield(task)
    except Exception:
        return hit[1] if hit else None
    finally:
        if _px_inflight.get(mint) is task and task.done():
            _px_inflight.pop(mint, None)


async def _fetch_price(s, mint):
    """SOL per token, measured with a fixed probe so every reading is comparable.

    Returns None if the coin has no route (illiquid / not migrated yet) — the
    caller should just skip that poll rather than treat it as a price of zero.
    """
    # A held coin's price IS its stop check, so it goes first when the
    # budget is contended. hold()/release() decide which those are.
    q = await quote(s, WSOL, mint, int(C.PROBE_SOL * 1e9), urgent=mint in _held)
    out = int(q.get("outAmount", 0) or 0)
    if out <= 0:
        return None
    decimals = 6  # pump.fun standard
    px = (C.PROBE_SOL) / (out / 10 ** decimals)
    _px_cache[mint] = (time.time(), px)
    return px


USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
_supply = {}          # mint -> total supply. Fixed, so read once.
_sol_usd = [0.0, 0.0]  # (fetched_at, price). One number for the whole market.


async def token_supply(s, mint):
    """Total supply, straight off the mint account. Cached: it does not move."""
    if mint in _supply:
        return _supply[mint]
    r = await rpc(s, "getTokenSupply", [mint])
    v = (r.get("result") or {}).get("value") or {}
    try:
        n = float(v.get("uiAmountString") or 0)
    except (TypeError, ValueError):
        return None
    if n > 0:
        _supply[mint] = n
        return n
    return None


async def sol_usd(s, ttl=60.0):
    """SOL in dollars, from a Jupiter quote. Shared across every coin."""
    now = time.time()
    if _sol_usd[1] and now - _sol_usd[0] < ttl:
        return _sol_usd[1]
    q = await quote(s, WSOL, USDC, int(1e9))
    out = int(q.get("outAmount") or 0)
    if out:
        _sol_usd[0], _sol_usd[1] = now, out / 1e6
    return _sol_usd[1] or None


async def mc_scale(s, mint):
    """Multiply a SOL-per-token price by this to get a market cap in dollars.

    supply x SOL/USD, both sourced directly. Returns None if either is missing,
    and the caller keeps whatever it had — a stale scale beats no figures.
    """
    sup = await token_supply(s, mint)
    su = await sol_usd(s)
    return (sup * su) if (sup and su) else None


async def position_value_sol(s, mint, raw_amount):
    """What you'd actually receive for selling THIS bag right now, in SOL.

    More honest than pricing off a fixed probe: it includes the price impact of
    your own size, which is the number that matters when you're deciding whether
    to take profit. Returns None if there's no route.
    """
    if raw_amount <= 0:
        return None
    q = await quote(s, mint, WSOL, int(raw_amount))
    out = int(q.get("outAmount", 0) or 0)
    return (out / 1e9) if out > 0 else None


async def token_balance(s, mint, wallet=None):
    """Raw token balance held by the wallet.

    0 means the wallet holds none. None means THE CHAIN COULD NOT BE READ,
    which is a different fact and must stay distinguishable: reconciliation
    treats an empty balance as a closed position, so an RPC hiccup reported as
    0 abandons a position that is still held.
    """
    w = _w(wallet)
    if w is None:
        return None
    r = await rpc(s, "getTokenAccountsByOwner",
                  [w.address, {"mint": mint}, {"encoding": "jsonParsed"}])
    if not isinstance(r, dict) or "result" not in r:
        return None                            # RPC refused, not "holds none"
    accs = (r.get("result") or {}).get("value")
    if accs is None:
        return None
    if not accs:
        return 0
    return int(accs[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])


def programs_in(tx_b64):
    """Every top-level program a transaction calls. Used to check our own work."""
    tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    keys = list(tx.message.account_keys)
    out = []
    for ix in tx.message.instructions:
        if ix.program_id_index < len(keys):
            out.append(str(keys[ix.program_id_index]))
    return out


async def tokens_from_tx(s, sig, mint, wallet=None):
    """What this transaction actually credited us, from its own record.

    The transaction knows exactly what it did; our token account balance is a
    separate read that can lag behind a transaction the same node just called
    confirmed. Ask the authoritative source. Returns None if the record isn't
    available yet, 0 if the swap errored on-chain.
    """
    r = await rpc(s, "getTransaction",
                  [sig, {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"}])
    res = r.get("result")
    if not res:
        return None
    meta = res.get("meta") or {}
    if meta.get("err"):
        return 0

    def total(key):
        return sum(int(b["uiTokenAmount"]["amount"])
                   for b in (meta.get(key) or [])
                   if b.get("mint") == mint and b.get("owner") == _w(wallet).address)
    return total("postTokenBalances") - total("preTokenBalances")


async def buy(s, mint, sol_amount, wallet=None,
              slippage_bps=None, priority_lamps=None, fee_bps=None):
    """Spend `sol_amount` SOL on `mint`. Returns (sig, tokens_received, error).

    THE RULE HERE, learned expensively: once a signature exists and the chain
    hasn't rejected it, this NEVER reports failure. It previously re-read the
    token balance immediately after confirmation and called a zero delta
    "no_tokens_received" — but the RPC hadn't caught up with a transaction it
    had just confirmed. A real purchase was reported as "no money spent", the
    caller dropped the session, and a live position was left with no stop and
    no take-profit, invisible even to restart reconciliation.

    So: the transaction record is the source of truth, the balance is a
    fallback, and the quote is a last-resort estimate. An unverified amount is
    a reporting problem. Pretending the trade didn't happen is a money problem.
    """
    w = _w(wallet)
    if w is None:
        return None, 0, "no_wallet"
    # None means unreadable. Treated as "held none" only for this comparison:
    # the fallbacks below verify against the transaction itself, which is the
    # stronger source anyway.
    before = await token_balance(s, mint, wallet=wallet)
    if before is None:
        before = 0

    import ultra as U
    # `is not None`, not truthiness: 0 is a real answer here — an exempt
    # account — and must not fall through to the default rate.
    bps = C.FEE_BPS if fee_bps is None else int(fee_bps)
    sig, got, err = await U.swap(s, WSOL, mint, int(sol_amount * 1e9), w,
                                 C.JUP_API_KEY, referral=C.REFERRAL_ACCOUNT,
                                 referral_fee_bps=bps, slippage_bps=slippage_bps,
                                 priority_lamps=priority_lamps)
    if got > 0 and not err:
        # /execute confirmed on chain and reported what actually arrived. This
        # is the normal path; everything below is for when it doesn't.
        return sig, got, None
    if not sig:
        return None, 0, err or "send_failed"      # nothing was ever broadcast
    if err and err.startswith("execute_failed"):
        return None, 0, err                       # Jupiter says it never landed

    # 1. ask the transaction itself
    got = await tokens_from_tx(s, sig, mint, wallet=wallet)
    if got == 0:
        return None, 0, "onchain_error:rejected"
    if got:
        return sig, got, None

    # 2. give the balance a few seconds to catch up
    for _ in range(10):
        await asyncio.sleep(1.5)
        after = await token_balance(s, mint, wallet=wallet)
        if after is not None and after > before:
            return sig, after - before, None
        got = await tokens_from_tx(s, sig, mint, wallet=wallet)
        if got:
            return sig, got, None

    # 3. We cannot measure it. Before assuming a position exists, find out
    #    whether the transaction is actually ON CHAIN.
    #
    #    The previous version reasoned "a signature is proof it exists". It
    #    isn't. A Solana signature is derived from the transaction bytes, so
    #    you hold one the moment you sign — landing has nothing to do with it.
    #    On a wallet with no gas nothing ever landed, and this line invented
    #    three positions the chain has no record of, one of which then "sold"
    #    at 1.5x and booked a profit that was never earned.
    st = await rpc(s, "getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
    landed = ((st.get("result") or {}).get("value") or [None])[0]
    if not landed:
        print(f"BUY NEVER LANDED {mint[:12]}… sig={sig} — no on-chain record, "
              f"treating as no trade.", flush=True)
        return None, 0, "not_confirmed"
    if landed.get("err"):
        return None, 0, f"onchain_error:{landed['err']}"

    # It is genuinely on chain and we still can't read the amount out of it.
    # Holding on the quote's estimate is right here: the position is real, only
    # the size is uncertain, and losing track of a real position means no stop.
    print(f"BUY LANDED BUT UNVERIFIED {mint[:12]}… sig={sig} — confirmed on "
          f"chain, holding on the quote's estimate.", flush=True)
    est = await quote(s, WSOL, mint, int(sol_amount * 1e9), slippage_bps=slippage_bps)
    return sig, int(est.get("outAmount") or 0), None


async def sell(s, mint, raw_amount, wallet=None,
               slippage_bps=None, priority_lamps=None, fee_bps=None):
    """Sell `raw_amount` raw tokens back to SOL. Returns (sig, out_lamports, error)."""
    if raw_amount <= 0:
        return None, 0, "nothing_to_sell"
    w = _w(wallet)
    if w is None:
        return None, 0, "no_wallet"

    import ultra as U
    bps = C.FEE_BPS if fee_bps is None else int(fee_bps)
    sig, out, err = await U.swap(s, mint, WSOL, int(raw_amount), w,
                                 C.JUP_API_KEY, referral=C.REFERRAL_ACCOUNT,
                                 referral_fee_bps=bps, slippage_bps=slippage_bps,
                                 priority_lamps=priority_lamps)
    if out > 0 and not err:
        return sig, out, None                     # confirmed, amount reported
    if not sig:
        return None, 0, err or "no_signature"     # nothing was ever broadcast
    if err and err.startswith("execute_failed"):
        return None, 0, err                       # Jupiter says it never landed

    # A signature is not a sale. Same lesson as buy(): it is derived from the
    # transaction bytes, so signing produces one whether or not anything lands.
    # This used to swallow "unconfirmed" and hand back the quote's estimate,
    # which reported profit that was never earned and left the position live
    # with nothing managing its exit.
    st = await rpc(s, "getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
    landed = ((st.get("result") or {}).get("value") or [None])[0]
    if not landed:
        print(f"SELL NEVER LANDED {mint[:12]}… sig={sig} — no on-chain record, "
              f"still holding.", flush=True)
        return None, 0, "not_confirmed"
    if landed.get("err"):
        return None, 0, f"onchain_error:{landed['err']}"

    # Landed. Prefer what actually arrived over what was quoted — the two
    # differ by slippage, and the journal should record the real number.
    got = await _wsol_delta(s, sig, wallet)
    if got is None:
        # The sale is confirmed; only the amount is unknown. Reporting 0 here
        # would journal a total loss on a trade that actually paid out.
        est = await quote(s, mint, WSOL, int(raw_amount))
        got = int(est.get("outAmount", 0) or 0)
    return sig, got, None


async def _wsol_delta(s, sig, wallet=None):
    """WSOL lamports this transaction actually added to our account.

    None if it can't be read, in which case the caller falls back to the quote
    — the sale is confirmed either way, only the amount is uncertain.
    """
    w = _w(wallet)
    if w is None:
        return None
    try:
        tx = await rpc(s, "getTransaction",
                       [sig, {"maxSupportedTransactionVersion": 0,
                              "encoding": "jsonParsed"}])
        meta = ((tx.get("result") or {}).get("meta")) or {}
        def bal(rows):
            for b in rows or []:
                if b.get("owner") == w.address and b.get("mint") == str(WSOL):
                    return int(b["uiTokenAmount"]["amount"])
            return None
        pre, post = bal(meta.get("preTokenBalances")), bal(meta.get("postTokenBalances"))
        if pre is None or post is None:
            return None
        return max(0, post - pre)
    except Exception:
        return None


async def unwrap_wsol(s, wallet=None, min_sol=0.0002):
    """Close the wallet's wrapped-SOL account so the proceeds become spendable.

    Returns (unwrapped_sol, error). A no-op when there is no wrapped account or
    the amount is dust not worth a signature.

    CloseAccount pays out to the account's OWNER, so this can only ever move
    money to the person who already holds it.
    """
    w = _w(wallet)
    if w is None:
        return 0.0, "no_wallet"
    acct = ata(w.address, WSOL)
    r = await rpc(s, "getTokenAccountBalance", [acct])
    v = (r.get("result") or {}).get("value")
    if not v:
        return 0.0, None                       # nothing wrapped, nothing to do
    amount = int(v.get("amount") or 0) / 1e9
    if amount < min_sol:
        return 0.0, None

    from solders.pubkey import Pubkey
    from solders.instruction import Instruction, AccountMeta
    from solders.message import MessageV0
    from solders.hash import Hash
    from solders.signature import Signature

    owner = Pubkey.from_string(w.address)
    ix = Instruction(TOKEN_PROGRAM, bytes([9]),           # SPL CloseAccount
                     [AccountMeta(Pubkey.from_string(acct), False, True),
                      AccountMeta(owner, False, True),
                      AccountMeta(owner, True, False)])
    bh = await rpc(s, "getLatestBlockhash", [{"commitment": "finalized"}])
    blockhash = Hash.from_string(bh["result"]["value"]["blockhash"])
    msg = MessageV0.try_compile(owner, [ix], [], blockhash)
    tx = VersionedTransaction.populate(msg, [Signature.default()])
    signed, err = await w.sign(s, base64.b64encode(bytes(tx)).decode())
    if err:
        return 0.0, err
    send = await rpc(s, "sendTransaction",
                     [signed, {"encoding": "base64", "skipPreflight": True, "maxRetries": 5}])
    if not send.get("result"):
        return 0.0, f"send_failed:{str(send.get('error'))[:120]}"
    return amount, None


async def trading_balance(s, wallet=None):
    """The trading balance, in SOL — which is now the NATIVE balance.

    It used to be the wrapped one, because the bot could only spend WSOL. Swap
    v2 spends native SOL and returns sell proceeds native, so wrapping stopped
    being a step: a wrapped balance is now simply unspendable, which is what
    made a funded wallet report "Insufficient funds".
    """
    w = _w(wallet)
    if w is None:
        return None
    r = await rpc(s, "getBalance", [w.address])
    v = (r.get("result") or {}).get("value")
    return (int(v) / 1e9) if v is not None else None


# ── the one entry point the bot calls ───────────────────────────────────────
# Every wallet kind now takes the same route: Jupiter builds the transaction,
# the wallet signs it, Jupiter lands it. The split that used to live here — a
# server-built path for local keys, a hand-assembled one for Privy — existed
# only so we could insert a fee and a gas sponsor ourselves, and both of those
# are Jupiter's job now.

async def exit_proceeds(s, mint, wallet=None, limit=40, since=None):
    """(sol_received, tokens_sold) for sells of `mint`, or (None, None).

    None means THE CHAIN COULD NOT BE READ — not that nothing was sold. The
    caller must treat those differently: writing a zero because an RPC timed
    out records a total loss on a trade that may have been profitable, which is
    exactly what happened once.

    `since` scopes it to sells after the position was opened. Without it this
    summed every sell of the mint the wallet had ever made, so a coin traded
    twice reported both exits against one position.
    """
    w = _w(wallet)
    if w is None:
        return None, None
    owner = w.address

    sigs = await rpc(s, "getSignaturesForAddress", [owner, {"limit": limit}])
    if not isinstance(sigs, dict) or "result" not in sigs:
        return None, None                      # RPC refused — not "no sells"
    rows = sigs.get("result")
    if rows is None:
        return None, None
    got_sol, sold_tokens = 0.0, 0

    for entry in rows:
        sig = entry.get("signature")
        if not sig or entry.get("err"):
            continue
        # Newest first, so anything older than the position is out of scope and
        # so is everything after it. Stopping here turns a 40-transaction scan
        # into a handful and keeps the RPC answering.
        if since and (entry.get("blockTime") or 0) < since:
            break
        tx = (await rpc(s, "getTransaction",
                        [sig, {"maxSupportedTransactionVersion": 0,
                               "encoding": "jsonParsed"}])).get("result")
        if not tx:
            continue
        meta = tx.get("meta") or {}
        pre = {b["accountIndex"]: b for b in meta.get("preTokenBalances") or []}
        post = {b["accountIndex"]: b for b in meta.get("postTokenBalances") or []}

        # Native lamports moved by this transaction, which is where sell
        # proceeds land now. Net of the fee, so it is what actually arrived.
        sol_delta = 0.0
        keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
        for i, k in enumerate(keys):
            pk = k.get("pubkey") if isinstance(k, dict) else k
            if pk != owner:
                continue
            preb, postb = meta.get("preBalances") or [], meta.get("postBalances") or []
            if i < len(preb) and i < len(postb):
                sol_delta = (postb[i] - preb[i]) / 1e9
            break

        coin_delta, wsol_delta = 0.0, 0.0
        for i in set(pre) | set(post):
            b = post.get(i) or pre.get(i)
            if b.get("owner") != owner:
                continue
            a = float((pre.get(i, {}).get("uiTokenAmount") or {}).get("uiAmount") or 0)
            c = float((post.get(i, {}).get("uiTokenAmount") or {}).get("uiAmount") or 0)
            if b.get("mint") == mint:
                coin_delta += c - a
            elif b.get("mint") == WSOL:
                wsol_delta += c - a

        # only sells: our balance of the coin went DOWN, and SOL came back in
        # whichever form. Wrapped for anything sold before the native switch,
        # native for everything since.
        proceeds = wsol_delta + max(0.0, sol_delta)
        if coin_delta < 0 and proceeds > 0:
            got_sol += proceeds
            sold_tokens += int(round(-coin_delta * 1e6))

    return got_sol, sold_tokens

async def tx_fee(s, sig):
    """What the chain actually charged for `sig`, in SOL.

    meta.fee is the whole cost of landing it: the per-signature base fee plus
    whatever priority tip was attached. Returns None when the transaction cannot
    be read, so the caller can fall back rather than record a zero — a missing
    fee booked as free is a small lie that compounds across a journal.
    """
    if not sig:
        return None
    try:
        r = await rpc(s, "getTransaction",
                      [str(sig), {"maxSupportedTransactionVersion": 0,
                                  "commitment": "confirmed"}])
        fee = (((r or {}).get("result") or {}).get("meta") or {}).get("fee")
        return (fee / 1e9) if isinstance(fee, int) else None
    except Exception:
        return None


async def execute_buy(s, mint, sol_amount, wallet=None,
                      slippage_bps=None, priority_lamps=None, fee_bps=None):
    """Spend SOL (or WSOL) on `mint`. Returns (tokens, error, gas_sol)."""
    w = _w(wallet)
    # Whose money is this? Logged because the call chain does not make it
    # obvious, and "which wallet did that trade come out of" is not a question
    # to answer by reading code.
    print(f"EXEC BUY {mint[:10]}… size={sol_amount} "
          f"wallet={'PASSED ' + str(getattr(w, 'address', None))[:14] if wallet is not None else 'FELL BACK TO ' + str(getattr(w, 'address', None))[:14]}",
          flush=True)
    if w is None:
        return 0, "no_wallet", None
    sig, got, err = await buy(s, mint, sol_amount, wallet=w,
                              slippage_bps=slippage_bps,
                              priority_lamps=priority_lamps,
                              fee_bps=fee_bps)
    if err:
        return got, err, None
    return got, None, await tx_fee(s, sig)


async def execute_sell(s, mint, raw_amount, wallet=None,
                       slippage_bps=None, priority_lamps=None, fee_bps=None):
    """Sell tokens back to SOL/WSOL. Returns (sol_out, error, gas_sol)."""
    w = _w(wallet)
    if w is None:
        return 0.0, "no_wallet", None
    sig, out, err = await sell(s, mint, raw_amount, wallet=w,
                               slippage_bps=slippage_bps,
                               priority_lamps=priority_lamps,
                               fee_bps=fee_bps)
    if err:
        return 0.0, err, None
    return out / 1e9, None, await tx_fee(s, sig)
