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
   entry are refused at fetch time. Every accepted version — including
   the legacy `accept()` path and the push-deliver path — uses a **fresh
   random DEK**; DEKs are never reused across versions.
4. **Contributors never write to canonical.** Contributor work lands as
   a push bundle that the pool gateway's automatic audit-on-push gate
   (sandbox scan + BLAKE3 + signature checks in `push_deliver`,
   `tickets.py:912-989`) must pass before it is accepted. A flagged push
   is refused, the task stays claimed, the grant is NOT consumed, and a
   `push_flagged` event is recorded. There is no queue and no human
   approval gate — the synchronous audit is the only gate.
5. **Workers run sandboxed.** Untrusted repo content is worked on in a
   `git worktree` on a per-ticket branch, seeded with a sandbox policy
   (bubblewrap, network off) and a hard budget (turns / tokens / wall
   clock) with process-group kill on exhaustion.
6. **Coordination payloads are small and signed.** Only manifests, notices,
   and ticket metadata (< 4 KB) travel the gateway. Repo bytes never touch
   Discord.
7. **Kill-switch, owner-only and absolute.** `/pool freeze` (global) or
   `/pool freeze <pool_id>` blocks all pool-mutating and claim/push
   actions across every pool in the process, persisted in the ledger so it
   survives restarts. **Only the pool owner can freeze or unfreeze** (the
   whitelisted Discord id recorded at register). While frozen, every other
   verb from every user returns exactly `pool frozen — contact the
   maintainer`; the owner's freeze/unfreeze and read-only queries still
   run (a kill path must not be blockable by the switch).

## What is explicitly out of scope / residual risk

- **Malware that defeats both the scan and the owner's review is out of
  scope.** The static scanner is a keyword/pattern pass (eval/exec,
  `curl|sh`, `base64.b64decode`, oversize files, etc.), not a proof of
  safety. Containment is *by construction*: contributors never write to
  canonical; workers are sandboxed; the worst case is a malicious diff
  the owner rejects. If you merge without reading the diff, that is on
  you.
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
- **Peer IP exposure is mode-conditional.** See "Anonymity / residual
  risk" below for the full Tor/relay analysis.
- **Maintainer key compromise = pool compromise.** Acceptance is a
  signature over the manifest; whoever holds the maintainer signing key
  can accept anything. Protect it like a root key.
- **No plugin-side Discord client.** There is no second credential
  surface: the gateway bot's allowlist
  (`H/plugins/platforms/discord/adapter.py:4159-4167`) is the only
  ingress gate, and slash authorization is evaluated there, not by
  per-command Discord permissions.
- **No encryption-at-rest beyond the per-version DEK.** The local ledger
  and worktrees on a member's machine are plaintext. Protect the machine.
- **The whitelist is the authorization boundary.** A pool answers only to
  the Discord ids it recorded: the owner (register) and each invitee
  (`/pool invite --discord-user`). *Absence of a record = refusal, on
  every verb.* There is no key-based bypass.
- **The DEK wrap is the byte-authorization boundary.** Only an agent whose
  key appears in a version's `wrapped_keys` can decrypt that version's
  bytes, so an unauthorized download fails closed even if the ciphertext
  (a bearer ticket) leaks. Invite **before** `/add-repo`: members invited
  later wait for the next version's DEK rotation.
- **Discord ID ↔ agent-key invite binding remains the residual auth
  risk.** The invite records a Discord user id AND a key; the id is the
  authorization and the key is the crypto attribution on every event. A
  mis-recorded invite id is the boundary case — double-check the id before
  inviting.
- **Fetch, not import: no local copies of foreign pools.** Reads are
  `/fetch repo list` / `/fetch task list` / `/fetch repo` on the pool's
  own gateway and `/fetch repo --manifest` on the requester's own
  gateway (own box key, BLAKE3-verified). Nothing is imported.

## Anonymity / residual risk (Tor layer)

What Tor hides: both participants speak Tor to a rendezvous point; neither
learns the other's IP; the local network sees a Tor client, not a
destination; no relay exists in the connection path to learn either IP;
the hidden-service identity is a pseudonym, not an IP.

What Tor does NOT hide (stated honestly):

1. **Timing/volume correlation.** A global passive adversary watching
   both ends' Tor circuits can correlate a download on one side with a
   transfer on the other. Tor is not a mix network.
2. **That both sides use Tor.** Tor traffic is recognizable; entry guards
   know the client uses Tor (not whom it talks to).
3. **Rendezvous-level facts.** Introduction points learn that *some*
   client connected to this onion service at time *T*. They do not learn
   the client's IP.
4. **Application-layer identity.** The pool operator knows **which invited
   agent** downloaded or pushed (the wrapped-keys ACL and the claimant
   binding identify them). **IP anonymity is not identity anonymity.**
5. **The pool's pseudonym.** A stable hidden-service identity links the
   pool's activity over time.
6. **Discord visibility.** Endpoint/ticket metadata travels in command
   replies, visible to the chat and to Discord (bytes never do — that
   rule is unchanged).
7. **`relay` fallback mode (operator choice) still routes through an Iroh
   relay** that sees both endpoints' IPs + metadata (never plaintext). Tor
   mode removes the relay from the connection path; relay mode is for
   "I trust my relay" deployments. The mode is explicit config — never
   an automatic downgrade.

**Relay residual (mode-conditional):**
- `transport_mode=tor`: no relay in the connection path; neither peer
  learns the other's IP.
- `transport_mode=relay`: the relay sees both peers' IPs, EndpointIds,
  and connection timing/size metadata — never plaintext (QUIC/TLS + NaCl
  SecretBox).

**ControlPort cookie auth is local-only:** cookie authentication is
disabled, but the ControlPort binds `127.0.0.1` only (`ops/torrc`), so
no remote host can reach it.

## Residual auth risks (carried)

- The whitelist is only as good as the operator's `/pool invite
  --discord-user` record — a mis-invited Discord id is the boundary case.
- The per-version DEK wrap is the byte-authorization boundary (only an
  invited agent can decrypt fetched bytes).
- The relay sees both peers' IPs — but only in `relay` mode; Tor mode
  removes it.
- Tor is not the transport in the sense of hiding identity: application-
  layer identity (which agent) is still known to the pool operator.
