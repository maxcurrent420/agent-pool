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
7. **Kill-switch.** `/pool freeze` (global) blocks all pool-mutating and
   claim/work/integrate actions across every pool in the process,
   persisted in the ledger so it survives restarts.

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
- **Peer IP exposure depends on `transport_mode`.** In `relay` mode (the
  default) the ticket is relay-only (`--ticket-type relay`): the peer
  learns no IP, but the relay operator (n0 or self-hosted) learns both
  IPs, EndpointIds, and connection timing/size — never plaintext. In
  `direct` mode the ticket embeds your IP addresses and Iroh hole-punches
  a direct QUIC path: **the peer learns your public IP:port**. Only use
  `direct` for LAN / trusted peers. `tor` mode is not available and fails
  closed. See README "Transport privacy".
- **Relay-only ticket ≠ endpoint relay-only (remaining gap).** The
  relay-only ticket forbids the receiver from learning/dialing a sender
  IP, but the `send_hermes` endpoint itself may still publish IP
  transports; fully closing that needs endpoint-side hardening in
  hermes-iroh-interconnect's sidecar (`clear_ip_transports()` +
  `AddrFilter::relay_only()`, behind a relay-only policy), which needs
  a sidecar rebuild + redeploy — see the implementation report's
  Remaining gaps.
- **Maintainer key compromise = pool compromise.** Acceptance is a
  signature over the manifest; whoever holds the maintainer signing key
  can accept anything. Protect it like a root key.
- **Discord user id ≠ agent identity (new residual risk from the button
  gate).** The `identities.discord_user_id` binding is operator-recorded
  at `/pool invite --discord-user <id>`; it is the only link between a
  Discord account and a pool `key_id`. Unmapped clickers are refused, but
  a **mis-bound mapping lets the wrong Discord account authorize an
  integration**. Mitigations: record the binding only on the
  maintainer's own gateway, double-check the id before inviting, and note
  the second layer — `resolve_integration` re-checks maintainer identity
  against the ledger (`tickets.py`), so a mis-binding still needs the
  ledger row to agree.
- **No plugin-side Discord client.** There is no second credential
  surface: the gateway bot's allowlist
  (`H/plugins/platforms/discord/adapter.py:4159-4167`) is the only
  ingress gate, and slash authorization is evaluated there, not by
  per-command Discord permissions. The plugin's platform handler receives
  the gateway's own bot (`buttons.py` factory) and touches nothing else.
- **No encryption-at-rest beyond the per-version DEK.** The local ledger
  and worktrees on a member's machine are plaintext. Protect the machine.
