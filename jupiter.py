"""
jupiter.py — price + execution. Both through Jupiter, which routes across every
Solana venue and always takes the best price. Free, no API key.

Price comes from the same quote endpoint we trade through, so the number the
strategy sees is the number you'd actually get filled at — impact included.
"""
import base64
import aiohttp
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

import config as C

JUP = "https://lite-api.jup.ag/swap/v1"
WSOL = "So11111111111111111111111111111111111111112"

kp = Keypair.from_base58_string(C.PRIVATE_KEY)
ME = str(kp.pubkey())


async def rpc(s, method, params):
    """Minimal Solana JSON-RPC call."""
    async with s.post(C.RPC_URL, json={"jsonrpc": "2.0", "id": 1,
                                       "method": method, "params": params}) as r:
        return await r.json()


async def quote(s, in_mint, out_mint, amount, slippage_bps=None):
    url = (f"{JUP}/quote?inputMint={in_mint}&outputMint={out_mint}"
           f"&amount={int(amount)}&slippageBps={slippage_bps or C.SLIPPAGE_BPS}"
           f"&restrictIntermediateTokens=true")
    try:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            return await r.json()
    except Exception:
        return {}


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


async def token_balance(s, mint):
    """Raw token balance held by our wallet (0 if we hold none)."""
    r = await rpc(s, "getTokenAccountsByOwner",
                  [ME, {"mint": mint}, {"encoding": "jsonParsed"}])
    accs = (r.get("result") or {}).get("value") or []
    if not accs:
        return 0
    return int(accs[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])


async def _send(s, q):
    """Build a swap from a quote, sign it, send it, wait for confirmation."""
    body = {
        "quoteResponse": q,
        "userPublicKey": ME,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": {
            "priorityLevelWithMaxLamports": {
                "maxLamports": C.PRIORITY_LAMPS, "priorityLevel": "high"}},
    }
    async with s.post(f"{JUP}/swap", json=body) as r:
        sj = await r.json()
    if not sj.get("swapTransaction"):
        return None, f"build_failed:{str(sj)[:80]}"

    raw = VersionedTransaction.from_bytes(base64.b64decode(sj["swapTransaction"]))
    signed = VersionedTransaction(raw.message, [kp])
    b64 = base64.b64encode(bytes(signed)).decode()

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
    """Sell `raw_amount` raw tokens back to SOL. Returns (sig, error)."""
    if raw_amount <= 0:
        return None, "nothing_to_sell"
    q = await quote(s, mint, WSOL, int(raw_amount))
    if not q.get("outAmount"):
        return None, "no_route"
    sig, err = await _send(s, q)
    return sig, (err if err and err != "unconfirmed" else None)
