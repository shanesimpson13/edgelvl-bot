"""
ultra.py — Jupiter Ultra: execution + accurate position pricing.

Uses api.jup.ag/ultra/v1. Unlike Jupiter Lite v1, Ultra handles transaction
submission AND on-chain confirmation inline — when execute_order returns
status="Success", the tx has already landed.

Functions:
  ultra_order(session, in_mint, out_mint, amount_raw, taker, api_key)
    → dict with requestId, transaction, outAmount. None on error.

  ultra_execute(session, request_id, signed_tx_b64, api_key)
    → dict with signature, status. status is "Success" or "Failed".

  ultra_swap(session, in_mint, out_mint, amount_raw, keypair, api_key, retries=3)
    → (signature, out_amount_raw) on success, (None, 0) on any failure.
    Handles order → sign → execute + retries.

  quote_sell_to_sol(session, mint, tokens_raw, taker, api_key)
    → int (SOL lamports out) or None. Uses /order as a sell-quote.
"""
import base64
import asyncio
import logging
import aiohttp
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

log = logging.getLogger("ultra")

ULTRA_BASE = "https://api.jup.ag/ultra/v1"
SOL_MINT = "So11111111111111111111111111111111111111112"


async def ultra_order(session, in_mint, out_mint, amount_raw, taker, api_key, referral=None, referral_fee_bps=50):
    """GET /ultra/v1/order — returns {requestId, transaction, outAmount} or None."""
    params = {
        "inputMint": in_mint,
        "outputMint": out_mint,
        "amount": str(amount_raw),
        "taker": taker,
    }
    if referral:
        params["referralAccount"] = referral
        params["referralFee"] = str(referral_fee_bps)
    try:
        async with session.get(
            f"{ULTRA_BASE}/order",
            params=params,
            headers={"x-api-key": api_key, "accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            if r.status != 200:
                text = await r.text()
                log.warning(f"ultra /order {r.status}: {text[:200]}")
                return None
            d = await r.json()
            if "error" in d and d.get("error"):
                log.warning(f"ultra /order error: {d.get('error')}")
                return None
            if "requestId" not in d or "transaction" not in d:
                log.warning(f"ultra /order missing fields: {list(d.keys())}")
                return None
            return d
    except Exception as e:
        log.error(f"ultra /order exc: {e}")
        return None


async def ultra_execute(session, request_id, signed_tx_b64, api_key):
    """POST /ultra/v1/execute — returns {signature, status} or None."""
    body = {"requestId": request_id, "signedTransaction": signed_tx_b64}
    try:
        async with session.post(
            f"{ULTRA_BASE}/execute",
            json=body,
            headers={
                "x-api-key": api_key,
                "content-type": "application/json",
                "accept": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            if r.status != 200:
                text = await r.text()
                log.warning(f"ultra /execute {r.status}: {text[:300]}")
                return None
            d = await r.json()
            return d
    except Exception as e:
        log.error(f"ultra /execute exc: {e}")
        return None


async def ultra_swap(session, in_mint, out_mint, amount_raw, keypair: Keypair, api_key, retries=3, retry_delay=1.5):
    """
    Full swap: order → sign → execute. Retries on transient failures.

    Returns (signature, out_amount_raw) on success, (None, 0) on permanent failure.
    A "Success" return from /execute guarantees the tx landed on-chain.
    """
    taker = str(keypair.pubkey())

    for attempt in range(1, retries + 1):
        order = await ultra_order(session, in_mint, out_mint, amount_raw, taker, api_key)
        if not order:
            log.warning(f"ultra_swap attempt {attempt}: order failed")
            if attempt < retries:
                await asyncio.sleep(retry_delay)
            continue

        request_id = order["requestId"]
        tx_b64 = order["transaction"]
        out_amount = int(order.get("outAmount", 0))

        # Deserialize, sign, re-serialize
        try:
            raw = base64.b64decode(tx_b64)
            tx = VersionedTransaction.from_bytes(raw)
            signed = VersionedTransaction(tx.message, [keypair])
            signed_b64 = base64.b64encode(bytes(signed)).decode()
        except Exception as e:
            log.error(f"ultra_swap sign exc: {e}")
            return None, 0  # crypto error — don't retry

        # Submit to execute
        result = await ultra_execute(session, request_id, signed_b64, api_key)
        if not result:
            log.warning(f"ultra_swap attempt {attempt}: execute returned None")
            if attempt < retries:
                await asyncio.sleep(retry_delay)
            continue

        status = result.get("status", "")
        sig = result.get("signature", "")

        if status == "Success":
            log.info(f"ultra_swap OK: {sig[:16]}... out={out_amount}")
            return sig, out_amount

        # Not success — log and retry if we have attempts left
        code = result.get("code", "")
        err = result.get("error", "")
        log.warning(
            f"ultra_swap attempt {attempt}: status={status} code={code} err={err}"
        )
        if attempt < retries:
            await asyncio.sleep(retry_delay)

    log.error(f"ultra_swap exhausted {retries} retries: {in_mint[:12]} -> {out_mint[:12]}")
    return None, 0


async def quote_sell_to_sol(session, mint, tokens_raw, taker, api_key):
    """
    Query Jupiter: how many SOL lamports would I get for selling `tokens_raw` of `mint`?

    Uses /order as a quote (no signature — we don't execute). Returns int or None.
    """
    if tokens_raw <= 0:
        return None
    order = await ultra_order(session, mint, SOL_MINT, tokens_raw, taker, api_key)
    if not order:
        return None
    try:
        return int(order.get("outAmount", 0))
    except (TypeError, ValueError):
        return None
