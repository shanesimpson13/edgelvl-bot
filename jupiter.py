"""
jupiter.py — price + execution. Both through Jupiter, which routes across every
Solana venue and always takes the best price. Free, no API key.

Price comes from the same quote endpoint we trade through, so the number the
strategy sees is the number you'd actually get filled at — impact included.
"""
import asyncio
import base64
import time

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
WALLET = privy.build(kp)
ME = WALLET.address if WALLET else None


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


async def _paced_get(s, url, headers=None):
    """One shared throttle for every Jupiter call, plus loud rate-limit handling."""
    global _last_req, _rate_limited_until, _warned

    async with _gate:
        gap = 1.0 / max(C.JUP_MAX_RPS, 0.1)
        wait = _last_req + gap - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_req = time.monotonic()

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


async def quote(s, in_mint, out_mint, amount, slippage_bps=None, with_fee=False):
    """A route for this swap.

    with_fee applies the platform fee. Deliberately OFF for the price-probe
    quotes the strategy watches with — a fee would skew every price reading —
    and ON only for quotes that become real swaps.
    """
    url = (f"{JUP}/quote?inputMint={in_mint}&outputMint={out_mint}"
           f"&amount={int(amount)}&slippageBps={slippage_bps or C.SLIPPAGE_BPS}"
           f"&restrictIntermediateTokens=true")
    if with_fee and C.FEE_ACCOUNT and C.FEE_BPS > 0:
        url += f"&platformFeeBps={C.FEE_BPS}"
    q, err = await _paced_get(s, url, _headers())
    return q


async def get_price(s, mint):
    """SOL per token, measured with a fixed probe so every reading is comparable.

    Returns None if the coin has no route (illiquid / not migrated yet) — the
    caller should just skip that poll rather than treat it as a price of zero.
    """
    q = await quote(s, WSOL, mint, int(C.PROBE_SOL * 1e9))
    out = int(q.get("outAmount", 0) or 0)
    if out <= 0:
        return None
    decimals = 6  # pump.fun standard
    return (C.PROBE_SOL) / (out / 10 ** decimals)


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


async def token_balance(s, mint):
    """Raw token balance held by our wallet (0 if we hold none)."""
    r = await rpc(s, "getTokenAccountsByOwner",
                  [ME, {"mint": mint}, {"encoding": "jsonParsed"}])
    accs = (r.get("result") or {}).get("value") or []
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


async def _send(s, q):
    """Build a swap from a quote, sign it, send it, wait for confirmation."""
    body = {
        "quoteResponse": q,
        "userPublicKey": ME,
        # A Privy wallet can't wrap SOL — the policy has no System Program — so
        # its swaps spend WSOL directly. A local key wraps as normal.
        "wrapAndUnwrapSol": WALLET.wrap_sol,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": {
            "priorityLevelWithMaxLamports": {
                "maxLamports": C.PRIORITY_LAMPS, "priorityLevel": "high"}},
    }
    # The quote carries platformFee; the swap build needs the account to pay it to.
    if C.FEE_ACCOUNT and q.get("platformFee"):
        body["feeAccount"] = C.FEE_ACCOUNT
    async with s.post(f"{JUP}/swap", json=body) as r:
        sj = await r.json()
    if not sj.get("swapTransaction"):
        return None, f"build_failed:{str(sj)[:80]}"

    # Check before asking Privy, not because Privy would let it through, but so
    # a build that drifts out of policy fails here with a readable reason
    # instead of an opaque refusal. Belt and braces, cheap.
    if not WALLET.wrap_sol:
        stray = [p for p in programs_in(sj["swapTransaction"])
                 if p not in privy.SWAP_PROGRAMS]
        if stray:
            return None, f"unexpected_program:{stray[0][:12]}"

    b64, err = await WALLET.sign(s, sj["swapTransaction"])
    if err:
        return None, err

    send = await rpc(s, "sendTransaction",
                     [b64, {"encoding": "base64", "skipPreflight": True, "maxRetries": 5}])
    sig = send.get("result")
    if not sig:
        return None, f"send_failed:{str(send.get('error'))[:80]}"

    # confirm — up to ~60s
    import asyncio
    for _ in range(40):
        await asyncio.sleep(1.5)
        st = await rpc(s, "getSignatureStatuses", [[sig]])
        v = ((st.get("result") or {}).get("value") or [None])[0]
        if v and v.get("confirmationStatus") in ("confirmed", "finalized"):
            return (None, f"onchain_error:{v['err']}") if v.get("err") else (sig, None)
    return sig, "unconfirmed"


async def buy(s, mint, sol_amount):
    """Spend `sol_amount` SOL on `mint`. Returns (sig, tokens_received, error)."""
    q = await quote(s, WSOL, mint, int(sol_amount * 1e9))
    if not q.get("outAmount"):
        return None, 0, "no_route"
    before = await token_balance(s, mint)
    sig, err = await _send(s, q)
    if err and err != "unconfirmed":
        return None, 0, err
    after = await token_balance(s, mint)
    got = after - before
    if got <= 0:
        return None, 0, "no_tokens_received"
    return sig, got, None


async def sell(s, mint, raw_amount):
    """Sell `raw_amount` raw tokens back to SOL. Returns (sig, out_lamports, error)."""
    if raw_amount <= 0:
        return None, 0, "nothing_to_sell"
    q = await quote(s, mint, WSOL, int(raw_amount), with_fee=True)
    out = int(q.get("outAmount", 0) or 0)
    if out <= 0:
        return None, 0, "no_route"
    sig, err = await _send(s, q)
    return sig, out, (err if err and err != "unconfirmed" else None)


async def wsol_balance(s):
    """The trading balance, in SOL.

    On a Privy wallet this is the number that matters — native SOL sitting in
    the account can't be spent by the bot, only wrapped WSOL can. Returns None
    if the account doesn't exist yet (nothing has been deposited or wrapped).
    """
    if not ME:
        return None
    r = await rpc(s, "getTokenAccountBalance", [ata(ME, WSOL)])
    v = (r.get("result") or {}).get("value")
    return (int(v["amount"]) / 1e9) if v else None


# ── the one entry point the bot calls ───────────────────────────────────────
# Local keypairs keep using Jupiter Ultra: it routes, submits and confirms in
# one call, and it's the path that's been trading. Ultra builds the transaction
# server-side though, so it always wraps SOL — which a Privy wallet's policy
# refuses. Privy therefore goes through the swap API, where we control the build.
async def execute_buy(s, mint, sol_amount):
    """Spend SOL (or WSOL) on `mint`. Returns (tokens_received, error)."""
    if WALLET is None:
        return 0, "no_wallet"
    if WALLET.kind == "local":
        import ultra as U
        sig, got = await U.ultra_swap(s, WSOL, mint, int(sol_amount * 1e9),
                                      WALLET.kp, C.JUP_API_KEY)
        return (got, None) if (sig and got > 0) else (0, "swap_failed")
    sig, got, err = await buy(s, mint, sol_amount)
    return (got, err) if err else (got, None)


async def execute_sell(s, mint, raw_amount):
    """Sell tokens back to SOL/WSOL. Returns (sol_out, error)."""
    if WALLET is None:
        return 0.0, "no_wallet"
    if WALLET.kind == "local":
        import ultra as U
        sig, out = await U.ultra_swap(s, mint, WSOL, int(raw_amount),
                                      WALLET.kp, C.JUP_API_KEY)
        return (out / 1e9, None) if sig else (0.0, "swap_failed")
    sig, out, err = await sell(s, mint, raw_amount)
    return (0.0, err) if err else (out / 1e9, None)
