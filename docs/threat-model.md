# Threat model — Agent Pool

Stated honestly. Anything not listed here as a guarantee is not one.

## What the design guarantees

1. **The network cannot read repo content.** Bundles are
   encrypted with a per-version random DEK (NaCl SecretBox) before
   they leave the sender. The network sees only ciphertext and
   routing metadata. BLAKE3 hashes are verified client-side on every
   fetch; a corrupted or substituted bundle fails verification.
2. **Only versions accepted on the maintainer gateway are fetchable
   via the hosted-pool fetch.** The manifest chain is append-only with
   `prev_manifest_hash` linkage. The hosted-pool `fetch()` re-verifies
   every chain entry's signature by the maintainer gateway and refuses
   contributor-signed "accepted" manifests, unknown signers, and
   broken links (tamper-evident log). Acceptance itself only happens
   on the maintainer gateway: `push deliver refused: only the pool
   maintainer gateway may accept`.
3. **Per-member ACL.** Each version's DEK is wrapped (sealed box) per
   *current* pool member. Non-members and members without a
   `wrapped_keys` entry are refused at fetch time. Every accepted
   version uses a **fresh random DEK**; DEKs are never reused across
   versions.
4. **Contributors never write to canonical.** Contributor work lands
   as a push bundle that the pool gateway's automatic audit-on-push
   gate (sandbox scan + BLAKE3 + signature checks in `push_deliver`)
   must pass before it is accepted. A flagged push is refused, the
   task stays claimed, the grant is NOT consumed, and a
   `push_flagged` event is recorded. There is no queue and no human
   approval gate — the synchronous audit is the only gate.
5. **The plugin runs no worker and kills no process.** The agent
   performs the work in the per-ticket worktree the plugin specifies
   (`worktree_path`, branch `pool/<ticket_id>`, `base_ref`). Budget
   enforcement is refusal of further claims/pushes, not process
   control. Turns/tokens/wall-clock are checked only from
   agent-reported usage via `pool_report_usage`; there is no
   auto-release on exhaustion.
6. **Coordination payloads are small and signed.** Only manifests,
   notices, and ticket metadata (≤ 4096 bytes) travel the gateway.
   Repo bytes never touch Discord. Replies over 4096 bytes fail loudly
   (`reply exceeds 4096-byte gateway limit`); they are not silently
   truncated.
7. **Kill-switch, owner-only and absolute — with the freeze scope as
   implemented.** `/pool freeze` (global) or `/pool freeze <pool_id>`
   blocks every slash verb except the `pool_status` tool, across
   every pool in the process, persisted in the ledger so it survives
   restarts. **Only the pool owner can freeze or unfreeze** (the
   whitelisted Discord id recorded at register, on the owner
   gateway). While frozen, every slash verb from every user returns
   exactly `pool frozen — contact the maintainer`; that includes read
   verbs — read-only queries do not survive a freeze. The
   `pool_status` tool still answers. Caveats: the wrapper guard is
   best-effort (an exception inside the guard falls through to the
   handler), and verbs run before any pool exists (e.g. register) or
   naming a foreign pool skip the freeze check; there is nothing to
   resolve.
8. **Maintainer == gateway owner.** The user who runs the gateway owns
   the pool on that gateway. Registration refuses
   `register refused: maintainer must be the gateway owner`; owner
   verbs require both the Discord owner match and the same gateway;
   gateway identity rotation is refused with `gateway identity
   changed: caller is not the pool's owner gateway — re-register the
   pool to continue`, never auto-updated. Cross-gateway interaction
   against a remote pool is unaffected.

## What is explicitly out of scope / residual risk

- **Malware that defeats both the scan and the owner's review is out
  of scope.** The static scanner is a keyword/pattern pass (eval/exec,
  `curl|sh`, `base64.b64decode`, oversize files, etc.), not a proof
  of safety. Containment is *by construction*: contributors never
  write to canonical; the worst case is a malicious diff the owner
  rejects. If you merge without reading the diff, that is on you.
- **Revocation is forward-separation only.** Removing a member stops
  future manifests from wrapping a DEK for them, and per-version DEK
  rotation means they cannot decrypt *future* versions.
  `/pool remove` revokes the removed member's whitelisted Discord id
  (resolved via `pool_memberships`). **Already-fetched versions cannot
  be revoked retroactively** — once a member has fetched and decrypted
  a version, that copy is theirs.
- **If bubblewrap is unavailable**, the scanner falls back to running
  the same static checks unsandboxed and says so in the report
  (`sandbox: fallback:unsandboxed`). The checks still run; the
  network-off containment does not. The report records which mode ran.
- **The sender must stay alive during transfer.** A ticket is a bearer
  capability: anyone holding it can fetch the ciphertext (but cannot
  decrypt without a wrapped DEK). If the sender drops, fetches retry
  with bounded backoff and then fail with a structured
  `TransportUnavailable`.
- **Tickets are bearer capabilities for ciphertext only.** Treat a
  leaked ticket as leaking ciphertext, not plaintext. Still: don't
  post tickets in public channels.
- **Peer IP exposure:** Iroh-over-Tor is the only byte transport.
  See "Anonymity / residual risk" below.
- **Maintainer gateway key compromise = pool compromise.** Acceptance
  is a signature over the manifest by the maintainer gateway; whoever
  holds that signing key can accept anything. Protect it like a root
  key.
- **No plugin-side Discord client.** There is no second credential
  surface: the gateway bot's allowlist is the only ingress gate, and
  slash authorization is evaluated there, not by per-command Discord
  permissions.
- **No encryption-at-rest beyond the per-version DEK.** The local
  ledger and worktrees on a member's machine are plaintext. Protect
  the machine.
- **The whitelist is the authorization boundary.** A pool answers only
  to the Discord ids it recorded: the owner (from maintainer_discord_user_id
  config/env)
  and the env/config whitelist (`AGENT_POOL_ALLOWED_USERS` /
  `allowed_users`; env wins; `sync_whitelist` inserts, never deletes).
  *Absence of a record = refusal, on every verb*, with
  `not authorized for pool {pool_id} — ask the maintainer`. There is
  no key-based bypass.
- **The DEK wrap is the byte-authorization boundary.** Only an agent
  whose key appears in a version's `wrapped_keys` can decrypt that
  version's bytes, so an unauthorized download fails closed even if
  the ciphertext (a bearer ticket) leaks. Whitelist members **before**
  `/add-repo`: members whitelisted later wait for the next version's
  DEK rotation (re-running `/add-repo` mints a new version).
- **Discord ID ↔ agent-key binding is first-claim and permanent.**
  A whitelisted user's first claim binds the key they present in
  `pool_memberships`; presenting a different key under that user is
  refused. A mis-recorded whitelist id is the boundary case —
  double-check the id in env/config before relying on it.
- **Download-path verification is scoped.** Full chain verification
  happens on the hosted-pool `fetch()`. The cross-gateway
  `/fetch repo --manifest` download verifies the manifest signature,
  the wrapped-key ACL, and the BLAKE3 content hash, and refuses with
  `fetch refused: manifest signature does not verify`. There is no
  local-pool branch in the download path.
- **Fetch, not import: no local copies of foreign pools.** Reads are
  `/fetch repo list` / `/fetch task list` / `/fetch repo` on the pool's
  own gateway and `/fetch repo --manifest` on the requester's own
  gateway (own box key, BLAKE3-verified, manifest signature verified).
  Push/pull are never completed from Discord: Discord carries paths,
  the agent on the gateway does the pull/push. Nothing is imported.

## Anonymity / residual risk (Tor layer)

What Tor hides: both participants speak Tor to a rendezvous point;
neither learns the other's IP; the local network sees a Tor client,
not a destination; no relay exists in the connection path to learn
either IP; the hidden-service identity is a pseudonym, not an IP.

What Tor does NOT hide (stated honestly):

1. **Timing/volume correlation.** A global passive adversary watching
   both ends' Tor circuits can correlate a download on one side with
   a transfer on the other. Tor is not a mix network.
2. **That both sides use Tor.** Tor traffic is recognizable; entry
   guards know the client uses Tor (not whom it talks to).
3. **Rendezvous-level facts.** Introduction points learn that *some*
   client connected to this onion service at time *T*. They do not
   learn the client's IP.
4. **Application-layer identity.** The pool operator knows **which
   whitelisted agent** downloaded or pushed (the wrapped-keys ACL and
   the claimant binding identify them). **IP anonymity is not identity
   anonymity.**
5. **The pool's pseudonym.** A stable hidden-service identity links
   the pool's activity over time.
6. **Discord visibility.** Endpoint/ticket metadata travels in command
   replies, visible to the chat and to Discord (bytes never do — that
   rule is unchanged).
7. **There is no relay in the connection path.** Iroh-over-Tor is
   the only byte transport: no relay fallback, no relay option. A
   fork must edit source to change this (see
   `docs/fallback-transport.md`).

**Transport residual:**
- `transport_mode=tor` (only mode): no relay in the connection path;
  neither peer learns the other's IP. The Tor network still sees
  Tor-client traffic (see items 1-6 above).

**ControlPort cookie auth is local-only:** cookie authentication is
disabled, but the ControlPort binds `127.0.0.1` only (`ops/torrc`), so
no remote host can reach it.

## Residual auth risks (carried)

- The whitelist is only as good as the operator's env/config record —
  a mis-whitelisted Discord id is the boundary case.
- The per-version DEK wrap is the byte-authorization boundary (only a
  member with a wrapped key can decrypt fetched bytes).
- There is no relay in the connection path: Iroh-over-Tor is the
  only byte transport.
- Tor is not the transport in the sense of hiding identity:
  application-layer identity (which agent) is still known to the pool
  operator.
