# settlement-retry-safety-v1 against Feeless402 0.2.9 (c82e72a)

Independent re-verification of the three fixes reported against `Feeless402/feeless402` 0.2.8 privately as GHSA-cx37-j5vc-c967, run against the real fix (tag `v0.2.9`, commit `c82e72a`). Same harness family as the 0.2.8 report (`feeless402-retry-safety-report-2026-09-23.zip`), adapted to the new API surface. This does not replace Feeless402's own 51 tests — it is external verification alongside them.

## What is real and what is simulated

**Real:** Feeless402's own code at c82e72a, imported unmodified:

- the `GET /premium` route (`server.create_app`);
- `settled_replay`, `verify_block`, `settle_block`, `block_hash`, `verify_replay_proof`;
- the client's `request_with_payment`, `_settle_outcome`, `build_payment_header`, `parse_quote`, `replay_proof`, and the `pending-payments.json` journal it now writes.

**Simulated:**

- **The Nano node** (`fake_nano_node.py`, unchanged from the 0.2.8 report). It implements `account_info`, `block_info` and `process` with Nano's Old block / Fork semantics. Every block is confirmed on apply, and `local_timestamp` is the node clock at that moment.
- **The HTTP hop**, through FastAPI TestClient. Transport faults are applied to the reply after the merchant has fully handled the request. `X-Forwarded-For` is set directly by the harness on cases that test it — nothing plays the role of a reverse proxy in front of the app.
- **Proof-of-work.** Work is a zero value, as in Feeless402's own tests. `verify_block` only checks the format, and in the client leg `Wallet._work_valid` is patched to return true. Difficulty is not modelled.

No XNO was spent, and no public node, faucet or Feeless402 service was contacted.

**Not covered:**

- real network timing, propagation, failover across public RPC nodes;
- concurrency;
- the `/demo/article` route (wired the same way, not run);
- **whether the real deployment sits behind a reverse proxy that overwrites `X-Real-IP`/`X-Forwarded-For` before they reach the app** — see finding 4 below. We tested the app directly, the way `TestClient` always does; we have no visibility into the actual production topology and did not probe it.
- whether a real Nano RPC node ever returns `local_timestamp` absent or `0` for a recently-confirmed block — see finding 5. This was an open question in the 0.2.8 report too.

## Run

```
git clone https://github.com/Feeless402/feeless402 && git -C feeless402 checkout c82e72a
pip install nanopy requests fastapi httpx anyio
FEELESS402_PATH=$PWD/feeless402 python3 run_feeless402.py
```

Not wired into CI (external checkout + `nanopy`, same as the 0.2.8 harness).

## Results (2026-09-27), against the three things reported on 0.2.8

**1. Client double-charge on retry — fixed, verified.**

| Case | 0.2.8 (reported) | 0.2.9 (measured) |
|---|---|---|
| C1: reply lost after merchant settles | `PaidRequestFailed`, `settled: true`, note says re-present the same block; receipt carries the hash only | same, plus a `will_re_present: true` field |
| C2: caller retries `request_with_payment` | signs a **new** block; `ledger_len` 2 (double charge) | re-presents the **journaled** block (`new_block: false`); `ledger_len` stays **1** |

`request_with_payment` now writes the signed payment to `pending-payments.json` next to the wallet *before* sending it. A retry — in the same call or a later one — re-presents that same block; a fresh block is signed only when the merchant rejects the re-presentation and the ledger confirms the first one never landed. Verified directly against the real client and journal file, not by reading the diff: `C2.new_block == false`, `C2.block_matches_journal == true`, journal entry cleared once served.

**2. 15-minute replay window — fixed, verified.**

`REPLAY_WINDOW_S` is now `24 * 3600`. Measured the exact boundary: re-presenting at 23:59:59h since settlement → `200`; at 24:00:01h → `402`. No drift from the stated value.

**3. Observer exhausting the replay honors before the payer retries — fixed for the shipped client, open for anyone re-presenting without a proof.**

Two things changed together: a payer can now attach `X-PAYMENT-PROOF` (a signature, with the block-signing key, over hash+method+path) to prove *it* is the one re-presenting, and anonymous re-presentations (no proof) are counted per requester (`REPLAY_MAX=3`) as well as in total per hash (`REPLAY_MAX_ANON_TOTAL=30`); a proven re-presentation gets its own, much larger budget (`REPLAY_MAX_PROVEN=100`).

| Scenario | Result |
|---|---|
| Observer (no proof) uses 3 honors, then the real payer re-presents **with** proof — what the shipped 0.2.9 client actually sends on retry (via the journal) | Observer: `200 x3`. Payer: **`200`** — fixed. |
| Observer (no proof) uses 3 honors, then the real payer re-presents **without** proof — a hand re-presentation per the old README instructions, or any client that predates 0.2.9 or otherwise omits the header | Observer: `200 x3`. Payer: **`402`** — same outcome as 0.2.8. |
| Observer (no proof) rotates 31 different `X-Forwarded-For` values, no proof at all | 30 of 31 succeed (`REPLAY_MAX_ANON_TOTAL`), the 31st is `402`. A same-hash payer re-presenting without proof afterward: **`402`**. |

`_client_ip()` in `server.py` reads `X-Real-IP`, then `X-Forwarded-For`, with no check that either came from a trusted reverse proxy — both are ordinary request headers a client sets itself. Whether that matters for a real deployment depends on whether it sits behind a reverse proxy that overwrites those headers with the real peer address before the app sees them (a common, recommended nginx pattern: `proxy_set_header X-Real-IP $remote_addr;`) — that's not something we tested or have visibility into, and it's the reason `X-Real-IP` is checked first. What we can say from testing the app directly: the "per-requester" bucketing is not itself a barrier against a client willing to vary a header it controls; it moves the cost of exhausting the anonymous budget for a hash from 3 requests to 30, not out of reach. The fix is real and closes the case that matters most in practice — the shipped client proves its own re-presentations by default — but it's a fix for the client, not (on its own) for the server-side anonymous path.

**4. Bonus, unrelated to the three reported fixes — the `local_timestamp` absent/zero edge from the 0.2.8 report is unchanged.**

`if ts and time.time() - ts > REPLAY_WINDOW_S: return None` — `ts` computed as `int(info.get("local_timestamp") or 0)`. When `ts` is `0` (absent or reported as zero), the condition is `if 0 and ...`, which is `False`, so the whole window check is skipped — not relaxed, skipped. Measured: a block backdated 25 hours (past the new 24h window) with `local_timestamp` omitted still gets `200`. This was already flagged as an untested edge in the 0.2.8 report ("not verified: whether real nodes report `local_timestamp` as 0/absent for recent blocks") and remains exactly as open; the three fixes in 0.2.9 didn't touch this line.

## Reading

- **The headline finding — the client double-charging itself on retry — is genuinely fixed**, verified by driving the real client through a lost-reply-then-retry sequence and reading the ledger, not by trusting the changelog. This is the one that mattered most: it's the only one of the four where money moved twice for one purchase.
- **The replay window is exactly what the changelog says.** No hedging needed here.
- **The observer-exhaustion fix protects the payer Feeless402 controls (its own client) but not the payer in general.** A payer who doesn't send `X-PAYMENT-PROOF` — for any reason, not just malice — is in the same anonymous bucket as an observer, and that bucket's "per requester" partition is trivially widened by an observer willing to vary a header it already controls. This is a real, reproducible gap, not a theoretical one; whether it's *exploitable in Feeless402's actual deployment* depends on infrastructure we have no visibility into (reverse-proxy header handling) and did not probe.
- **The `local_timestamp` edge is unchanged** — same code, same open question about real nodes, now sitting under a 24h window instead of 15 minutes if it is ever reachable.

## Limits of this report

Same as the 0.2.8 report: logic against a simulated node, not production network timing, propagation, or RPC failover. New in this round: the `X-Forwarded-For` finding is about the app in isolation; we did not test (and have no access to test) whatever sits in front of it in production.
