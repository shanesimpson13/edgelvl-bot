"""Robinhood Chain executor — the same contract jupiter.py exposes, over EVM.

bot.py, strategy.py and copywatch.py are chain-neutral: they ask an executor for
a price, a balance, a buy and a sell, and never learn which chain answered. This
module answers for Robinhood Chain so none of them had to change.

Three differences from the Solana path are worth knowing, because each one is a
place a naive port breaks:

  * SELLING NEEDS AN APPROVAL FIRST. ERC-20 spending is opt-in per spender, so a
    sell is two transactions the first time. Solana has no equivalent and a port
    that forgets it sees every first sell revert.
  * GAS IS PAID IN ETH, SEPARATELY FROM THE TRADE. A wallet holding only tokens
    cannot sell them. The stop is what dies when that happens, so trading_balance
    is what the caller must watch, not the token balance.
  * AMOUNTS ARE PER-TOKEN DECIMALS, not a fixed 9. Reading decimals from chain
    rather than assuming 18 is the difference between a 0.01 buy and a 10,000,000
    one.

Routing is LI.FI: it aggregates the RH venues (Uniswap, fly, and the launchpad
pools) behind one quote, and it returns a ready-to-send transaction, so this file
never has to know which DEX filled. 0x and 1inch cover the chain too but both
require a paid key; LI.FI does not, which is the only reason it is first.
"""

import asyncio
import json
import time

import aiohttp

import config as C

RPC_URL   = getattr(C, "RH_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
CHAIN_ID  = int(getattr(C, "RH_CHAIN_ID", 4663))
LIFI      = getattr(C, "LIFI_API", "https://li.quest/v1")
LIFI_KEY  = getattr(C, "LIFI_API_KEY", "")

# LI.FI echoes the zero address back inside its own quotes, but only ROUTES from
# native when given the 0xEeee sentinel -- a zero fromToken is filtered out with
# "no available quotes for the requested transfer". Sells resolved either way,
# so zero looked correct right up until the first buy failed.
NATIVE = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"

PRICE_TTL = float(getattr(C, "PRICE_TTL", 0.9))
SLIPPAGE  = float(getattr(C, "RH_SLIPPAGE", 0.15))
NO_ROUTE  = "no_route"      # fraction, LI.FI wants 0.15 not 1500

# selector constants
SEL_BALANCE_OF = "0x70a08231"
SEL_DECIMALS   = "0x313ce567"
SEL_ALLOWANCE  = "0xdd62ed3e"
SEL_APPROVE    = "0x095ea7b3"
MAX_UINT       = (1 << 256) - 1

_px_cache: dict = {}
_dec_cache: dict = {}
_held: set = set()


# ── plumbing ────────────────────────────────────────────────────────────────
async def rpc(s, method, params):
    """Minimal EVM JSON-RPC call. Same shape as jupiter.rpc so callers port."""
    async with s.post(RPC_URL, json={"jsonrpc": "2.0", "id": 1,
                                     "method": method, "params": params},
                      timeout=aiohttp.ClientTimeout(total=20)) as r:
        return await r.json()


def _hex(n):
    return hex(int(n))


def _pad(addr_or_int):
    """32-byte ABI word."""
    if isinstance(addr_or_int, str):
        return addr_or_int.lower().replace("0x", "").rjust(64, "0")
    return format(int(addr_or_int), "064x")


def _w(wallet):
    """The wallet to act as: the one passed, or the environment's."""
    return wallet if wallet is not None else getattr(C, "RH_WALLET", None)


def hold(mint):
    """This mint now has money in it — its price jumps the queue."""
    _held.add(mint)


def release(mint):
    _held.discard(mint)


# ── reads ───────────────────────────────────────────────────────────────────
async def decimals(s, mint):
    """Token decimals, read once per mint.

    Assumed 18 by most EVM code and wrong often enough to matter: a token with
    6 decimals sized as though it had 18 is a trade a million times too small,
    which reads as a failed swap rather than a bad number.
    """
    if mint in _dec_cache:
        return _dec_cache[mint]
    try:
        r = await rpc(s, "eth_call", [{"to": mint, "data": SEL_DECIMALS}, "latest"])
        d = int(r["result"], 16)
        if 0 <= d <= 36:
            _dec_cache[mint] = d
            return d
    except Exception:
        pass
    return None                      # caller decides; never silently assume 18


async def token_balance(s, mint, wallet=None):
    """Raw token balance held by the wallet, or None if it cannot be read.

    None is not zero. A failed read that returned 0 would look exactly like a
    position that had already been sold, and the stop path treats those
    differently -- that mix-up is what left a phantom position open on Solana.
    """
    w = _w(wallet)
    if w is None:
        return None
    try:
        data = SEL_BALANCE_OF + _pad(w.address)
        r = await rpc(s, "eth_call", [{"to": mint, "data": data}, "latest"])
        return int(r["result"], 16)
    except Exception:
        return None


async def trading_balance(s, wallet=None):
    """Native ETH, in ether. This is both the trading asset and the gas.

    On Solana those are the same token by accident; here it is by design, and a
    wallet that spends its last ETH on a buy cannot pay to sell.
    """
    w = _w(wallet)
    if w is None:
        return 0.0
    try:
        r = await rpc(s, "eth_getBalance", [w.address, "latest"])
        return int(r["result"], 16) / 1e18
    except Exception:
        return 0.0


# ── routing ─────────────────────────────────────────────────────────────────
def _hdrs():
    return {"x-lifi-api-key": LIFI_KEY} if LIFI_KEY else {}


async def quote(s, from_token, to_token, amount_raw, taker, slippage=None):
    """A LI.FI route, including a ready-to-send transactionRequest.

    Returns (quote_dict, error). Same-chain only: fromChain == toChain, because
    a cross-chain route would settle minutes later and this is a scalper.
    """
    params = {
        "fromChain": CHAIN_ID, "toChain": CHAIN_ID,
        "fromToken": from_token, "toToken": to_token,
        "fromAmount": str(int(amount_raw)),
        "fromAddress": taker,
        "slippage": str(slippage if slippage is not None else SLIPPAGE),
    }
    try:
        async with s.get(f"{LIFI}/quote", params=params, headers=_hdrs(),
                         timeout=aiohttp.ClientTimeout(total=20)) as r:
            body = await r.json()
            if r.status != 200:
                # LI.FI answers 404 both for a token it cannot route at all and
                # for a size this pool cannot absorb. Same status, different
                # problem -- name the second one so callers can react to it
                # instead of reporting a dead market.
                if r.status == 404 and "no available quotes" in str(body).lower():
                    return None, NO_ROUTE
                return None, f"lifi_{r.status}:{str(body)[:160]}"
            return body, None
    except Exception as e:
        return None, f"lifi_exc:{type(e).__name__}"


async def get_price(s, mint):
    """ETH per token, via a small probe quote. Cached like the Solana path.

    Priced by selling a nominal amount rather than buying one: the sell side is
    the one the stop has to execute, and on a thin book the two are not the same
    number. Quoting the side we do not trade would flatter every stop.
    """
    hit = _px_cache.get(mint)
    if hit and time.time() - hit[0] < PRICE_TTL:
        return hit[1]
    d = await decimals(s, mint)
    if d is None:
        return None
    probe = 10 ** d                                    # one whole token
    w = _w(None)
    taker = w.address if w else "0x0000000000000000000000000000000000000001"
    q, err = await quote(s, mint, NATIVE, probe, taker)
    if err or not q:
        return _px_cache.get(mint, (0, None))[1]       # stale beats nothing
    try:
        out = int((q.get("estimate") or {}).get("toAmount") or 0)
    except (TypeError, ValueError):
        return None
    if out <= 0:
        return None
    px = out / 1e18
    _px_cache[mint] = (time.time(), px)
    return px


async def mc_scale(s, mint, supply=None):
    """Multiply an ETH-per-token price by this to get a market cap in dollars.

    supply x ETH/USD. Supply comes from the caller's board row when it has one
    -- the row carries mcap and price for the same instant, so supply is
    mcap/price and costs no call. Falls back to totalSupply on chain.
    """
    if supply is None:
        d = await decimals(s, mint)
        try:
            r = await rpc(s, "eth_call", [{"to": mint, "data": "0x18160ddd"}, "latest"])
            supply = int(r["result"], 16) / (10 ** (d if d is not None else 18))
        except Exception:
            return None
    usd = await eth_usd(s)
    return (supply * usd) if (supply and usd) else None


_eth_usd = [0.0, None]


async def eth_usd(s, ttl=60.0):
    """ETH in dollars, from LI.FI's own token record. Shared across coins."""
    now = time.time()
    if _eth_usd[1] and now - _eth_usd[0] < ttl:
        return _eth_usd[1]
    try:
        async with s.get(f"{LIFI}/token", params={"chain": CHAIN_ID, "token": NATIVE},
                         headers=_hdrs(), timeout=aiohttp.ClientTimeout(total=15)) as r:
            d = await r.json()
        px = float(d.get("priceUSD") or 0)
        if px:
            _eth_usd[0], _eth_usd[1] = now, px
    except Exception:
        pass
    return _eth_usd[1]


# ── writing ─────────────────────────────────────────────────────────────────
async def _send(s, w, tx):
    """Sign and broadcast one transaction. Returns (tx_hash, error).

    An exception after broadcast is never treated as "it did not happen": the
    hash is returned wherever we have one, so the caller can resolve the outcome
    on chain instead of assuming and double-spending.
    """
    try:
        nonce = int((await rpc(s, "eth_getTransactionCount",
                               [w.address, "pending"]))["result"], 16)
        gas_price = int((await rpc(s, "eth_gasPrice", []))["result"], 16)
    except Exception as e:
        return None, f"prep_failed:{type(e).__name__}"

    body = {
        "from": w.address,
        "to": tx["to"],
        "data": tx.get("data", "0x"),
        "value": _hex(int(tx.get("value", 0) or 0)),
        "nonce": _hex(nonce),
        "gasPrice": _hex(int(tx.get("gasPrice") or gas_price)),
        "chainId": _hex(CHAIN_ID),
    }
    gl = tx.get("gasLimit") or tx.get("gas")
    if gl:
        body["gas"] = _hex(int(gl, 16) if isinstance(gl, str) and gl.startswith("0x") else int(gl))
    else:
        try:
            est = await rpc(s, "eth_estimateGas", [{k: v for k, v in body.items()
                                                    if k in ("from", "to", "data", "value")}])
            body["gas"] = _hex(int(int(est["result"], 16) * 1.25))
        except Exception:
            body["gas"] = _hex(600000)

    signed, err = await w.sign_tx(s, body)
    if err:
        return None, err
    try:
        r = await rpc(s, "eth_sendRawTransaction", [signed])
        if "error" in r:
            return None, f"send_failed:{str(r['error'])[:140]}"
        return r.get("result"), None
    except Exception as e:
        return None, f"send_exc:{type(e).__name__}"


async def _receipt(s, tx_hash, timeout=90.0):
    """Wait for a receipt. Returns (receipt, error)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = await rpc(s, "eth_getTransactionReceipt", [tx_hash])
            got = r.get("result")
            if got:
                return got, None
        except Exception:
            pass
        await asyncio.sleep(1.0)
    return None, "receipt_timeout"


def _gas_eth(receipt):
    try:
        return (int(receipt["gasUsed"], 16) * int(receipt["effectiveGasPrice"], 16)) / 1e18
    except Exception:
        return None


async def _ensure_allowance(s, w, mint, spender, need):
    """Approve `spender` for this token if it is not already approved.

    Solana has no analogue, so this is the step a port silently omits and then
    watches every first sell revert. Approves max once rather than per-trade:
    an approval per sell is a second transaction and a second gas fee on every
    exit, and the exit is the leg that is already fragile.
    """
    try:
        data = SEL_ALLOWANCE + _pad(w.address) + _pad(spender)
        r = await rpc(s, "eth_call", [{"to": mint, "data": data}, "latest"])
        if int(r["result"], 16) >= need:
            return None
    except Exception:
        pass
    h, err = await _send(s, w, {"to": mint,
                                "data": SEL_APPROVE + _pad(spender) + _pad(MAX_UINT),
                                "value": 0})
    if err:
        return f"approve_failed:{err}"
    rec, err = await _receipt(s, h)
    if err:
        return f"approve_unconfirmed:{err}"
    if int(rec.get("status", "0x0"), 16) != 1:
        return "approve_reverted"
    return None


async def _swap(s, mint_in, mint_out, amount_raw, wallet, slippage_bps=None):
    """One swap. Returns (out_raw, tx_hash, gas_eth, error)."""
    w = _w(wallet)
    if w is None:
        return 0, None, None, "no_wallet"
    slip = (slippage_bps / 10000.0) if slippage_bps else None
    q, err = await quote(s, mint_in, mint_out, amount_raw, w.address, slip)
    if err or not q:
        return 0, None, None, err or "no_route"

    tx = q.get("transactionRequest") or {}
    if not tx.get("to"):
        return 0, None, None, "no_transaction_request"

    if mint_in != NATIVE:
        aerr = await _ensure_allowance(s, w, mint_in, tx["to"], int(amount_raw))
        if aerr:
            return 0, None, None, aerr

    before = await token_balance(s, mint_out, wallet=w) if mint_out != NATIVE else None
    h, err = await _send(s, w, tx)
    if err:
        return 0, None, None, err
    rec, err = await _receipt(s, h)
    if err:
        # Submitted, outcome unknown. Hand back the hash so the caller can
        # resolve it rather than assuming nothing happened.
        return 0, h, None, f"unresolved:{err}"
    if int(rec.get("status", "0x0"), 16) != 1:
        return 0, h, _gas_eth(rec), "reverted"

    gas = _gas_eth(rec)
    if mint_out == NATIVE:
        try:
            out = int((q.get("estimate") or {}).get("toAmount") or 0)
        except (TypeError, ValueError):
            out = 0
        return out, h, gas, None
    after = await token_balance(s, mint_out, wallet=w)
    if after is None or before is None:
        try:
            return int((q.get("estimate") or {}).get("toAmount") or 0), h, gas, None
        except (TypeError, ValueError):
            return 0, h, gas, "unreadable_fill"
    return max(0, after - before), h, gas, None


async def max_routable_buy(s, mint, taker, want_eth, floor_eth=0.0002, steps=7):
    """Largest ETH buy at or below want_eth that LI.FI will actually route.

    On a thin coin a no-route 404 is usually an answer about size, not a
    missing market: the same coin quotes fine a few multiples smaller, and its
    sells route at any size. Measured on the live board, AITAX and FROGE both
    refused 0.005 ETH and filled at 0.002. Binary-searching once turns "No
    available quotes" into a number the caller can put in front of the user.

    Returns 0.0 when even floor_eth will not route, which is the real
    can't-trade-this case.
    """
    _q, err = await quote(s, NATIVE, mint, int(float(floor_eth) * 1e18), taker)
    if err:
        return 0.0

    lo, hi = float(floor_eth), float(want_eth)
    for _ in range(steps):
        if hi - lo <= float(floor_eth):
            break
        mid = (lo + hi) / 2
        _q, err = await quote(s, NATIVE, mint, int(mid * 1e18), taker)
        if err:
            hi = mid
        else:
            lo = mid
    return lo


async def execute_buy(s, mint, eth_amount, wallet=None,
                      slippage_bps=None, priority_lamps=None, fee_bps=None):
    """Buy `mint` with native ETH. Returns (tokens_raw, error, gas_eth).

    Signature mirrors jupiter.execute_buy so bot.py needs no branch: the second
    positional is the native amount either way. priority_lamps is accepted and
    ignored -- this chain prices gas, not tips.
    """
    got, _h, gas, err = await _swap(s, NATIVE, mint, int(float(eth_amount) * 1e18),
                                    wallet, slippage_bps)
    if err == NO_ROUTE:
        # Say which it is. "No route" on a coin whose sells work is a size
        # problem, and the caller can offer a size that would have worked
        # rather than telling the user the coin is untradeable.
        w = _w(wallet)
        if w is not None:
            cap = await max_routable_buy(s, mint, w.address, float(eth_amount))
            err = (f"size_too_large:max_eth={cap:.4f}" if cap > 0
                   else "no_route:untradeable")
    return (got or 0), err, gas


async def execute_sell(s, mint, raw_amount, wallet=None,
                       slippage_bps=None, priority_lamps=None, fee_bps=None):
    """Sell tokens back to native ETH. Returns (eth_out, error, gas_eth)."""
    out, _h, gas, err = await _swap(s, mint, NATIVE, int(raw_amount),
                                    wallet, slippage_bps)
    if err:
        return 0.0, err, gas
    return (out or 0) / 1e18, None, gas


async def exit_proceeds(s, mint, wallet=None, limit=40, since=None):
    """(eth_received, tokens_sold) for recent sells of `mint`, or (None, None).

    Not implemented for this chain yet. Returns None rather than zero so the
    caller falls back to its own accounting instead of journalling a sale as
    free -- a missing figure booked as zero is a lie that compounds.
    """
    return None, None


async def unwrap_wsol(s, wallet=None, min_sol=0.0002):
    """No analogue on this chain. Native ETH is already native."""
    return 0.0, None
