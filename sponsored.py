"""Build swaps ourselves so we can sponsor gas and take a fee in one transaction.

Jupiter's /swap builds a finished transaction with the user as fee payer, and it
ignores a feePayer field — so sponsoring gas means assembling the transaction
from /swap-instructions instead. Once we are assembling it, the platform fee is
one more instruction rather than a separate mechanism.

Why the fee is a plain wSOL transfer:
  Jupiter's platformFeeBps pays in a mint from the swap pair. On a Token-2022
  coin that means a Token-2022 fee account, and a classic wSOL account fails the
  whole swap with IncorrectTokenProgramID. Taking 1% of the INPUT in wSOL always
  works, on every coin, into one account.

Signature order: the message's first account is the fee payer, so the sponsor
signs in slot 0 and the user in slot 1.
"""
import base64
import aiohttp

from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.instruction import Instruction, AccountMeta
from solders.message import MessageV0
from solders.transaction import VersionedTransaction
from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.hash import Hash
from solders.signature import Signature

import config as C

WSOL = Pubkey.from_string("So11111111111111111111111111111111111111112")
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ATA_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

# Only these may appear in a transaction the sponsor signs. Privy warns that ATA
# rent refunds go to the account owner rather than the fee payer, so an
# unrestricted sponsor can be drained by opening and closing accounts in a loop.
# Solana rejects anything over 1232 raw bytes. Jupiter routes without knowing we
# will add to the transaction, so we cap the route's account count and check the
# finished size rather than discovering it at send time.
TX_LIMIT = 1232
MAX_ACCOUNTS = 40

ALLOWED_PROGRAMS = {
    "ComputeBudget111111111111111111111111111111",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
    "11111111111111111111111111111111",
}


def sponsor_keypair():
    import base58
    secret = getattr(C, "SPONSOR_SECRET", "") or ""
    return Keypair.from_bytes(base58.b58decode(secret)) if secret else None


def ata(owner: Pubkey, mint: Pubkey = WSOL) -> Pubkey:
    return Pubkey.find_program_address(
        [bytes(owner), bytes(TOKEN_PROGRAM), bytes(mint)], ATA_PROGRAM)[0]


def _ix(d: dict) -> Instruction:
    """Turn Jupiter's JSON instruction into one solders can compile."""
    return Instruction(
        Pubkey.from_string(d["programId"]),
        base64.b64decode(d["data"]),
        [AccountMeta(Pubkey.from_string(a["pubkey"]), a["isSigner"], a["isWritable"])
         for a in d["accounts"]],
    )


def fee_transfer(user: Pubkey, fee_ata: Pubkey, lamports: int) -> Instruction:
    """1% of the input, in wSOL, from the user's wrapped account to ours.

    SPL Token Transfer is instruction 3. Deliberately wSOL rather than native
    SOL: the Privy policy permits Token-program instructions but restricts
    System transfers to the wallet's own wrapped account, so this needs no
    policy change.
    """
    return Instruction(
        TOKEN_PROGRAM,
        bytes([3]) + int(lamports).to_bytes(8, "little"),
        [AccountMeta(ata(user), False, True),
         AccountMeta(fee_ata, False, True),
         AccountMeta(user, True, False)],
    )


async def _lookup_tables(s, addresses):
    """Fetch the address lookup tables the swap instruction references."""
    if not addresses:
        return []
    out = []
    for addr in addresses:
        r = await s.post(C.RPC_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [addr, {"encoding": "base64"}]})
        d = await r.json()
        v = (d.get("result") or {}).get("value")
        if not v:
            continue
        raw = base64.b64decode(v["data"][0])
        # LUT layout: 56-byte header, then a packed array of 32-byte addresses
        keys = [Pubkey.from_bytes(raw[i:i + 32]) for i in range(56, len(raw), 32)]
        out.append(AddressLookupTableAccount(key=Pubkey.from_string(addr), addresses=keys))
    return out


def check_safe(tx: VersionedTransaction, sponsor: Pubkey) -> str | None:
    """Refuse to sponsor anything we did not expect. None means it is fine."""
    msg = tx.message
    keys = list(msg.account_keys)
    if str(keys[0]) != str(sponsor):
        return f"fee payer is {keys[0]}, not the sponsor"
    for c in msg.instructions:
        pid = str(keys[c.program_id_index])
        if pid not in ALLOWED_PROGRAMS:
            return f"unexpected program {pid}"
    return None



def _repay(ix: Instruction, payer: Pubkey) -> Instruction:
    """Make `payer` the funding account of an associated-token-account create.

    Account 0 of an ATA create is the payer. Only touched for that program, and
    only when the account is a writable signer, so nothing else is rewritten.
    """
    if str(ix.program_id) != str(ATA_PROGRAM) or not ix.accounts:
        return ix
    first = ix.accounts[0]
    if not (first.is_signer and first.is_writable):
        return ix
    return Instruction(ix.program_id, bytes(ix.data),
                       [AccountMeta(payer, True, True)] + list(ix.accounts[1:]))


async def build_swap(s, user: Pubkey, in_mint: str, out_mint: str,
                     amount_lamports: int, fee_bps: int = 0,
                     slippage_bps: int = None, priority_lamps: int = None):
    """Assemble a swap the sponsor pays for, with our fee inside it.

    Returns (VersionedTransaction, quote, fee_lamports) or (None, None, error).

    The fee comes off the INPUT before quoting, so the user is quoted on what is
    actually swapped and the arithmetic on screen matches the fill.
    """
    sponsor = sponsor_keypair()
    if sponsor is None:
        return None, None, "no sponsor configured"

    fee_lamports = (amount_lamports * fee_bps) // 10_000 if fee_bps else 0
    swap_amount = amount_lamports - fee_lamports
    if swap_amount <= 0:
        return None, None, "amount too small once the fee is taken"

    hdrs = {"x-api-key": C.JUP_API_KEY} if C.JUP_API_KEY else {}
    # maxAccounts leaves headroom for the sponsor key and the fee instruction we
    # add afterwards. Jupiter's default (64) can produce a route that fits its
    # own build and overflows ours.
    url = (f"{C.JUP_HOST}/quote?inputMint={in_mint}&outputMint={out_mint}"
           f"&amount={swap_amount}&slippageBps={slippage_bps or C.SLIPPAGE_BPS}"
           f"&restrictIntermediateTokens=true&maxAccounts={MAX_ACCOUNTS}")
    async with s.get(url, headers=hdrs) as r:
        quote = await r.json()
    if not quote.get("outAmount"):
        return None, None, f"no route ({str(quote)[:60]})"

    body = {"quoteResponse": quote, "userPublicKey": str(user),
            "wrapAndUnwrapSol": False}
    if priority_lamps:
        body["prioritizationFeeLamports"] = {
            "priorityLevelWithMaxLamports": {
                "maxLamports": int(priority_lamps), "priorityLevel": "high"}}
    async with s.post(f"{C.JUP_HOST}/swap-instructions", json=body, headers=hdrs) as r:
        ins = await r.json()
    if not ins.get("swapInstruction"):
        return None, None, f"no instructions ({str(ins)[:60]})"

    ixs = [_ix(i) for i in ins.get("computeBudgetInstructions") or []]
    # Jupiter makes the USER the rent payer on setup instructions. Repoint that
    # at the sponsor, otherwise creating a token account for a coin they have
    # never held still needs native SOL — the thing sponsorship exists to remove.
    ixs += [_repay(_ix(i), sponsor.pubkey()) for i in ins.get("setupInstructions") or []]
    if fee_lamports:
        dest = getattr(C, "FEE_ACCOUNT", "") or ""
        if not dest:
            return None, None, "fee_bps set but FEE_ACCOUNT is empty"
        # Before the swap: the fee comes off the input, not out of the proceeds,
        # so the quote the user sees is for what is actually swapped.
        ixs.append(fee_transfer(user, Pubkey.from_string(dest), fee_lamports))
    ixs.append(_ix(ins["swapInstruction"]))
    if ins.get("cleanupInstruction"):
        ixs.append(_ix(ins["cleanupInstruction"]))

    luts = await _lookup_tables(s, ins.get("addressLookupTableAddresses") or [])

    async with s.post(C.RPC_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash",
            "params": [{"commitment": "finalized"}]}) as r:
        bh = Hash.from_string(((await r.json())["result"]["value"]["blockhash"]))

    # The FEE PAYER is account 0 — that is what makes this sponsored.
    msg = MessageV0.try_compile(sponsor.pubkey(), ixs, luts, bh)
    # One empty slot per required signer. The sponsor is slot 0 because it is
    # the fee payer; the user's wallet fills the slot matching its own index.
    nsigs = msg.header.num_required_signatures
    tx = VersionedTransaction.populate(msg, [Signature.default()] * nsigs)

    size = len(bytes(tx))
    if size > TX_LIMIT:
        # Better to say so here than to have the node refuse it with a truncated
        # RPC message after we have already asked the user's wallet to sign.
        return None, None, f"tx_too_large:{size}>{TX_LIMIT}"
    return tx, quote, fee_lamports
