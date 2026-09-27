"""
FakeNanoNode — an in-memory stand-in for the Nano node RPC that Feeless402
talks to. This is the ONLY simulated component of the Feeless402 run: the
merchant route, verify_block, settled_replay, settle_block, block_hash and
(for the client leg) request_with_payment are Feeless402's own code at
a7a8154, imported unmodified.

What it models, and only this:
  - account_info  -> {frontier, balance, representative} or None (unopened)
  - block_info    -> {contents, subtype, amount, confirmed, local_timestamp}
                     or RPCError("Block not found")
  - process       -> applies a send if block.previous == frontier; a block
                     already in the ledger answers "Old block", a block on a
                     stale frontier answers "Fork" (the two answers real
                     nodes give)
  - receivable / account_history / work_generate -> inert answers, so
    Feeless402's boot-time and bookkeeping threads never reach a network.

Every block is confirmed the moment it is applied. `local_timestamp` is the
node clock at apply time; `age(h, seconds)` backdates it, which is how the
15-minute replay window is driven without touching time.time().

Fault hooks (all default off) inject the rail-level faults the battery
modes need: lose the reply of the next `process` after applying it, make
the next N block_info calls fail, omit local_timestamp.
"""

import time

import nanopy
from nano_pay.rpc import RPCError
from nano_pay.verify import block_hash

NET = nanopy.Network()
REP = "nano_1center16ci77qw5w69ww8sy4i4bfmgfhr81ydzpurm91cauj11jn6y3uc5y"


class FakeNanoNode:
    def __init__(self):
        self.accounts = {}   # addr -> {"frontier", "balance"}
        self.blocks = {}     # hash -> {"contents", "subtype", "amount", "ts"}
        self.process_calls = 0
        # fault hooks
        self.lose_process_reply = 0      # apply, then raise, for the next N process calls
        self.block_info_unavailable = 0  # next N block_info calls raise
        self.omit_local_timestamp = False

    # ---- setup / inspection -------------------------------------------------
    def open_account(self, addr, frontier, balance):
        self.accounts[addr] = {"frontier": frontier.upper(), "balance": int(balance)}

    def age(self, h, seconds):
        self.blocks[h.upper()]["ts"] -= seconds

    def sends_to(self, dest_addr, source_addr=None):
        pk = NET.to_pk(dest_addr).upper()
        return [h for h, b in self.blocks.items()
                if b["subtype"] == "send" and b["contents"]["link"].upper() == pk
                and (source_addr is None or b["contents"]["account"] == source_addr)]

    # ---- RPC surface used by Feeless402 ------------------------------------
    def account_info(self, addr):
        a = self.accounts.get(addr)
        if a is None:
            return None
        return {"frontier": a["frontier"], "balance": str(a["balance"]),
                "representative": REP}

    def receivable(self, addr, count=50):
        return {}

    def work_generate(self, root, difficulty):
        return "0" * 16

    def process(self, block, subtype):
        self.process_calls += 1
        h = block_hash(block)
        if h in self.blocks:
            raise RPCError("Old block")
        a = self.accounts.get(block["account"])
        if a is None or a["frontier"] != str(block["previous"]).upper():
            raise RPCError("Fork")
        amount = a["balance"] - int(block["balance"])
        contents = {k: block[k] for k in ("type", "account", "previous", "representative",
                                         "balance", "link", "signature", "work")}
        contents["link"] = str(contents["link"]).upper()
        self.blocks[h] = {"contents": contents, "subtype": subtype,
                          "amount": amount, "ts": int(time.time())}
        a["frontier"], a["balance"] = h, int(block["balance"])
        if self.lose_process_reply:
            self.lose_process_reply -= 1
            raise RPCError("all RPC nodes failed, last error: read timeout")
        return h

    def call(self, payload):
        action = payload.get("action")
        if action == "block_info":
            if self.block_info_unavailable:
                self.block_info_unavailable -= 1
                raise RPCError("all RPC nodes failed, last error: connection refused")
            b = self.blocks.get(str(payload["hash"]).upper())
            if b is None:
                raise RPCError("Block not found")
            info = {"contents": dict(b["contents"]), "subtype": b["subtype"],
                    "amount": str(b["amount"]), "confirmed": "true"}
            if not self.omit_local_timestamp:
                info["local_timestamp"] = str(b["ts"])
            return info
        if action == "account_history":
            return {"history": []}
        raise RPCError(f"unsupported action in FakeNanoNode: {action}")
