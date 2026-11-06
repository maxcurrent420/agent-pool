# Threat model — Agent Pool

Stated honestly. Anything not listed here as a guarantee is not one.

## What the design guarantees

1. **The relay / network cannot read repo content.** Bundles are encrypted
   with a per-version random DEK (NaCl SecretBox) before they leave the
   sender. The Iroh relay (n0 public relay or a self-hosted one) sees only
   ciphertext and routing metadata. BLAKE3 hashes are verified client-side
   on every fetch; a corrupted or substituted bundle fails verification.
2. **Only maintainer-signed, accepted versions are fetchable.** The
   manifest chain is append-only with `prev_manifest_hash` linkage.
   `fetch()` re-verifies every chain entry's maintainer signature and
   refuses contributor-signed "accepted" manifests, unknown signers, and
   broken links (tamper-evident log).
3. **Per-member ACL.** Each version's DEK is wrapped (sealed box) per
   *current* pool member. Non-members and members without a `wrapped_keys`
   entry are refused at fetch time. Every accepted version — including the
   legacy `accept()` path and the Phase 5 merge path — uses a **fresh
   random DEK**; DEKs are never reused across versions.
4. **Contributors never write to canonical.** Contributor work lands as a
   *pending* submission. The maintainer's agent sandbox-scans (bubblewrap,
   `--unshare-net`, network off) the whole submission before acceptance
   and the *diff* before merge. A scan-flagged submission is rejected
   before any human review UI is offered; no buttons are posted for it.
5. **Workers run sandboxed.** Untrusted repo content is worked on in a
   `git worktree` on a per-ticket branch, seeded with a sandbox policy
   (bubblewrap, network off) and a hard budget (turns / tokens / wall
   clock) with process-group kill on exhaustion.
6. **Coordination payloads are small and signed.** Only manifests, notices,
   and ticket metadata (< 4 KB) travel the gateway. Repo bytes never touch
   Discord. The plugin never imports `discord` outside the button factory
   (`buttons.py`, lazily, inside the factory) and never opens its own
   client — Hermes's gateway bot does all posting and all button routing;
   the plugin only returns strings for Hermes to post.
7. **Kill-switch, owner-only and absolute.** `/pool freeze` (global) or
   `/pool freeze <pool_id>` blocks all pool-mutating and
   claim/work/integrate actions across every pool in the process,
   persisted in the ledger so it survives restarts. **Only the pool
   owner can freeze or unfreeze** (the whitelisted Discord id recorded at
   register) — no `is_freeze_ctl` bypass exists. While frozen, every other
   verb from every user returns exactly `pool frozen — contact the
   maintainer`; the owner's freeze/unfreeze and read-only queries still
   run (a kill path must not be blockable by the switch).

## What is explicitly out of scope / residual risk

- **Malware that defeats both the scan and the owner's review is out of
  scope.** The static scanner is a keyword/pattern pass (eval/exec,
  `curl|sh`, `base64.b64decode`, oversize files, etc.), not a proof of
  safety. Containment is *by construction*: contributors never write to
  canonical; workers are sandboxed; the worst case is a malicious diff
  the owner rejects at the approve gate. If you merge without reading the
  diff, that is on you.
- **Revocation is forward-separation only.** Removing a member stops
  future manifests from wrapping a DEK for them, and per-version DEK
  rotation means they cannot decrypt *future* versions. **Already-fetched
  versions cannot be revoked retroactively** — once a member has fetched
  and decrypted a version, that copy is theirs. This limitation is stated
  in every revocation notice and ledger event.
- **If bubblewrap is unavailable**, the scanner falls back to running the
  same static checks unsandboxed and says so in the report
  (`sandbox: fallback:unsandboxed`). The checks still run; the
  network-off containment does not. The report records which mode ran.
- **The sender must stay alive during transfer.** A ticket is a bearer
  capability: anyone holding it can fetch the ciphertext (but cannot
  decrypt without a wrapped DEK). If the sender drops, fetches retry with
  bounded backoff and then fail with a structured `TransportUnavailable`.
- **Tickets are bearer capabilities for ciphertext only.** Treat a leaked
  ticket as leaking ciphertext, not plaintext. Still: don't post tickets
  in public channels.
- **Peer IP exposure: none — transfers are always relay-only on both
  ends.** The send argv always uses a relay-only ticket
  (`--ticket-type relay`) and the `send_hermes` endpoints (send and
  receive) are built with `clear_ip_transports()` +
  `AddrFilter::relay_only()`, so neither side publishes or accepts
  direct IP addresses and **the peer never learns the other side's
  IP**. There is no direct mode and no way to opt into one;
  `HERMES_IROH_RELAY=off` is a no-op in every mode and an explicit
  `relay_url: off` is rejected at construction. The relay operator
  (n0 by default, or a self-hosted/trusted relay configured via
  `relay_url` on both ends) still learns both IPs, EndpointIds, and
  connection timing/size — never plaintext. `tor` mode is not
  available and fails closed with instructions to use `relay`. See
  README "Transport privacy".
- **Maintainer key compromise = pool compromise.** Acceptance is a
  signature over the manifest; whoever holds the maintainer signing key
  can accept anything. Protect it like a root key.
- **Button gate: the clicker must be the pool owner, not a member.** A
  Discord interaction carries only a platform user id, so the gate is
  "is this id the pool's owner?" (`_require_owner`) — an authorized
  non-owner who clicks Approve gets an ephemeral refusal and nothing is
  dispatched. Unmapped clickers are refused too (fail closed). The
  platform-id → key map the host supplies is attribution only.
- **No plugin-side Discord client.** There is no second credential
  surface: the gateway bot's allowlist
  (`H/plugins/platforms/discord/adapter.py:4159-4167`) is the only
  ingress gate, and slash authorization is evaluated there, not by
  per-command Discord permissions. The plugin's platform handler receives
  the gateway's own bot (`buttons.py` factory) and touches nothing else.
- **No encryption-at-rest beyond the per-version DEK.** The local ledger
  and worktrees on a member's machine are plaintext. Protect the machine.
- **Pending-until-approved lifecycle.** Tickets mint `pending`; only the **owner** moves them to `open` (approve/deny/modify — owner-only in the whitelist model). Non-owner lists exclude `pending` by default. This means a compromised member credential alone cannot create claimable work, and an authorized non-owner cannot mint at all.
- **The whitelist is the authorization boundary.** A pool answers only to the Discord ids it recorded: the owner (register) and each invitee (`/pool invite --discord-user`). *Absence of a record = refusal, on every verb.* There is no key-based bypass: the gateway's own agent key, which is always the pool owner on that host, never satisfies a check by itself — the driving user id decides. Keys are consulted only where crypto needs them (signing artifacts, wrapping DEKs).
- **The DEK wrap is the byte-authorization boundary.** Only an agent whose key appears in a version's `wrapped_keys` can decrypt that version's bytes, so an unauthorized download fails closed even if the ciphertext (a bearer ticket) leaks. Invite **before** `/add-repo`: members invited later wait for the next version's DEK rotation.
- **Packet-relay integrity rests on signatures, not the copy-paste channel.** A contributor's bytes reach the maintainer only as a signed proposal packet carrying the transfer ticket; integrity is the ed25519 signature + replay guard, not the channel it was pasted through. If a signer's key is compromised or the packet is replaced, verification fails closed (nothing mints).
- **Discord ID ↔ agent-key invite binding remains the residual auth risk.** The invite records a Discord user id AND a key; the id is the authorization and the key is the crypto attribution on every event. A **mis-recorded invite id** (wrong id typed, or a platform id spoofed upstream) is the boundary case — double-check the id before inviting, and note that a mis-bound mapping grants the *authorized* verbs, not owner-only ones, unless the owner id itself is wrong.
- **Fetch, not import: no local copies of foreign pools.** Reads are `/pool fetch` on the pool's own gateway and `/work fetch` on the requester's own gateway (own box key, BLAKE3-verified against the manifest). Nothing is imported, so there is no untrusted local copy to act on, and no peer/anchor state to mis-pin.
