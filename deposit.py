"""
deposit.py — fund a trading wallet with WSOL, signed entirely by YOU.

    FUNDER_KEY=<base58 secret> python deposit.py <destination> <sol amount>

WHY IT LOOKS LIKE THIS
  The trading wallet's policy has no System Program, so it cannot wrap SOL —
  that omission is the custody guarantee and we don't want to weaken it just to
  make a deposit convenient. So the deposit happens the other way round: your
  wallet creates the destination's WSOL account, transfers SOL into it, and
  calls SyncNative to mint the matching WSOL balance.

  SyncNative needs no authority signature — it just reconciles a token account's
  balance with the lamports actually in it. That's what makes this possible:
  every instruction here is authorised by the funder alone. The destination
  wallet signs nothing and its policy is never relaxed.

  This is also exactly what the terminal's deposit button will do from the
  user's connected wallet.

The destination ends up with SPENDABLE WSOL, which is what the bot trades with.
"""
import os
import sys

import requests
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction

WSOL = Pubkey.from_string("So11111111111111111111111111111111111111112")
TOKEN = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ATA_PROG = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYS = Pubkey.from_string("11111111111111111111111111111111")
RPC = os.environ.get("RPC_URL", "https://api.mainnet-beta.solana.com")


def rpc(method, params):
    r = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1,
                                 "method": method, "params": params}, timeout=30)
    return r.json()


def ata(owner, mint):
    addr, _ = Pubkey.find_program_address(
        [bytes(owner), bytes(TOKEN), bytes(mint)], ATA_PROG)
    return addr


def create_ata_idempotent(payer, owner, mint, account):
    """CreateIdempotent (discriminator 1) — a no-op if the account already exists."""
    return Instruction(
        program_id=ATA_PROG,
        accounts=[
            AccountMeta(payer, True, True),
            AccountMeta(account, False, True),
            AccountMeta(owner, False, False),
            AccountMeta(mint, False, False),
            AccountMeta(SYS, False, False),
            AccountMeta(TOKEN, False, False),
        ],
        data=bytes([1]),
    )


def sync_native(account):
    """SyncNative (discriminator 17). No signer — anyone may call it."""
    return Instruction(program_id=TOKEN,
                       accounts=[AccountMeta(account, False, True)],
                       data=bytes([17]))


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 1
    dest_str, amount_str = sys.argv[1], sys.argv[2]
    secret = os.environ.get("FUNDER_KEY", "")
    if not secret:
        print("Set FUNDER_KEY to the base58 secret key of the funding wallet.")
        return 1

    funder = Keypair.from_base58_string(secret)
    dest = Pubkey.from_string(dest_str)
    lamports = int(float(amount_str) * 1e9)
    dest_wsol = ata(dest, WSOL)

    bal = rpc("getBalance", [str(funder.pubkey())]).get("result", {}).get("value", 0)
    print(f"from    {funder.pubkey()}  ({bal/1e9:.6f} SOL)")
    print(f"to      {dest}")
    print(f"  its WSOL account: {dest_wsol}")
    print(f"amount  {lamports/1e9} SOL -> WSOL\n")

    # Leave room for rent on the token account (~0.00204 SOL) and the fee.
    if bal < lamports + 3_000_000:
        print(f"Not enough SOL. Need ~{(lamports + 3_000_000)/1e9:.4f} including "
              f"rent and fees, have {bal/1e9:.6f}.")
        return 1

    if input("Send it? [y/N] ").strip().lower() != "y":
        print("Nothing sent.")
        return 1

    bh = rpc("getLatestBlockhash", [{"commitment": "finalized"}])
    blockhash = Hash.from_string(bh["result"]["value"]["blockhash"])

    ixs = [
        create_ata_idempotent(funder.pubkey(), dest, WSOL, dest_wsol),
        transfer(TransferParams(from_pubkey=funder.pubkey(),
                                to_pubkey=dest_wsol, lamports=lamports)),
        sync_native(dest_wsol),
    ]
    tx = Transaction([funder], Message.new_with_blockhash(
        ixs, funder.pubkey(), blockhash), blockhash)

    import base64
    send = rpc("sendTransaction", [base64.b64encode(bytes(tx)).decode(),
                                   {"encoding": "base64", "maxRetries": 5}])
    sig = send.get("result")
    if not sig:
        print(f"send failed: {send.get('error')}")
        return 1
    print(f"\nsent: https://solscan.io/tx/{sig}")

    import time
    for _ in range(30):
        time.sleep(2)
        st = rpc("getSignatureStatuses", [[sig]])
        v = (st.get("result", {}).get("value") or [None])[0]
        if v and v.get("confirmationStatus") in ("confirmed", "finalized"):
            if v.get("err"):
                print(f"on-chain error: {v['err']}")
                return 1
            b = rpc("getTokenAccountBalance", [str(dest_wsol)])
            got = (b.get("result", {}).get("value") or {}).get("uiAmountString", "?")
            print(f"confirmed. destination now holds {got} WSOL — spendable by the bot.")
            return 0
    print("not confirmed in 60s — check the link above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
