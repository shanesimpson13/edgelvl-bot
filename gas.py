"""Keep enough native SOL in the wallet to pay for transactions.

The hole this closes: deposits get wrapped to WSOL so the bot can trade them,
but nothing ever converts WSOL back. Every swap burns native SOL on fees and
priority tips, so native only ever falls. Around twenty trades from a full
reserve the wallet runs dry — and it does NOT announce itself as "out of gas".
It surfaces as a sell that fails to broadcast, which reads like a broken sell.
Worse, on this wallet it produced buys that never landed while the bot recorded
them as real positions.

The fix is two instructions the wallet is already allowed to make:

  CloseAccount on the WSOL account  — Token program, permitted by the swap
      policy. Returns every lamport in it AS NATIVE SOL, including the ~0.00204
      rent deposit, and deletes the account.
  Transfer + SyncNative back        — permitted by the "wrap into own WSOL
      only" rule, which is why the destination is checked against our own ATA.

Net effect: native tops back up, the rest goes straight back to tradeable, and
no funds ever leave the wallet — so nothing here needs a permission the bot
doesn't already have.
"""
import base64

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction

import config as C

TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ATA_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
WSOL = Pubkey.from_string("So11111111111111111111111111111111111111112")

# What "enough" means. A swap costs a base fee plus a priority tip; the floor is
# set well above one transaction so a top-up is never itself unaffordable.
GAS_FLOOR = 0.004          # below this, top up
GAS_TARGET = 0.012         # leave roughly this much native behind
RENT_WSOL = 2_039_280      # lamports returned when the WSOL account closes
WRAP_MIN = 0.002           # don't spend a fee wrapping dust


def _ata(owner: Pubkey) -> Pubkey:
    return Pubkey.find_program_address(
        [bytes(owner), bytes(TOKEN_PROGRAM), bytes(WSOL)], ATA_PROGRAM)[0]


async def _rpc(s, method, params):
    async with s.post(C.RPC_URL, json={"jsonrpc": "2.0", "id": 1,
                                       "method": method, "params": params}) as r:
        return await r.json()


async def balances(s, address: str):
    """(native SOL, wrapped SOL). Either can be zero."""
    owner = Pubkey.from_string(address)
    b = await _rpc(s, "getBalance", [address])
    native = ((b.get("result") or {}).get("value") or 0) / 1e9
    w = await _rpc(s, "getTokenAccountBalance", [str(_ata(owner))])
    v = (w.get("result") or {}).get("value")
    return native, (int(v["amount"]) / 1e9 if v else 0.0)


async def needs_gas(s, address: str) -> bool:
    native, _ = await balances(s, address)
    return native < GAS_FLOOR


async def wrap_idle(s, wallet):
    """Turn a plain-SOL deposit into tradeable balance.

    The bot spends WSOL. Native SOL sent to the wallet is invisible to it, and
    until now the only thing that converted a deposit was a poller in the
    terminal that runs *while the wallet modal is open*. Deposit from your
    phone and close the tab and the money sits there, not trading, and not even
    showing in the header — which reads as "my deposit disappeared".

    The policy already allows this: a System Transfer whose destination is the
    wallet's OWN wrapped-SOL account, then SyncNative. Nothing leaves the
    wallet, so no new permission is involved.
    """
    address = wallet.address
    owner = Pubkey.from_string(address)
    wsol_acct = _ata(owner)

    native, _ = await balances(s, address)
    spare = native - GAS_TARGET
    if spare < WRAP_MIN:
        return None, "nothing_idle"

    lamports = int(spare * 1e9)
    ixs = [
        # Idempotent create, in case this is the first deposit and there is no
        # wrapped account yet.
        Instruction(ATA_PROGRAM, bytes([1]), [
            AccountMeta(owner, True, True),
            AccountMeta(wsol_acct, False, True),
            AccountMeta(owner, False, False),
            AccountMeta(WSOL, False, False),
            AccountMeta(Pubkey.from_string("11111111111111111111111111111111"), False, False),
            AccountMeta(TOKEN_PROGRAM, False, False)]),
        transfer(TransferParams(from_pubkey=owner, to_pubkey=wsol_acct, lamports=lamports)),
        Instruction(TOKEN_PROGRAM, bytes([17]), [AccountMeta(wsol_acct, False, True)]),
    ]

    bh = await _rpc(s, "getLatestBlockhash", [{"commitment": "finalized"}])
    blockhash = Hash.from_string(bh["result"]["value"]["blockhash"])
    msg = Message.new_with_blockhash(ixs, owner, blockhash)
    unsigned = base64.b64encode(bytes(Transaction.new_unsigned(msg))).decode()

    signed, err = await wallet.sign(s, unsigned)
    if err:
        return None, f"sign_failed:{err}"
    r = await _rpc(s, "sendTransaction",
                   [signed, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}])
    if r.get("error"):
        return None, f"send_failed:{str(r['error'])[:120]}"
    return r.get("result"), None


async def top_up(s, wallet):
    """Close the WSOL account and re-wrap all but the gas reserve.

    Returns (signature, error). Does nothing and reports why if there's no
    wrapped balance to draw on — that's a genuinely empty wallet, and the
    honest answer is "deposit more", not a transaction that can't help.
    """
    address = wallet.address
    owner = Pubkey.from_string(address)
    wsol_acct = _ata(owner)

    native, wrapped = await balances(s, address)
    if native >= GAS_FLOOR:
        return None, "not_needed"

    recoverable = wrapped + RENT_WSOL / 1e9
    if recoverable <= GAS_TARGET:
        return None, (f"nothing to convert — {native:.6f} native and "
                      f"{wrapped:.6f} wrapped is not enough to cover fees. "
                      f"Deposit SOL to {address}.")

    ixs = [
        # CloseAccount (discriminator 9): account, destination, authority.
        # Everything in the account, plus its rent, comes back as native SOL.
        Instruction(TOKEN_PROGRAM, bytes([9]), [
            AccountMeta(wsol_acct, False, True),
            AccountMeta(owner, False, True),
            AccountMeta(owner, True, False)]),
        # Recreate it, idempotently (discriminator 1), so the re-wrap has
        # somewhere to land.
        Instruction(ATA_PROGRAM, bytes([1]), [
            AccountMeta(owner, True, True),
            AccountMeta(wsol_acct, False, True),
            AccountMeta(owner, False, False),
            AccountMeta(WSOL, False, False),
            AccountMeta(Pubkey.from_string("11111111111111111111111111111111"), False, False),
            AccountMeta(TOKEN_PROGRAM, False, False)]),
    ]

    # Everything above the reserve goes back to tradeable. The destination is
    # our OWN wrapped-SOL account, which is the only transfer the policy allows.
    rewrap = int((recoverable - GAS_TARGET) * 1e9)
    if rewrap > 0:
        ixs.append(transfer(TransferParams(
            from_pubkey=owner, to_pubkey=wsol_acct, lamports=rewrap)))
        ixs.append(Instruction(TOKEN_PROGRAM, bytes([17]),        # SyncNative
                               [AccountMeta(wsol_acct, False, True)]))

    bh = await _rpc(s, "getLatestBlockhash", [{"commitment": "finalized"}])
    blockhash = Hash.from_string(bh["result"]["value"]["blockhash"])
    msg = Message.new_with_blockhash(ixs, owner, blockhash)
    tx = Transaction.new_unsigned(msg)
    unsigned = base64.b64encode(bytes(tx)).decode()

    signed, err = await wallet.sign(s, unsigned)
    if err:
        return None, f"sign_failed:{err}"

    r = await _rpc(s, "sendTransaction",
                   [signed, {"encoding": "base64", "skipPreflight": False,
                             "maxRetries": 3}])
    if r.get("error"):
        return None, f"send_failed:{str(r['error'])[:120]}"
    return r.get("result"), None
