"""
ultra.py — swap execution through Jupiter's managed path.

Jupiter's Swap API v2 unified what used to be Ultra and Metis. `/order` returns
a finished transaction; `/execute` lands it through Jupiter's own infrastructure
— routing, slippage estimation, priority fees, sending, retries and confirmation
all happen there.

So we do three things: ask for an order, hand the transaction to the wallet to
sign, hand it back. Everything we used to own — assembling instructions, staying
under the 1232-byte packet limit, sponsoring gas, choosing a fee mint, sending,
polling for confirmation — is Jupiter's problem now. Every one of those was a
source of failed buys.

Two deliberate constraints:

  ROUTERS ARE PINNED TO METIS. v2 can also route through jupiterz, dflow and
  okx, whose programs the Privy policy does not whitelist — a customer's wallet
  would refuse to sign them, and a refusal costs a trade. Pinning keeps the
  program surface identical to what the policy already allows, so execution
  moved without touching custody. Widening it is a policy change, made on
  purpose, not a surprise at 3am.

  NOTHING RETRIES ONCE /execute HAS BEEN CALLED. Before that no transaction
  exists and retrying is free. After it, the request may have landed even if the
  reply never arrived, and a retry buys twice. An ambiguous send is reported as
  ambiguous and resolved against the chain by the caller.

THE FEE rides on `referralAccount` + `referralFee`, which Jupiter requires
together. Jupiter picks which mint to collect in, preferring SOL — which is what
makes this work identically on Token-2022 coins, where our own fee account
failed the whole swap with IncorrectTokenProgramID. Jupiter keeps 20% of it.
"""
import base64
import asyncio
import logging

import aiohttp
from solders.transaction import VersionedTransaction

log = logging.getLogger("ultra")

SWAP_BASE = "https://api.jup.ag/swap/v2"
SOL_MINT = "So11111111111111111111111111111111111111112"

# See the module docstring: these route through programs the wallet policy
# doesn't know, so we never ask for them.
EXCLUDE_ROUTERS = "jupiterz,dflow,okx"

# Jupiter's floor for a referral fee. Below this it rejects the pair outright,
# so a sub-50bps fee isn't expressible — it's all-or-nothing per wallet, which
# is exactly how the exemption list already works.
MIN_REFERRAL_BPS = 50


def _hdrs(api_key):
    h = {"accept": "application/json"}
    if api_key:
        h["x-api-key"] = api_key
    return h


async def order(session, in_mint, out_mint, amount_raw, taker, api_key,
                referral=None, referral_fee_bps=None, slippage_bps=None,
                priority_lamps=None, exclude_routers=EXCLUDE_ROUTERS):
    """GET /swap/v2/order — a quote and a finished transaction.

    Returns (order_dict, error). A 200 is not success: v2 reports "Insufficient
    funds" and similar in the body with `transaction` left null, so the presence
    of a transaction is the real test.

    With `taker` omitted it returns the routed amount and NO transaction, which
    is how we price a coin we don't hold — v2 has no separate quote endpoint,
    and /order refuses when the taker can't cover the input.
    """
    params = {
        "inputMint": in_mint,
        "outputMint": out_mint,
        "amount": str(int(amount_raw)),
    }
    if taker:
        params["taker"] = taker
    if exclude_routers:
        params["excludeRouters"] = exclude_routers
    if slippage_bps:
        params["slippageBps"] = str(int(slippage_bps))
    if priority_lamps:
        params["priorityFeeLamports"] = str(int(priority_lamps))
    # Both or neither — Jupiter rejects one without the other.
    if referral and referral_fee_bps and int(referral_fee_bps) >= MIN_REFERRAL_BPS:
        params["referralAccount"] = referral
        params["referralFee"] = str(int(referral_fee_bps))

    try:
        async with session.get(f"{SWAP_BASE}/order", params=params,
                               headers=_hdrs(api_key),
                               timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                return None, f"order_http_{r.status}:{(await r.text())[:120]}"
            d = await r.json()
    except Exception as e:
        return None, f"order_exc:{type(e).__name__}"

    if not taker:
        # Price-only call. There is nothing to sign, so a routed amount is all
        # the success there is to have.
        return (d, None) if d.get("outAmount") else (None, "no_route")
    if not d.get("transaction"):
        # The body says why, and it is usually worth reading: "Insufficient
        # funds" here means the wallet's balance is wrapped, not that it's empty.
        why = d.get("errorMessage") or d.get("error") or "no transaction returned"
        return None, f"no_order:{why}"
    if not d.get("requestId"):
        return None, "no_order:missing requestId"
    return d, None


async def execute(session, request_id, signed_tx_b64, api_key):
    """POST /swap/v2/execute — Jupiter sends it and waits for the chain.

    Returns (result_dict, error). `error` set means we do not know the outcome;
    a result dict means we do, and its `status` says which.
    """
    body = {"requestId": request_id, "signedTransaction": signed_tx_b64}
    try:
        async with session.post(f"{SWAP_BASE}/execute", json=body,
                                headers={**_hdrs(api_key),
                                         "content-type": "application/json"},
                                timeout=aiohttp.ClientTimeout(total=90)) as r:
            if r.status != 200:
                return None, f"execute_http_{r.status}:{(await r.text())[:160]}"
            return await r.json(), None
    except Exception as e:
        # The transaction may well have landed. Never treated as "didn't happen".
        return None, f"execute_exc:{type(e).__name__}"


def _signature_of(signed_tx_b64):
    """The signature of an already-signed transaction, for resolving an
    ambiguous send against the chain without having to send anything again."""
    try:
        tx = VersionedTransaction.from_bytes(base64.b64decode(signed_tx_b64))
        sig = str(tx.signatures[0])
        return None if sig.strip("1") == "" else sig      # all-1s = unsigned
    except Exception:
        return None


async def swap(session, in_mint, out_mint, amount_raw, wallet, api_key,
               referral=None, referral_fee_bps=None, slippage_bps=None,
               priority_lamps=None, order_retries=3, retry_delay=1.5):
    """order -> sign -> execute. Returns (signature, out_amount_raw, error).

    `out_amount_raw` is what actually arrived where possible: /execute reports
    the realised amounts, and only if those are missing does it fall back to the
    order's estimate. The journal should record the real number.

    On error, `signature` may still be set — meaning something was submitted and
    its fate is unknown. The caller must resolve that against the chain rather
    than assume either way.
    """
    taker = getattr(wallet, "address", None)
    if not taker:
        return None, 0, "no_wallet"

    # Retry only here, where nothing has been signed or sent yet.
    o = err = None
    for attempt in range(1, order_retries + 1):
        o, err = await order(session, in_mint, out_mint, amount_raw, taker,
                             api_key, referral=referral,
                             referral_fee_bps=referral_fee_bps,
                             slippage_bps=slippage_bps,
                             priority_lamps=priority_lamps)
        if o:
            break
        # "Insufficient funds" won't change in 1.5 seconds; a timeout might.
        if err and "Insufficient funds" in err:
            break
        if attempt < order_retries:
            await asyncio.sleep(retry_delay)
    if not o:
        return None, 0, err or "no_order"

    signed_b64, err = await wallet.sign(session, o["transaction"])
    if err:
        return None, 0, err            # nothing was sent; a refusal is final
    sig = _signature_of(signed_b64)

    result, err = await execute(session, o["requestId"], signed_b64, api_key)
    if err:
        # Submitted, outcome unknown. Hand back the signature so the caller can
        # ask the chain instead of guessing.
        return sig, 0, f"unresolved:{err}"

    status = result.get("status")
    sig = result.get("signature") or sig
    if status == "Success":
        out = (result.get("totalOutputAmount")        # after any output-mint fee
               or result.get("outputAmountResult")    # before it
               or o.get("outAmount") or 0)            # the estimate, last resort
        return sig, int(out), None

    code = result.get("code")
    why = result.get("error") or status or "unknown"
    # A Failed status from Jupiter means it did not land — that is a real answer,
    # not an ambiguous one, so the caller can safely treat it as no trade.
    return sig, 0, f"execute_failed:{why} (code={code})"


async def quote_sell_to_sol(session, mint, tokens_raw, taker, api_key):
    """What this position would fetch in SOL lamports, via /order as a quote.

    No signature and no execution — asking for an order does not commit to one.
    """
    if tokens_raw <= 0:
        return None
    o, _ = await order(session, mint, SOL_MINT, tokens_raw, taker, api_key)
    if not o:
        return None
    try:
        return int(o.get("outAmount", 0))
    except (TypeError, ValueError):
        return None
