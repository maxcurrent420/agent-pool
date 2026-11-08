"""Ticket coordination service — gateway-native.

Hermes posts; the plugin only returns content. Every method returns a result
dict with the state word and a ``notice`` string; handlers format it via
``replies.format_result`` and Hermes posts it. The service never opens a
Discord client and never imports ``discord``.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
from pathlib import Path

try:  # package import (Hermes loads plugins as a package)
    from . import crypto as C
    from . import ledger as L
    from .propagation import Member, RepoPropagator
    from .transport import TransportBackend
except ImportError:  # flat import (pytest from repo root)
    import crypto as C
    import ledger as L
    from propagation import Member, RepoPropagator
    from transport import TransportBackend

STATE_MARKER = "[agent-pool:state={state}]"
MAX_BRIEF_CHARS = 3000  # coordination payloads stay under the 4 KB guard


def _env_get(name, default=""):
    """Env read via the profile secret scope (see plugin ``__init__``)."""
    try:
        from agent.secret_scope import get_secret

        val = get_secret(name, default)
        return default if val is None else val
    except Exception:
        return os.environ.get(name, default)


def _ticket_payload(ticket: dict) -> dict:
    """Forum-post body: title + mandate brief + state marker. Never repo bytes."""
    budget = json.loads(ticket.get("budget_json") or "{}")
    return {
        "ticket_id": ticket["ticket_id"],
        "title": ticket["title"],
        "repo_id": ticket["repo_id"],
        "brief": ticket.get("brief", "")[:MAX_BRIEF_CHARS],
        "budget": budget,
        "state": ticket["state"],
        "state_marker": STATE_MARKER.format(state=ticket["state"]),
    }


def _parse_env_allocations(raw: str) -> dict:
    """Parse ``AGENT_POOL_TASK_ALLOCATIONS`` ("agent:a=2,agent:b=3").

    Malformed pairs are ignored; the value must be a non-negative int.
    """
    out = {}
    for pair in (raw or "").split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        try:
            n = int(value.strip())
        except ValueError:
            continue
        if key.strip() and n >= 0:
            out[key.strip()] = n
    return out


def _fmt_num(value):
    """Render a usage/limit number without a trailing '.0'."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class PoolNotLocalError(Exception):
    """A gateway command named a pool not hosted by this agent (D3)."""

    def __init__(self, detail, pool_id=None):
        super().__init__(detail)
        self.detail = detail
        self.pool_id = pool_id

    @property
    def error_code(self):
        return "pool_not_local"


class PoolService:
    """Ticket coordination for one pool. Caller identity is injected."""

    def __init__(self, conn, *args,
                 transport=None, caller_key_id: str = "", caller_signing_key=None,
                 caller_box_sk=None, budget_defaults=None, pool_resolver=None,
                 gateway=None, transport_overrides=None,
                 agent_task_allocations=None, worktree_root=None,
                 maintainer_discord_user_id=None, clamd_socket="",
                 auto_provision=True):
        # Backwards-compat: old call sites passed (conn, gateway, transport).
        # Duck-type: anything with post_notice is a legacy gateway (ignored).
        if args:
            for _a in args:
                if hasattr(_a, "post_notice"):
                    gateway = _a
                elif hasattr(_a, "send"):
                    transport = _a
                elif transport is None:
                    transport = _a
                else:
                    raise TypeError(f"unexpected positional arg: {_a!r}")
        if transport is not None and hasattr(transport, "post_notice"):
            raise TypeError("PoolService no longer takes a gateway adapter")
        self._conn = conn
        self._transport = transport
        # Per-pool transport overrides are removed: Iroh-over-Tor is
        # the only byte transport, so overrides are always empty.
        # Any passed overrides are ignored (legacy config).
        self._transport_overrides: dict = {}
        self._caller_key_id = caller_key_id
        self._caller_sk = caller_signing_key
        self._caller_box_sk = caller_box_sk
        self._budget_defaults = budget_defaults
        self._pool_resolver = pool_resolver
        # Phase 3: per-agent task allocations (config mapping) + env override
        # read at construction. Precedence: env > config > max_tasks_per_day
        # config > 5. No pool-level budgets.
        self._agent_task_allocations = dict(agent_task_allocations or {})
        self._env_task_allocations = _parse_env_allocations(
            _env_get("AGENT_POOL_TASK_ALLOCATIONS", ""))
        # Maintainer Discord id from config (threaded in by _service_for);
        # AGENT_POOL_MAINTAINER_DISCORD_USER_ID env beats config.
        _maint_env = _env_get("AGENT_POOL_MAINTAINER_DISCORD_USER_ID", "").strip()
        if _maint_env:
            self._maintainer_discord_user_id = _maint_env
        else:
            self._maintainer_discord_user_id = (
                str(maintainer_discord_user_id).strip()
                if maintainer_discord_user_id else "")
        # The agent's configured checkout root for per-ticket worktrees.
        self._worktree_root = (
            worktree_root
            or _env_get("AGENT_POOL_WORKTREE_ROOT", "").strip()
            or "/tmp/agent-pool-worktrees").rstrip("/")
        # ClamAV ingestion-scan config (plugin.yaml config_schema keys,
        # threaded by the plugin host; defaults match plugin.yaml).
        self._clamd_socket = clamd_socket or "/run/clamav/clamd.ctl"
        self._auto_provision = bool(auto_provision)

    def _allocation_for(self, key_id) -> int:
        """Resolved task allocation for one agent on this pool."""
        if key_id in self._env_task_allocations:
            return self._env_task_allocations[key_id]
        if key_id in self._agent_task_allocations:
            return int(self._agent_task_allocations[key_id])
        try:
            return int((self._budget_defaults or {}).get("max_tasks_per_day", 5))
        except (TypeError, ValueError):
            return 5

    # -- pool resolution + isolation (D2/D3) ---------------------------------

    def _source_from(self, source=None):
        if source is not None:
            return source
        if callable(self._pool_resolver):
            try:
                return self._pool_resolver()
            except TypeError:
                return self._pool_resolver(source)
        return None

    @staticmethod
    def _source_field(source, *names):
        for n in names:
            if isinstance(source, dict) and source.get(n) is not None:
                return source.get(n)
            v = getattr(source, n, None) if source is not None else None
            if v is not None:
                return v
        return None

    def _resolve_pool(self, pool_id=None, source=None) -> str:
        """The hosted pool is THE single pool (one pool per gateway).

        This is a lookup-or-refuse of the single hosted pool, not a
        selection among candidates. There is no pool selection to perform.

        No pool named -> the one hosted pool. An explicit --pool naming
        anything but the hosted pool is refused (pool_not_local) naming
        the hosted pool. A pre-existing multi-pool DB is refused until
        the operator removes the extras (never auto-deleted).
        """
        hosts = L.list_local_pools(self._conn)
        if pool_id:
            pool_id = str(pool_id).strip()
            if pool_id:
                if pool_id not in hosts:
                    raise PoolNotLocalError(
                        f"pool {pool_id} is not hosted by this gateway (hosts: {', '.join(hosts) or 'none'})",
                        pool_id=pool_id)
                if len(hosts) != 1:
                    raise PoolNotLocalError(
                        f"multiple hosted pools ({', '.join(hosts)}) — one pool per gateway; "
                        f"the operator must remove the extras",
                        pool_id=pool_id)
                return pool_id
        if len(hosts) == 1:
            return hosts[0]
        if not hosts:
            raise PoolNotLocalError(
                "no pool hosted by this gateway",
                pool_id=pool_id or "")
        raise PoolNotLocalError(
            f"multiple hosted pools ({', '.join(hosts)}) — one pool per gateway; "
            f"the operator must remove the extras",
            pool_id=pool_id or "")

    def record_gateway(self, pool_id, gateway_endpoint="", transport_addr="", role="") -> dict:
        """Record a pool/gateway this agent interacts with (differentiation)."""
        return L.record_gateway(self._conn, pool_id,
                                gateway_endpoint=gateway_endpoint,
                                transport_addr=transport_addr, role=role)

    def list_gateways(self) -> list:
        """Pools/gateways this agent interacts with (not the hosted pool)."""
        return L.list_gateways(self._conn)

    def _assert_local_owned(self, pool_id=None, *, require_maintainer=False,
                            source=None) -> str:
        """Hosted-only: pool must exist locally. Owner checks are whitelist
        helpers; no key-based authz remains."""
        resolved = self._resolve_pool(pool_id, source=source)
        row = self._conn.execute("SELECT * FROM pools WHERE pool_id=?",
                                 (resolved,)).fetchone()
        if row is None:
            raise PoolNotLocalError(
                f"pool_not_local: pool {resolved!r} is not hosted by this agent",
                pool_id=resolved)
        return resolved

    def fetch_repo_list(self, pool_id=None, *, source=None) -> dict:
        """Hosted-pool repo list: repos + latest accepted version + available
        (unclaimed) tasks + gateway addressing (the /fetch repo list verb).

        Runs on the pool's own gateway (the ledger lives there).
        A foreign pool id is refused (pool_not_local) but recorded in
        ``pool_gateways`` so the agent can differentiate the pools it
        interacts with (interaction, not management).
        """
        if pool_id and str(pool_id).strip() not in L.list_local_pools(self._conn):
            self.record_gateway(str(pool_id).strip(), role="interact")
            raise PoolNotLocalError(
                f"pool {str(pool_id).strip()} is not hosted by this gateway "
                f"(hosts: {', '.join(L.list_local_pools(self._conn)) or 'none'})",
                pool_id=str(pool_id).strip())
        resolved = self._resolve_pool(pool_id, source=source)
        self._check_not_frozen(resolved)
        self._require_authorized(resolved, source)
        repos = []
        for r in L.list_repos(self._conn, resolved):
            available = [dict(t) for t in L.list_tickets(
                self._conn, pool_id=resolved, state="open", repo_id=r["repo_id"])]
            repos.append({
                "repo_id": r["repo_id"], "pool_id": r["pool_id"],
                "display_name": r["display_name"], "name": r["name"],
                "accepted_version": r["accepted_version"],
                "available_tasks": [
                    {"ticket_id": t["ticket_id"], "title": t["title"],
                     "claimed": False, "claimant_key_id": t.get("claimant_key_id")}
                    for t in available],
                "open_tickets": r["open_tickets"],
                "claimed_tickets": r["claimed_tickets"],
                "total_tickets": r["total_tickets"],
            })
        out = {"ok": True, "pool_id": resolved, "repos": repos,
               "gateway": self._gateway_addressing(resolved)}
        self._enforce_reply_guard(out)
        return out

    def fetch_task_list(self, pool_id=None, *, source=None, ticket=None) -> dict:
        """All non-completed tasks with claimed status (the /fetch task
        list verb). A claimed task is shown as claimed (and by which
        key), never as available. ``--ticket <id>`` returns one task's
        detail (completed tasks are returned on explicit request).
        """
        if pool_id and str(pool_id).strip() and str(pool_id).strip() in L.list_local_pools(self._conn):
            resolved = self._resolve_pool(pool_id, source=source)
        elif ticket:
            resolved = self.show_ticket(ticket)["pool_id"]
        else:
            resolved = self._resolve_pool(pool_id, source=source)
        self._check_not_frozen(resolved)
        self._require_authorized(resolved, source)
        if ticket:
            t = self.show_ticket(ticket)
            if t["pool_id"] != resolved:
                raise PoolNotLocalError(
                    f"pool_not_local: ticket {ticket!r} belongs to pool "
                    f"{t['pool_id']!r}, not pool {resolved!r}",
                    pool_id=t["pool_id"])
            row = dict(t)
            row["claimed"] = t["state"] == "claimed"
            row["claimant_key_id"] = t.get("claimant_key_id")
            return {"ok": True, "pool_id": resolved, "ticket": row}
        rows = [dict(t) for t in L.list_tickets(self._conn, pool_id=resolved, state="open")]
        rows += [dict(t) for t in L.list_tickets(self._conn, pool_id=resolved, state="claimed")]
        tasks = [{"ticket_id": t["ticket_id"], "title": t["title"],
                  "repo_id": t["repo_id"], "state": t["state"],
                  "claimed": t["state"] == "claimed",
                  "claimant_key_id": t.get("claimant_key_id")}
                 for t in rows]
        return {"ok": True, "pool_id": resolved, "tasks": tasks}

    def fetch_repo(self, repo_id, *, pool_id=None, source=None) -> dict:
        """The latest accepted signed manifest for one repo (the
        /fetch repo <repo_id> verb) — the artifact the contributor's
        plugin downloads with. Carried as ``manifest_json`` (canonical
        serialization the signature covers); OD3 4 KB guard applies.
        """
        resolved = self._resolve_pool(pool_id, source=source)
        self._check_not_frozen(resolved)
        self._require_authorized(resolved, source)
        manifest = L.get_latest_accepted_manifest(self._conn, repo_id)
        if manifest is None:
            raise ValueError(f"no ACCEPTED version for repo {repo_id!r} to fetch")
        try:
            from crypto import manifest_hash as _manifest_hash
        except Exception:
            from .crypto import manifest_hash as _manifest_hash
        out = {"ok": True, "pool_id": resolved, "repo_id": repo_id,
               "manifest_json": json.dumps(manifest, sort_keys=True),
               "manifest_hash": _manifest_hash(manifest),
               "transfer_ticket": manifest.get("transfer_ticket", "")}
        self._enforce_reply_guard(out)
        return out

    def fetch_repo_download(self, manifest, *, dest, source=None) -> dict:
        """Download + decrypt + clone one repo from a fetched manifest
        using the caller's own box key (the /fetch repo --manifest
        form), over the configured transport.

        Cross-gateway path (D6): this ALWAYS uses the wrapped-key ACL +
        BLAKE3 path (``verify_chain=False``) — there is no local-pool
        branch. If a local pool happens to host the repo, that is a
        different operation (``/fetch repo`` on that gateway). Full
        chain verification happens only on the hosted-pool ``fetch()``;
        this download path verifies the manifest signature (D4, against
        the signer's recorded verify key) + the wrapped-key ACL +
        BLAKE3 content hash. Discord carries only the manifest JSON;
        the agent on this gateway performs the download.
        """
        if not manifest:
            raise ValueError("fetch repo needs --manifest <json>")
        if not self._caller_box_sk:
            raise PermissionError("fetch repo needs the agent's own box key for DEK unwrap")
        wrapped = manifest.get("wrapped_keys") or {}
        if self._caller_key_id not in wrapped:
            raise PermissionError(
                "fetch repo refused: caller key not in manifest wrapped_keys (only whitelisted members can decrypt)")
        signer = manifest.get("signer_key_id", "")
        verify_hex = self._recorded_verify_key_hex(signer)
        if not verify_hex:
            raise RuntimeError("fetch refused: manifest signature does not verify")
        try:
            from nacl.encoding import RawEncoder
            from nacl.signing import VerifyKey
            signer_vk = VerifyKey(bytes.fromhex(verify_hex), encoder=RawEncoder)
        except Exception:
            raise RuntimeError("fetch refused: manifest signature does not verify")
        if not C.verify_manifest(signer_vk, manifest):
            raise RuntimeError("fetch refused: manifest signature does not verify")
        from propagation import Member as _M
        from propagation import RepoPropagator as _RP
        members = {self._caller_key_id: _M(key_id=self._caller_key_id, role="member",
                                           box_pubkey_hex="", box_sk=self._caller_box_sk)}
        if signer and signer not in members:
            members[signer] = _M(key_id=signer, role="maintainer",
                                 verify_key_hex=verify_hex,
                                 box_pubkey_hex="", box_sk=None)
        prop = _RP(self._conn, self._transport, pool_id="fetch",
                   maintainer_key_id=signer, maintainer_signing_key=None,
                   members=members)
        fetcher = members[self._caller_key_id]
        return prop.fetch(manifest=manifest, fetcher=fetcher, dest=Path(dest),
                          verify_chain=False)

    def _recorded_verify_key_hex(self, key_id) -> str:
        """The recorded verify key for ``key_id`` from this gateway's
        ledger: ``members.verify_pubkey_hex`` first, then
        ``identities.verify_key_hex``; the caller's own key falls back
        to its live identity. Empty when no key is recorded (the
        download path then refuses — it cannot verify the manifest)."""
        if not key_id:
            return ""
        if key_id == self._caller_key_id and self._caller_sk is not None:
            try:
                own = self._caller_sk.verify_key.encode().hex()
                if own:
                    return own
            except Exception:
                pass
        for sql in (
            "SELECT verify_pubkey_hex FROM members WHERE key_id=? AND verify_pubkey_hex != '' LIMIT 1",
            "SELECT verify_key_hex FROM identities WHERE key_id=? AND verify_key_hex != '' LIMIT 1",
        ):
            try:
                row = self._conn.execute(sql, (key_id,)).fetchone()
            except Exception:
                row = None
            if row and row[0]:
                return row[0]
        return ""

    def _gateway_addressing(self, pool_id) -> dict:
        """Transport addressing for this gateway's hosted pool (endpoint
        id; under Tor, the hidden-service identity). transport_addr is
        intentionally empty and the destination bot is named by the user."""
        try:
            prop = self._propagator(pool_id)
            info = prop._transport_info()
        except Exception:
            info = {"transport_mode": "", "relay_url": ""}
        # Ledger relay_url column is legacy/ignored (no relay transport).
        return {"endpoint": pool_id,
                "transport_addr": "",
                "transport_mode": info.get("transport_mode", ""),
                "relay_url": "",
                "transport_note": info.get("transport_note", "")}

    def _enforce_reply_guard(self, out: dict) -> None:
        """OD3: fail loudly rather than silently truncate a manifest."""
        try:
            from replies import MAX_CONTENT_BYTES
        except Exception:
            MAX_CONTENT_BYTES = 4096
        size = len(json.dumps(out, indent=2, sort_keys=True).encode("utf-8"))
        if size > MAX_CONTENT_BYTES:
            raise ValueError(
                f"fetch reply {size} bytes exceeds the {MAX_CONTENT_BYTES}-byte "
                f"gateway limit (manifest {out.get('manifest_hash','?')});"
                f" hash-addressed retrieval is deferred")
    def _thread_ref_from(self, source=None):
        src = self._source_from(source)
        if src is None:
            return None
        chat_id = self._source_field(src, "chat_id", "parent_chat_id", "channel_id")
        thread_id = self._source_field(src, "thread_id")
        guild_id = self._source_field(src, "guild_id", "scope_id")
        if chat_id is None and thread_id is None and guild_id is None:
            return None
        return {"chat_id": chat_id, "thread_id": thread_id, "guild_id": guild_id}

    def _queue_notice(self, pool_id, text, kind="notice") -> None:
        L.queue_notice(self._conn, pool_id, text, kind=kind)

    def drain_notices(self, pool_id=None):
        """Unread notices accumulated since last drain (D5)."""
        return L.drain_notices(self._conn, pool_id=pool_id)

    def peek_notices(self, pool_id=None):
        return L.peek_notices(self._conn, pool_id=pool_id)

    # -- pool membership ----------------------------------------------------

    def register_pool(self, pool_id, name, *, relay_url="", source=None,
                      maintainer_discord_user_id="", key_id=None) -> dict:
        # Hard tor gate: a real transport (one with _require_tor_ready)
        # must probe tor_ready() before registration. FakeTransport
        # (test-only) has no such method and is exempt. Fail closed.
        _gate = getattr(self._transport, "_require_tor_ready", None)
        if callable(_gate):
            _gate()
        if L.is_global_frozen(self._conn):
            raise L.ClaimRefused(L.FROZEN_MESSAGE)
        discord_user_id = (
            str(maintainer_discord_user_id).strip()
            or getattr(self, "_maintainer_discord_user_id", "")
            or _env_get("AGENT_POOL_MAINTAINER_DISCORD_USER_ID", "").strip())
        if not discord_user_id:
            raise ValueError("register needs maintainer_discord_user_id: set it in config or AGENT_POOL_MAINTAINER_DISCORD_USER_ID")
        maintainer = key_id or self._caller_key_id
        if not maintainer:
            raise ValueError("register needs a maintainer key_id")
        # Phase 2: the pool maintainer IS the gateway owner — the maintainer
        # must be this gateway's identity. No second owner / second pool.
        if maintainer != self._caller_key_id:
            raise ValueError("register refused: maintainer must be the gateway owner")
        # relay_url is legacy/ignored; the column remains for compat.
        L.create_pool(self._conn, pool_id, name, maintainer, relay_url="", owner_discord_user_id=discord_user_id, owner_gateway_key_id=self._caller_key_id)
        # Record the maintainer's box pubkey so submit wraps the DEK for the
        # maintainer too (fixes "maintainer has no wrapped DEK" at audit).
        _irow = self._conn.execute(
            "SELECT box_pubkey_hex, verify_key_hex FROM identities WHERE key_id=?", (maintainer,)).fetchone()
        _box = _irow[0] if _irow else ""
        _vhex = _irow[1] if _irow else ""
        if not _box and self._caller_box_sk is not None and maintainer == self._caller_key_id:
            try:
                _box = self._caller_box_sk.public_key.encode().hex()
            except Exception:
                _box = ""
        L.add_member(self._conn, pool_id, maintainer, role="maintainer", box_pubkey_hex=_box or "",
                     verify_pubkey_hex=_vhex or "")
        L.authorize_user(self._conn, pool_id, discord_user_id)
        L.record_event(self._conn, pool_id, "pool_registered",
                       {"pool_id": pool_id, "maintainer": maintainer,
                        "owner_discord_user_id": str(discord_user_id)})
        # Register authorizes the owner only. Key attribution is NOT bound
        # here: it binds on the user's first claim (pool_memberships), and
        # the whitelist itself is synced from env/config by _service_for.
        return {"ok": True, "pool_id": pool_id, "name": name, "maintainer": maintainer,
                "owner_discord_user_id": str(discord_user_id)}

    def members(self, pool_id, source=None) -> list:
        pool_id = self._resolve_pool(pool_id, source=source)
        # Authorized-only read verb (whitelist model).
        self._require_pool_authorized(pool_id, source)
        return L.list_members(self._conn, pool_id)

    def freeze(self, pool_id, frozen: bool, *, source=None) -> dict:
        self._require_owner(pool_id, source)
        L.set_frozen(self._conn, pool_id, frozen)
        notice = f"pool {pool_id} {'frozen' if frozen else 'unfrozen'}"
        self._queue_notice(pool_id, notice, kind="freeze")
        return {"ok": True, "pool_id": pool_id, "frozen": frozen, "notice": notice}

    def remove_member(self, pool_id, key_id, *, source=None) -> dict:
        """Revoke a member: registry removal + forward-separation notice.

        Future manifests omit the removed member's wrapped key (fresh DEK per
        version). Already-fetched versions are inherently unfetchable
        retroactively — the limitation is stated in the thread notice.
        """
        self._check_not_frozen(pool_id)
        self._require_owner(pool_id, source)
        pool = self._conn.execute("SELECT * FROM pools WHERE pool_id=?",
                                  (pool_id,)).fetchone()
        if pool is None:
            raise ValueError(f"unknown pool: {pool_id!r}")
        # Drop the whitelist row for the removed member's bound Discord id.
        # Binding lives in pool_memberships (first-claim, Phase 1);
        # identities.discord_user_id has no production writer.
        try:
            _row = self._conn.execute(
                "SELECT discord_user_id FROM pool_memberships WHERE pool_id=? AND key_id=?",
                (pool_id, key_id)).fetchone()
            _discord_user_id = _row[0] if _row else None
        except Exception:
            _discord_user_id = None
        removed = L.remove_member(self._conn, pool_id, key_id)
        if _discord_user_id:
            L.deauthorize_user(self._conn, pool_id, _discord_user_id)
        notice = (f"member removed: {key_id} (forward separation only: "
                  f"{L.REVOCATION_LIMITATION})")
        self._queue_notice(pool_id, notice, kind="member_removed")
        removed = dict(removed)
        removed["notice"] = notice
        return removed

    def is_frozen(self, pool_id) -> bool:
        return L.is_frozen(self._conn, pool_id)

    def is_global_frozen(self) -> bool:
        return L.is_global_frozen(self._conn)

    def _check_not_frozen(self, pool_id) -> None:
        """Global kill-switch first, then per-pool freeze (Phase 6)."""
        L.check_not_frozen(self._conn, pool_id)

    def freeze_global(self, frozen: bool, *, source=None) -> dict:
        """GLOBAL kill-switch: freeze/unfreeze EVERY pool in the process.

        Owner-only (whitelist model): requires ownership of every hosted
        pool. Sets the ledger ``meta`` flag (persists across restarts) and
        mirrors it into each pool's per-pool flag so ``is_frozen`` stays
        truthful. While set, all pool-mutating, claim, and push actions
        raise (see ``_check_not_frozen``); only this command (to
        unfreeze) and read-only queries are allowed. Notice posted to every
        pool channel.
        """
        hosts = [r[0] for r in self._conn.execute("SELECT pool_id FROM pools")]
        for _h in hosts:
            self._require_owner(_h, source)
        L.set_global_frozen(self._conn, frozen)
        pools = hosts
        notices = []
        for pool_id in pools:
            L.set_frozen(self._conn, pool_id, frozen)
            notice = (f"pool {pool_id} {'frozen' if frozen else 'unfrozen'} "
                      f"(global kill-switch {'set' if frozen else 'cleared'})")
            self._queue_notice(pool_id, notice, kind="freeze")
            notices.append(notice)
        return {"ok": True, "global_frozen": bool(frozen), "pools": pools,
                "notice": "; ".join(notices) if notices else ""}

    # -- tickets --------------------------------------------------------------

    def add_ticket(self, pool_id, repo_id, title, brief="", budget=None,
                   source=None) -> dict:
        if not title.strip():
            raise ValueError("ticket title is required")
        pool_id = self._resolve_pool(pool_id, source=source)
        self._check_not_frozen(pool_id)
        self._require_owner(pool_id, source)
        self._assert_local_owned(pool_id, source=source)
        L.ensure_repo(self._conn, repo_id, pool_id)
        full_budget = L.normalize_budget(budget, self._budget_defaults)
        ticket_id = f"t-{secrets.token_hex(4)}"
        thread_ref = self._thread_ref_from(source)
        thread_ref_str = (f"chat:{thread_ref['chat_id']}:thread:{thread_ref['thread_id']}:"
                          f"guild:{thread_ref['guild_id']}" if thread_ref else None)
        state = "open"
        L.create_ticket(self._conn, ticket_id, pool_id, repo_id, title.strip(),
                        brief=brief, budget=full_budget, thread_ref=thread_ref_str, state=state)
        ticket = L.get_ticket(self._conn, ticket_id)
        L.record_event(self._conn, pool_id, "ticket_created",
                       {"ticket_id": ticket_id, "title": title,
                        "thread_ref": thread_ref_str})
        ticket = dict(ticket)
        ticket["thread_ref"] = thread_ref_str
        ticket["thread_ref_parts"] = thread_ref
        ticket["notice"] = (f"{STATE_MARKER.format(state=ticket.get('state','open'))} ticket {ticket_id}: "
                            f"{title}")
        self._queue_notice(pool_id, ticket["notice"], kind="ticket_created")
        return ticket

    def list_tickets(self, pool_id=None, state=None, source=None, repo_id=None) -> list:
        if repo_id:
            repo = L.get_repo(self._conn, repo_id)
            if repo is None:
                raise ValueError(f"unknown repo: {repo_id!r}")
            if pool_id:
                pool_id = self._resolve_pool(pool_id, source=source)
                if repo["pool_id"] != pool_id:
                    raise PoolNotLocalError(
                        f"pool_not_local: repo {repo_id!r} belongs to pool "
                        f"{repo['pool_id']!r}, not pool {pool_id!r}",
                        pool_id=repo["pool_id"])
            else:
                pool_id = repo["pool_id"]
        elif pool_id:
            pool_id = self._resolve_pool(pool_id, source=source)
        else:
            try:
                pool_id = self._resolve_pool(None, source=source)
            except PoolNotLocalError:
                pool_id = None
        # Authorized-only read verb (whitelist model).
        self._require_pool_authorized(pool_id, source)
        return L.list_tickets(self._conn, pool_id=pool_id, state=state, repo_id=repo_id)

    def _require_pool_authorized(self, pool_id, source) -> None:
        """Authorized-only read verb (2109): `/task list|show`,
        `/pool list|members`, `/audit show` are not public."""
        if not pool_id:
            return
        self._require_authorized(pool_id, source)

    def _is_bound_maintainer(self, pool_id, source=None) -> bool:
        # Whitelist model: owner check by Discord id, not gateway key.
        if not pool_id:
            return False
        src = self._source_from(source)
        user_id = self._source_field(src, "user_id", "discord_user_id")
        if user_id is None:
            return False
        return L.is_owner(self._conn, pool_id, str(user_id))

    def _discord_user(self, source) -> str | None:
        src = self._source_from(source)
        user_id = self._source_field(src, "user_id", "discord_user_id")
        return str(user_id) if user_id is not None else None

    def _whitelist_bound_key(self, pool_id=None, source=None) -> str | None:
        """Whitelist-bound agent key for the driving Discord user (Phase 1).

        Reads ``pool_memberships`` (first-claim binding). Attribution only —
        crypto records WHO acted (overlap checks, manifests, notices). The
        Discord id is the authorization; this lookup never decides access.
        Returns the bound key_id, or None when unbound (callers fall back
        to the gateway identity).
        """
        user_id = self._discord_user(source)
        if user_id is None:
            return None
        try:
            return L.lookup_membership_key(self._conn, user_id, pool_id=pool_id)
        except Exception:
            return None

    def _caller_box_pubkey_hex(self) -> str:
        if self._caller_box_sk is None:
            return ""
        try:
            return self._caller_box_sk.public_key.encode().hex()
        except Exception:
            return ""

    def _caller_verify_key_hex(self) -> str:
        if self._caller_sk is None:
            return ""
        try:
            return self._caller_sk.verify_key.encode().hex()
        except Exception:
            return ""

    def _ensure_member(self, pool_id, key_id) -> None:
        """Provision the ``members`` row for a key (DEK-wrap registry).

        The whitelist authorizes the Discord id, and first-claim binds
        it to a key — but DEKs are wrapped per ``members`` row, and in
        production nothing except ``register_pool`` ever wrote one.
        Without this row, a whitelisted/bound user gets no wrapped DEK
        in future repo versions and cannot fetch bytes. Call this for
        the bound/presented key at claim time.

        Box/verify keys come from, in order: the existing member row,
        the recorded ``identities`` row, or the live caller identity
        when ``key_id`` is this gateway's own key. Never clobbers a
        non-empty recorded value. Only a local caller key can be
        provisioned with a box pubkey here; a foreign key with no
        recorded pubkey still gets a row (future versions skip
        wrapping it until a pubkey is recorded, exactly as a member
        with an empty ``box_pubkey_hex`` is skipped today).

        Limitation: this cannot create a wrapped DEK for a repo
        version that was submitted BEFORE this key was provisioned —
        the DEK plaintext is gone by then. The user waits for the
        next version (noted in the ``add_repo`` notice).
        """
        key_id = str(key_id or "").strip()
        if not pool_id or not key_id:
            return
        existing = self._conn.execute(
            "SELECT role, box_pubkey_hex, verify_pubkey_hex FROM members"
            " WHERE pool_id=? AND key_id=?", (pool_id, key_id)).fetchone()
        role = (existing[0] if existing and existing[0] else None)
        box = (existing[1] if existing and existing[1] else "")
        verify = (existing[2] if existing and existing[2] else "")
        if not box or not verify:
            try:
                irow = self._conn.execute(
                    "SELECT box_pubkey_hex, verify_key_hex FROM identities"
                    " WHERE key_id=?", (key_id,)).fetchone()
            except Exception:
                irow = None
            if irow:
                box = box or (irow[0] or "")
                verify = verify or (irow[1] or "")
        if key_id == self._caller_key_id:
            box = box or self._caller_box_pubkey_hex()
            verify = verify or self._caller_verify_key_hex()
        if not role:
            prow = self._conn.execute(
                "SELECT maintainer_key_id FROM pools WHERE pool_id=?",
                (pool_id,)).fetchone()
            role = "maintainer" if (prow and prow[0] == key_id) else "member"
        if existing and box == (existing[1] or "") and verify == (existing[2] or "") and role == existing[0]:
            return
        L.add_member(self._conn, pool_id, key_id, role=role,
                     box_pubkey_hex=box or "", verify_pubkey_hex=verify or "")

    def _require_owner(self, pool_id, source) -> None:
        # No legacy skip: absence of an owner row = refusal.
        # Phase 2: owner == (Discord owner) AND (same gateway). The gateway
        # arm only gates maintaining/hosting verbs (this method's callers);
        # fetch/claim use _require_authorized and are unaffected, so a
        # cross-gateway agent interacting with a remote pool still works.
        # Key rotation: if the gateway identity changed, owner verbs are
        # refused until the pool is re-registered; the stored column is
        # never silently updated.
        user_id = self._discord_user(source)
        if user_id is None or not L.is_owner(self._conn, pool_id, user_id):
            raise PermissionError(f"not authorized for pool {pool_id} — ask the maintainer")
        row = self._conn.execute(
            "SELECT owner_gateway_key_id, maintainer_key_id FROM pools WHERE pool_id=?",
            (pool_id,)).fetchone()
        owner_gateway = (row[0] if row and row[0] else
                         row[1] if row else None)
        if owner_gateway != self._caller_key_id:
            raise PermissionError(
                "gateway identity changed: caller is not the pool's owner gateway — "
                "re-register the pool to continue")

    def _require_authorized(self, pool_id, source) -> None:
        # No legacy skip: absence of whitelist rows = refusal.
        user_id = self._discord_user(source)
        if user_id is None or not L.is_authorized(self._conn, pool_id, user_id):
            raise PermissionError(f"not authorized for pool {pool_id} — ask the maintainer")


    # -- project / repo registry ----------------------------------------------

    def add_repo(self, pool_id, repo_id, path, display_name="", source=None) -> dict:
        """Register a project and submit its bytes as the next repo version.

        Registry row (upsert) + propagation submit (pack -> BLAKE3 -> DEK
        wrap -> Iroh send -> signed manifest appended to the chain).
        Re-running submits a NEW version (next_repo_version), never refuses.
        ``path`` is required: a project without bytes is not a project.
        The old ``--link`` pointer form is deleted (OD5).
        """
        if not path:
            raise ValueError(
                "add-repo needs --path <abs/path>: a project without bytes "
                "is not a project; pass the local checkout to submit")
        if not repo_id:
            raise ValueError("add-repo needs a repo_id (or --path to derive one)")
        pool_id = self._resolve_pool(pool_id, source=source)
        self._check_not_frozen(pool_id)
        self._require_owner(pool_id, source)
        self._assert_local_owned(pool_id, source=source)
        repo_path = Path(path)
        if not repo_path.is_dir():
            raise ValueError(f"repo path is not a directory: {path!r}")
        if self._caller_sk is None:
            raise RuntimeError("add-repo needs the local signing key")
        L.upsert_repo(self._conn, repo_id, pool_id,
                      path=str(repo_path.resolve()),
                      display_name=display_name or None,
                      submitter_key_id=self._caller_key_id or None)
        version = L.next_repo_version(self._conn, repo_id)
        # Persist the submitting gateway's member row so manifests
        # wrap the DEK for it even if no claim has provisioned it.
        self._ensure_member(pool_id, self._caller_key_id)
        prop = self._propagator(pool_id)
        submitter = prop._members.get(self._caller_key_id)
        if submitter is None:
            raise PermissionError(
                f"add-repo refused: caller {self._caller_key_id!r} is not a pool member")
        result = prop.submit(repo_path=repo_path, repo_id=repo_id, version=version,
                             submitter=submitter,
                             submitter_signing_key=self._caller_sk)
        L.record_event(self._conn, pool_id, "repo_added",
                       {"repo_id": repo_id, "version": version,
                        "state": result["state"],
                        "manifest_hash": result["manifest_hash"]})
        # DEK timing: members whitelisted AFTER this version have no wrapped
        # key for it and must wait for the next version.
        members_with_box = [m for m in prop._members.values() if m.box_pubkey_hex]
        wrapped = set((result["manifest"].get("wrapped_keys") or {}).keys())
        missing = [m.key_id for m in members_with_box if m.key_id not in wrapped]
        notice = (f"project {repo_id} registered: v{version} "
                  f"{result['state']} ({result['manifest_hash'][:12]})")
        if missing:
            notice += (f"; WARNING: {len(missing)} member(s) have no DEK key "
                       f"for this version ({', '.join(sorted(missing))}) — whitelist "
                       "members BEFORE /add-repo; late joiners wait for the "
                       "next version")
        else:
            notice += ("; whitelist members BEFORE /add-repo — members whitelisted "
                       "later wait for the next version")
        return {"ok": True, "repo_id": repo_id, "pool_id": pool_id,
                "version": version, "state": result["state"],
                "manifest_hash": result["manifest_hash"],
                "transfer_id": result.get("transfer_id", ""),
                "notice": notice}


    def list_projects(self, pool_id=None, source=None) -> list:
        """Hosted-pool-only project listing for /pool list.

        Authorized-only read verb (whitelist model). One pool per gateway:
        when no pool is named, the single hosted pool is resolved via
        _resolve_pool; a PoolNotLocalError (no/foreign/multiple hosted
        pools) is re-raised as pool_not_local — never aggregated across
        hosted pools.
        """
        if pool_id:
            resolved = self._resolve_pool(pool_id, source=source)
            self._require_pool_authorized(resolved, source)
            return self._project_rows(resolved)
        bound = self._resolve_pool(None, source=source)
        self._require_pool_authorized(bound, source)
        return self._project_rows(bound)

    def _project_rows(self, pool_id) -> list:
        rows = []
        for r in L.list_repos(self._conn, pool_id):
            accepted = r.get("accepted_version")
            row = {"repo_id": r["repo_id"], "pool_id": r["pool_id"],
                   "display_name": r["display_name"], "name": r["name"],
                   "accepted_version": accepted,
                   "open_tickets": r["open_tickets"],
                   "claimed_tickets": r["claimed_tickets"],
                   "total_tickets": r["total_tickets"],
                   "submitter_key_id": r.get("submitter_key_id") or ""}
            if accepted is None:
                row["note"] = ("registered via ticket, no bytes submitted "
                               "yet — run /add-repo --path <checkout>")
            rows.append(row)
        return rows

    def show_ticket(self, ticket_id) -> dict:
        t = L.get_ticket(self._conn, ticket_id)
        if t is not None:
            return t
        raise ValueError(f"unknown ticket: {ticket_id!r}")

    def _worktree_spec(self, repo_id, ticket_id):
        """Per-ticket worktree spec handed to the agent (Phase 3).

        The plugin specifies; the agent runs the git operations at the
        returned path/branch. No git is shelled out here.
        """
        worktree_path = f"{self._worktree_root}/{repo_id}/worktree-{ticket_id}"
        branch = f"pool/{ticket_id}"
        manifest = L.get_latest_accepted_manifest(self._conn, repo_id)
        version = manifest.get("version") if manifest else None
        base_ref = f"v{version}" if version is not None else ""
        return worktree_path, branch, base_ref

    def _active_cleanup_notice(self, ticket_id) -> str | None:
        """Exact worktree cleanup notice for the ticket's active claim.

        The agent performs the git cleanup; the notice text is the contract
        (no cleanup hook exists).
        """
        claim = L.get_active_claim(self._conn, ticket_id)
        if claim is None or not claim.get("worktree_path") or not claim.get("branch"):
            return None
        return (f"release worktree {claim['worktree_path']}"
                f" and delete branch {claim['branch']}")

    def _set_state_and_notify(self, ticket, state) -> dict:
        updated = L.set_ticket_state(self._conn, ticket["ticket_id"], state)
        notice = (f"{STATE_MARKER.format(state=state)} ticket "
                  f"{updated['ticket_id']}: {updated['title']}")
        self._queue_notice(updated["pool_id"], notice, kind="ticket_state")
        updated = dict(updated)
        updated["notice"] = notice
        return updated

    # -- claims ---------------------------------------------------------------

    def claim(self, ticket_id, key_id=None, declared_paths=None, budget=None,
              maintainer_override=False, *, source=None) -> dict:
        ticket = self.show_ticket(ticket_id)
        # Freeze is absolute: the exact message for every user.
        self._check_not_frozen(ticket["pool_id"])
        # Whitelist: only authorized users may claim (the Discord id is the
        # authorization; the bound key lookup is attribution, not authz).
        self._require_authorized(ticket["pool_id"], source)
        # First-claim key binding (Phase 1): the presented key is
        # explicit key_id, else the gateway identity. If this user already
        # has a bound key, the presented key MUST match it — the binding is
        # permanent; a different key is refused with the exact message
        # below. If unbound, the presented key binds on a successful claim.
        user_id = self._discord_user(source)
        bound = self._whitelist_bound_key(ticket["pool_id"], source)
        presented = key_id or self._caller_key_id
        if bound and presented and presented != bound:
            raise PermissionError(
                f"claim refused: user {user_id} already bound to key {bound} "
                f"on pool {ticket['pool_id']} — presented key {presented} differs; "
                "first-claim binding is permanent")
        key = bound or presented
        if not key:
            raise ValueError("claim needs a claimant key_id")
        # Phase 3: the agent's task-count budget is the primary stop. The
        # budget belongs to the agent (claimant key), not the pool/ticket.
        agent_budget = L.ensure_agent_budget(
            self._conn, ticket["pool_id"], key, self._allocation_for(key))
        if agent_budget["tasks_completed"] >= agent_budget["task_allocation"]:
            raise L.ClaimRefused(
                f"agent budget exhausted: {agent_budget['tasks_completed']} of"
                f" {agent_budget['task_allocation']} tasks completed on pool"
                f" {ticket['pool_id']} — no further claims allowed")
        worktree_path, branch, base_ref = self._worktree_spec(
            ticket["repo_id"], ticket_id)
        claim = L.claim_ticket_enforced(
            self._conn, f"c-{secrets.token_hex(4)}", ticket_id, key,
            declared_paths=declared_paths, budget=budget,
            maintainer_override=maintainer_override,
            worktree_path=worktree_path, branch=branch, base_ref=base_ref)
        if not bound and user_id and key:
            L.bind_membership(self._conn, ticket["pool_id"], user_id, key)
        if user_id and key:
            # Provision the crypto member row so FUTURE repo versions
            # wrap a DEK for this user's bound key. Without it the
            # user is whitelisted+bound yet has no wrapped DEK and
            # fetch refuses them. Runs on every claim (not just the
            # first) so legacy bound users with no member row get
            # provisioned too.
            self._ensure_member(ticket["pool_id"], key)
        ticket = L.get_ticket(self._conn, ticket_id)
        notice = (f"{STATE_MARKER.format(state='claimed')} ticket {ticket_id} "
                  f"claimed by {key}")
        self._queue_notice(ticket["pool_id"], notice, kind="ticket_claim")
        claim = dict(claim)
        claim["notice"] = notice
        claim["state_word"] = "claimed"
        claim["worktree_path"] = worktree_path
        claim["branch_name"] = branch
        claim["branch"] = branch
        claim["base_ref"] = base_ref
        claim["budget"] = json.loads(claim.get("budget_json") or "{}")
        claim["stop_condition"] = {"condition": "budget_exhausted",
                                   "enforce": True}
        # Workflow C: the claim reply carries the accepted repo's Tor
        # download link + manifest directly, so the contributor needs no
        # local lookup — everything needed arrives from the pool gateway.
        # The 4096-byte reply guard is respected: when the full manifest
        # would overflow it, carry a preview + hash + the /fetch repo
        # instruction instead (same guard ``replies.py`` enforces).
        accepted = L.get_latest_accepted_manifest(self._conn, ticket["repo_id"])
        if accepted is not None:
            from propagation import manifest_hash as _manifest_hash
            claim["repo_id"] = ticket["repo_id"]
            claim["manifest_hash"] = _manifest_hash(accepted)
            claim["transfer_ticket"] = accepted.get("transfer_ticket", "")
            candidate = dict(claim)
            candidate["manifest"] = accepted
            full = json.dumps({"ok": True, "claim": candidate},
                              indent=2, sort_keys=True, ensure_ascii=False)
            if len(full.encode("utf-8")) <= 3400:
                claim["manifest"] = accepted
            else:
                claim["manifest_preview"] = {
                    "repo_id": accepted.get("repo_id"),
                    "version": accepted.get("version"),
                    "content_hash": accepted.get("content_hash"),
                    "transfer_ticket": accepted.get("transfer_ticket", ""),
                }
                claim["manifest_truncated"] = True
                claim["fetch_instruction"] = (
                    f"the full accepted manifest exceeds the reply size "
                    f"limit; run /fetch repo {ticket['repo_id']} on the "
                    f"pool gateway to obtain it (manifest_hash above "
                    f"pins the exact version)")
        return claim

    def claim_any(self, pool_id=None, repo_id=None, max_tasks=1, key_id=None,
                  budget=None, maintainer_override=False, source=None) -> dict:
        """Claim up to ``max_tasks`` open tickets, whole-ticket form (no
        paths pre-declared), across any project in the pool (or one repo).

        Every individual claim still goes through the enforced path
        (freeze, one-ticket-one-worker, overlap, budget), so the bound is
        ``max_tasks`` successful claims. Tickets that refuse (e.g. a live
        file claim on the same repo) are reported as skipped and the
        search continues. Bounded hard at 10 tickets per call.
        """
        max_tasks = int(max_tasks or 1)
        if max_tasks < 1:
            raise ValueError("max_tasks must be >= 1")
        max_tasks = min(max_tasks, 10)
        key = key_id or self._caller_key_id
        if not key:
            raise ValueError("claim needs a claimant key_id")
        open_tickets = self.list_tickets(pool_id=pool_id, state="open",
                                         source=source, repo_id=repo_id)
        claimed, skipped = [], []
        for ticket in open_tickets:
            if len(claimed) >= max_tasks:
                break
            try:
                c = self.claim(ticket["ticket_id"], key_id=key,
                               declared_paths=[], budget=budget,
                               maintainer_override=maintainer_override, source=source)
                claimed.append({"ticket_id": ticket["ticket_id"],
                                "title": ticket["title"],
                                "repo_id": ticket["repo_id"],
                                "claim_id": c.get("claim_id", ""),
                                "worktree_path": c.get("worktree_path", ""),
                                "branch_name": c.get("branch_name", ""),
                                "branch": c.get("branch", ""),
                                "base_ref": c.get("base_ref", ""),
                                "budget": c.get("budget", {}),
                                "stop_condition": c.get("stop_condition", {})})
            except (L.ClaimRefused, ValueError) as exc:
                skipped.append({"ticket_id": ticket["ticket_id"],
                                "reason": str(exc)})
        notice = (f"claim-any: claimed {len(claimed)} ticket(s)"
                  + (f"; skipped {len(skipped)}" if skipped else ""))
        return {"ok": True, "claimed": claimed, "skipped": skipped,
                "count": len(claimed), "notice": notice}

    def release(self, ticket_id, key_id=None, *, source=None) -> dict:
        ticket = self.show_ticket(ticket_id)
        # Freeze is absolute: the exact message for every user.
        self._check_not_frozen(ticket["pool_id"])
        # Whitelist: authorized-level verb.
        self._require_authorized(ticket["pool_id"], source)
        key = key_id or self._whitelist_bound_key(ticket["pool_id"], source) or self._caller_key_id
        cleanup_notice = self._active_cleanup_notice(ticket_id)
        updated = L.release_ticket(self._conn, ticket_id, key)
        notice = f"{STATE_MARKER.format(state='open')} ticket {ticket_id} released"
        self._queue_notice(updated["pool_id"], notice, kind="ticket_release")
        updated = dict(updated)
        updated["notice"] = notice
        if cleanup_notice:
            updated["cleanup_notice"] = cleanup_notice
            self._queue_notice(updated["pool_id"], cleanup_notice,
                               kind="worktree_cleanup")
        return updated

    def complete(self, ticket_id, key_id=None, *, source=None) -> dict:
        """Complete a claimed ticket (terminal ``completed`` state)."""
        ticket = self.show_ticket(ticket_id)
        self._check_not_frozen(ticket["pool_id"])
        self._require_authorized(ticket["pool_id"], source)
        key = key_id or self._whitelist_bound_key(ticket["pool_id"], source) or self._caller_key_id
        cleanup_notice = self._active_cleanup_notice(ticket_id)
        updated = L.complete_ticket(self._conn, ticket_id, key)
        notice = f"{STATE_MARKER.format(state='completed')} ticket {ticket_id} completed"
        self._queue_notice(updated["pool_id"], notice, kind="ticket_completed")
        updated = dict(updated)
        updated["notice"] = notice
        if cleanup_notice:
            updated["cleanup_notice"] = cleanup_notice
            self._queue_notice(updated["pool_id"], cleanup_notice,
                               kind="worktree_cleanup")
        return updated

    # -- Phase 4: push write path (grant -> push -> deliver -> complete) -----

    def push_repo(self, repo_id, ticket_id, *, pool_id=None, source=None) -> dict:
        """Issue a one-time push grant for a claimed task (the ``/push
        repo <repo_id> --ticket <id>`` verb, on the POOL's gateway).

        Claimant-only: the ticket must be ``claimed`` by the driving
        user's whitelist-bound key. The grant token plus the gateway
        addressing is the push link; the contributor's agent pushes the
        diff over the configured transport and delivers the transfer
        ticket + grant back (see :meth:`push_deliver`).
        """
        resolved = self._resolve_pool(pool_id, source=source)
        self._check_not_frozen(resolved)
        self._require_authorized(resolved, source)
        ticket = self.show_ticket(ticket_id)
        if ticket["pool_id"] != resolved:
            raise PoolNotLocalError(
                f"pool_not_local: ticket {ticket_id!r} belongs to pool "
                f"{ticket['pool_id']!r}, not pool {resolved!r}",
                pool_id=ticket["pool_id"])
        if repo_id and ticket["repo_id"] != repo_id:
            raise ValueError(
                f"push refused: ticket {ticket_id!r} is on repo "
                f"{ticket['repo_id']!r}, not {repo_id!r}")
        key = self._whitelist_bound_key(resolved, source) or self._caller_key_id
        if ticket["state"] != "claimed" or ticket.get("claimant_key_id") != key:
            raise PermissionError(
                f"push refused: ticket {ticket_id!r} is {ticket['state']!r} "
                f"(claimant {ticket.get('claimant_key_id')!r}); only the "
                f"claimant may push")
        token = secrets.token_hex(16)
        grant = L.create_push_grant(self._conn, token, resolved, ticket_id, key)
        L.record_event(self._conn, resolved, "push_granted",
                       {"ticket_id": ticket_id, "repo_id": ticket["repo_id"],
                        "key_id": key})
        gateway = self._gateway_addressing(resolved)
        notice = (f"push link issued for ticket {ticket_id} "
                  f"(repo {ticket['repo_id']}): token {token[:12]}…")
        self._queue_notice(resolved, notice, kind="push_granted")
        return {"ok": True, "pool_id": resolved, "repo_id": ticket["repo_id"],
                "ticket_id": ticket_id, "token": token,
                "push_link": {"token": token, "gateway": gateway,
                              "repo_id": ticket["repo_id"],
                              "ticket_id": ticket_id},
                "gateway": gateway, "grant": grant, "notice": notice}

    def push_build_bundle(self, repo_path, repo_id, *, version=None,
                          pool_id=None, source=None) -> dict:
        """Build a contributor push bundle on the contributor's own
        gateway (the push step): pack -> hash -> DEK wrap -> encrypt ->
        transport send -> contributor-signed manifest, with no ledger
        write. Thin wrapper over the propagator so the contributor's
        plugin drives it through the service; *version* defaults to the
        next repo version.

        Agent-facing, service-level only (no slash command): the AGENT
        on the gateway calls this with its local ``repo_path`` and does
        the push work; Discord carries only the resulting
        ``manifest_json`` + transfer ticket + grant token. ``repo_path``
        must be an existing directory on the agent's gateway.
        """
        if not repo_path or not Path(repo_path).is_dir():
            raise ValueError(
                f"push_build_bundle: repo_path is not an existing directory: {repo_path}")
        resolved = self._resolve_pool(pool_id, source=source)
        prop = self._propagator(resolved)
        pusher = prop._members.get(self._caller_key_id)
        if pusher is None:
            raise PermissionError(
                f"push refused: caller {self._caller_key_id!r} is not a pool member")
        if version is None:
            version = L.next_repo_version(self._conn, repo_id)
        result = prop.build_push_bundle(
            repo_path=Path(repo_path), repo_id=repo_id, version=int(version),
            pusher=pusher, pusher_signing_key=self._caller_sk)
        result["manifest_json"] = json.dumps(result["manifest"], sort_keys=True)
        return result

    def add_repo_build_bundle(self, repo_path, repo_id, *, version=None,
                              pool_id=None, source=None) -> dict:
        """Build a contributor add-repo bundle on the contributor's own
        gateway: pack -> hash -> DEK wrap -> encrypt -> Iroh-over-Tor send
        -> contributor-signed submitted manifest, with no ledger write.

        This is the add-repo counterpart of :meth:`push_build_bundle` and
        reuses it verbatim (no duplicated pack/encrypt/send logic). The
        returned ``deliver_command`` is what the contributor's agent pastes
        to the maintainer bot as ``/add-repo --deliver ...``; bytes move
        only over Iroh-over-Tor, never over Discord.
        """
        result = self.push_build_bundle(repo_path, repo_id, version=version,
                                        pool_id=pool_id, source=source)
        manifest = result["manifest"]
        result["deliver_command"] = (
            f"/add-repo --deliver {manifest['transfer_ticket']} "
            f"--manifest {result['manifest_json']} --repo {repo_id}")
        return result

    def add_repo_deliver(self, transfer_ticket, manifest, *, pool_id=None,
                         source=None, repo_id=None) -> dict:
        """Deliver a contributor add-repo bundle to the maintainer gateway.

        Fetches the bytes over Iroh-over-Tor, verifies the contributor
        signature, membership (ACL) and BLAKE3 (inside the audit), runs the
        sandbox audit, and records a ``submitted`` repo_versions row for
        later owner approval. A flagged audit keeps the submitted row as
        evidence and records ``repo_submission_refused``; the version is
        never accepted here and no manifest_chain row is written.
        """
        resolved = self._resolve_pool(pool_id, source=source)
        self._check_not_frozen(resolved)
        self._require_authorized(resolved, source)
        # Maintainer-gateway gate (mirrors push_deliver): only the pool's
        # owner gateway may receive and audit a submission.
        _pool_row = self._conn.execute(
            "SELECT owner_gateway_key_id, maintainer_key_id FROM pools WHERE pool_id=?",
            (resolved,)).fetchone()
        _owner_gateway = (_pool_row[0] if _pool_row and _pool_row[0] else
                          _pool_row[1] if _pool_row else None)
        if _owner_gateway != self._caller_key_id:
            raise PermissionError(
                "add-repo deliver refused: only the pool maintainer gateway may accept")
        if not manifest:
            raise ValueError(
                "add-repo deliver needs the contributor-signed manifest (--manifest <json>)")
        if manifest.get("transfer_ticket") != transfer_ticket:
            raise PermissionError(
                "add-repo deliver refused: manifest transfer_ticket does not match the delivered ticket")
        if repo_id and manifest.get("repo_id") != repo_id:
            raise PermissionError(
                "add-repo deliver refused: manifest repo does not match --repo")
        prop = self._propagator(resolved)
        submitter = prop._members.get(manifest.get("submitter_key_id", ""))
        if submitter is None:
            raise PermissionError(
                "add-repo deliver refused: submitter is not a pool member")
        from propagation import _verify_key
        if not C.verify_manifest(_verify_key(submitter), manifest):
            raise RuntimeError("add-repo manifest signature does not verify")
        maintainer = prop._members.get(prop._maintainer_key_id)
        if maintainer is None:
            raise RuntimeError(
                f"maintainer {prop._maintainer_key_id!r} is not a pool member")
        aud = prop.audit(manifest=manifest, maintainer=maintainer)
        report = aud["report"]
        # Register the project row so the submitted version has a home even
        # when the repo was never added via the --path fast path.
        L.upsert_repo(self._conn, manifest["repo_id"], resolved,
                      name=manifest["repo_id"],
                      submitter_key_id=manifest.get("submitter_key_id") or None)
        from propagation import manifest_hash as _manifest_hash
        m_hash = _manifest_hash(manifest)
        L.record_submission(self._conn, manifest["repo_id"],
                            int(manifest["version"]), m_hash,
                            json.dumps(manifest, sort_keys=True))
        if report.get("verdict") != "clean":
            hits = report.get("hits", [])
            L.record_event(self._conn, resolved, "repo_submission_refused",
                           {"repo_id": manifest["repo_id"],
                            "version": manifest.get("version"),
                            "report_hash": aud["report_hash"], "hits": hits})
            notice = (f"add-repo submission for {manifest['repo_id']} "
                      f"v{manifest['version']} recorded as submitted: audit "
                      f"flagged {len(hits)} hit(s); awaiting approval")
            return {"ok": False, "repo_id": manifest["repo_id"],
                    "version": manifest["version"], "state": "submitted",
                    "manifest_hash": m_hash, "verdict": report.get("verdict"),
                    "report_hash": aud["report_hash"], "hits": hits,
                    "notice": notice}
        notice = (f"add-repo submission {manifest['repo_id']} "
                  f"v{manifest['version']} recorded as submitted "
                  f"({m_hash[:12]}); awaiting approval")
        self._queue_notice(resolved, notice, kind="repo_submitted")
        agent_prompt = self._notify_repo_submitted(
            resolved, manifest, aud)
        return {"ok": True, "repo_id": manifest["repo_id"],
                "version": manifest["version"], "state": "submitted",
                "manifest_hash": m_hash, "verdict": report.get("verdict"),
                "clamav": (report.get("clamav") or {}).get("verdict"),
                "report_hash": aud["report_hash"], "audit_id": aud["audit_id"],
                "notice": notice, "agent_prompt": agent_prompt}

    def _notify_repo_submitted(self, pool_id, manifest, aud) -> dict:
        """Ephemeral (gateway-only) payload for the Hermes agent when an
        add-repo submission lands as submitted.

        Same shape as the push prompt: notify the operator that the
        project is awaiting approval, quoting verdict + audit id; on
        approval invoke the agent-side accept path
        (``/add-repo approve <repo_id>`` or
        ``scripts/pool-approve.py --repo <id>`` → :meth:`add_repo_approve`);
        on denial record it (``scripts/pool-approve.py --repo <id> --deny``
        → :meth:`add_repo_deny`).
        """
        report = aud.get("report") or {}
        clamav = report.get("clamav") or {}
        prompt = (
            f"An add-repo submission for {manifest.get('repo_id')} "
            f"v{manifest.get('version')} was audited clean "
            f"(verdict {report.get('verdict')}, ClamAV "
            f"{clamav.get('verdict', 'unknown')} via "
            f"{clamav.get('via', 'unknown')}, audit {aud.get('audit_id')}) "
            f"and is awaiting approval. Notify the operator now, quoting "
            f"the verdict and audit id, and ask whether to approve. On "
            f"approval, invoke the agent-side accept path: "
            f"/add-repo approve {manifest.get('repo_id')} (accepts the "
            f"version); then merge into the canonical checkout yourself "
            f"with git. On denial, run scripts/pool-approve.py --repo "
            f"{manifest.get('repo_id')} --deny to record the denial.")
        return {
            "event": "repo_submitted",
            "ephemeral": True,
            "pool_id": pool_id,
            "repo_id": manifest.get("repo_id"),
            "version": manifest.get("version"),
            "audit_id": aud.get("audit_id"),
            "verdict": report.get("verdict"),
            "clamav_verdict": clamav.get("verdict"),
            "prompt": prompt,
        }

    def add_repo_approve(self, repo_id, *, version=None, pool_id=None,
                         source=None) -> dict:
        """Owner-only promotion of a submitted repo version to accepted.

        Reuses the propagator accept step: DEK rotation, a maintainer-signed
        accepted manifest and the chain append. The stale submitted row is
        dropped by the accept step before the accepted row is appended.
        """
        resolved = self._resolve_pool(pool_id, source=source)
        self._check_not_frozen(resolved)
        self._require_owner(resolved, source)
        self._assert_local_owned(resolved, source=source)
        sub = L.get_submission(self._conn, repo_id, version=version)
        if sub is None:
            raise ValueError(
                f"add-repo approve refused: no submitted version for {repo_id!r}"
                + (f" v{version}" if version else ""))
        manifest = json.loads(sub["manifest_json"])
        prop = self._propagator(resolved)
        maintainer = prop._members.get(prop._maintainer_key_id)
        if maintainer is None:
            raise RuntimeError(
                f"maintainer {prop._maintainer_key_id!r} is not a pool member")
        audit_row = self._conn.execute(
            "SELECT report_hash FROM audits WHERE repo_id=? AND version=?"
            " ORDER BY created_at DESC LIMIT 1",
            (repo_id, manifest["version"])).fetchone()
        if audit_row is None:
            raise RuntimeError(
                "add-repo approve refused: no audit on file for this version")
        accepted = prop.accept(
            manifest=manifest, report_hash=audit_row[0],
            maintainer=maintainer,
            maintainer_signing_key=self._caller_sk)
        notice = (f"add-repo {repo_id} v{manifest['version']} approved and "
                  f"accepted ({accepted['manifest_hash'][:12]})")
        self._queue_notice(resolved, notice, kind="repo_approved")
        return {"ok": True, "repo_id": repo_id, "pool_id": resolved,
                "version": manifest["version"], "state": "accepted",
                "manifest_hash": accepted["manifest_hash"],
                "transfer_id": accepted.get("transfer_id", ""),
                "dek_rotated": accepted.get("dek_rotated", False),
                "report_hash": audit_row[0], "notice": notice}

    def add_repo_submissions(self, *, repo_id=None, pool_id=None,
                             source=None) -> dict:
        """List submitted (awaiting approval) repo versions, newest first."""
        resolved = self._resolve_pool(pool_id, source=source)
        self._require_pool_authorized(resolved, source)
        rows = L.list_submissions(self._conn, repo_id=repo_id)
        return {"ok": True, "pool_id": resolved, "submissions": [
            {"repo_id": r["repo_id"], "version": r["version"],
             "state": r["state"], "manifest_hash": r["manifest_hash"]}
            for r in rows]}

    def awaiting_approval(self, *, pool_id=None, source=None) -> dict:
        """Discovery surface: every submitted version awaiting owner
        approval on the hosted pool (both ingestion paths), each with
        its audit verdict and, for pushes, the claimed ticket waiting on
        the decision. The gateway agent reads this (via ``pool_status``)
        to tell the operator what needs a decision; acceptance/denial
        follow through the agent-side paths (:meth:`push_approve` /
        :meth:`add_repo_approve` / the deny counterparts). Read-only and
        authorized (maintainer gateway or whitelisted user)."""
        resolved = self._resolve_pool(pool_id, source=source)
        self._require_pool_authorized(resolved, source)
        items = []
        for row in L.list_submissions(self._conn):
            manifest = json.loads(row["manifest_json"])
            audit = L.get_audit(self._conn, row["repo_id"], row["version"])
            ticket_id = None
            for t in self.list_tickets(pool_id=resolved, source=source):
                if (t["repo_id"] == row["repo_id"]
                        and t["state"] == "claimed"):
                    ticket_id = t["ticket_id"]
                    break
            items.append({
                "repo_id": row["repo_id"], "version": row["version"],
                "state": row["state"],
                "manifest_hash": row["manifest_hash"],
                "submitter_key_id": manifest.get("submitter_key_id"),
                "ticket_id": ticket_id,
                "audit_id": audit["audit_id"] if audit else None,
                "verdict": audit["verdict"] if audit else None,
            })
        return {"ok": True, "pool_id": resolved, "awaiting_approval": items}

    # -- remote member enrollment (Workflow C) ------------------------------

    def my_keys(self) -> dict:
        """This gateway's public key material: key id + box/verify public
        keys. Public keys are safe to carry over Discord; the gateway
        agent sends them to the pool maintainer, who enrolls them with
        :meth:`enroll_member`."""
        verify_hex = ""
        box_hex = ""
        try:
            if self._caller_sk is not None:
                verify_hex = self._caller_sk.verify_key.encode().hex()
            if self._caller_box_sk is not None:
                box_hex = self._caller_box_sk.public_key.encode().hex()
        except Exception:
            pass
        return {"ok": True, "key_id": self._caller_key_id,
                "verify_key_hex": verify_hex, "box_pubkey_hex": box_hex}

    def enroll_member(self, pool_id, key_id, box_pubkey_hex,
                      verify_pubkey_hex, *, source=None) -> dict:
        """Enroll a REMOTE gateway key as a real pool member (owner-only).

        Public key material only (safe over Discord). After enrollment,
        the latest accepted version of every repo is re-wrapped so the
        new member holds a DEK entry for existing accepted versions —
        the same rotation machinery as acceptance, keeping each
        manifest's ``prev_manifest_hash`` and audit reference.
        """
        resolved = self._resolve_pool(pool_id, source=source)
        self._check_not_frozen(resolved)
        self._require_owner(resolved, source)
        self._assert_local_owned(resolved, source=source)
        if not key_id:
            raise ValueError("enroll needs a key_id")
        for label, value in (("box_pubkey_hex", box_pubkey_hex),
                             ("verify_pubkey_hex", verify_pubkey_hex)):
            try:
                raw = bytes.fromhex(value or "")
            except ValueError:
                raise ValueError(f"enroll refused: {label} is not hex")
            if len(raw) != 32:
                raise ValueError(
                    f"enroll refused: {label} must be 32 bytes "
                    f"({len(raw)} given)")
        L.add_member(self._conn, resolved, key_id, role="member",
                     box_pubkey_hex=box_pubkey_hex,
                     verify_pubkey_hex=verify_pubkey_hex)
        rewrapped = []
        prop = self._propagator(resolved)
        maintainer = prop._members.get(prop._maintainer_key_id)
        if maintainer is None:
            raise RuntimeError(
                f"maintainer {prop._maintainer_key_id!r} is not a pool member")
        repos = [r[0] for r in self._conn.execute(
            "SELECT DISTINCT repo_id FROM repo_versions WHERE state='accepted'")]
        for repo_id in repos:
            manifest = L.get_latest_accepted_manifest(self._conn, repo_id)
            if manifest is None:
                continue
            prop.rewrap_accepted(
                manifest=manifest, maintainer=maintainer,
                maintainer_signing_key=self._caller_sk)
            rewrapped.append(repo_id)
        notice = (f"member {key_id} enrolled on {resolved} "
                  f"(rewrapped {len(rewrapped)} accepted version(s))")
        self._queue_notice(resolved, notice, kind="member_enrolled")
        return {"ok": True, "pool_id": resolved, "key_id": key_id,
                "rewrapped_repos": rewrapped, "notice": notice}

    def push_deliver(self, transfer_ticket, token, manifest, *,
                     pool_id=None, source=None) -> dict:
        """Deliver a pushed bundle to the pool gateway (the ``/push repo
        --deliver <ticket> --token <grant>`` verb).

        The grant is verified (consumed only at approval, never here);
        the ticket must still be claimed by the grant's key
        (released/completed ⇒ refused). The contributor signature,
        membership and claimant are checked, the bytes are fetched over
        the configured transport, decrypted with the maintainer's
        wrapped DEK, BLAKE3-verified and audited (ClamAV + keyword).
        A clean audit records the version as ``submitted`` — awaiting
        owner approval — and the ticket stays claimed; acceptance and
        completion happen only through the agent-driven approval path
        (:meth:`push_approve`), never automatically. A flagged audit
        refuses the push, the task stays claimed, and a ``push_flagged``
        event records the scan hits. The grant is NOT consumed by a
        deliver, so fixing the diff can redeliver.
        """
        resolved = self._resolve_pool(pool_id, source=source)
        self._check_not_frozen(resolved)
        self._require_authorized(resolved, source)
        # Phase 2: only the pool maintainer gateway may accept a push.
        # The caller must be the pool's owner gateway (== maintainer by the
        # registration gate). This is the maintainer gate for accept().
        _pool_row = self._conn.execute(
            "SELECT owner_gateway_key_id, maintainer_key_id FROM pools WHERE pool_id=?",
            (resolved,)).fetchone()
        _owner_gateway = (_pool_row[0] if _pool_row and _pool_row[0] else
                          _pool_row[1] if _pool_row else None)
        if _owner_gateway != self._caller_key_id:
            raise PermissionError(
                "push deliver refused: only the pool maintainer gateway may accept")
        grant = L.get_push_grant(self._conn, token)
        if grant is None or grant.get("consumed_at") is not None:
            raise PermissionError("push deliver refused: unknown or replayed grant token")
        if grant["pool_id"] != resolved:
            raise PermissionError(
                "push deliver refused: grant is for a different pool")
        ticket = self.show_ticket(grant["ticket_id"])
        if ticket["state"] != "claimed" or ticket.get("claimant_key_id") != grant["key_id"]:
            raise PermissionError(
                f"push deliver refused: ticket {ticket['ticket_id']!r} is "
                f"{ticket['state']!r} (grant expired — released or completed)")
        # Phase 3: the claimant's task-count budget is checked before accept.
        # The push that completes the last allowed task still succeeds: this
        # check runs before this push's completion is recorded.
        agent_budget = L.ensure_agent_budget(
            self._conn, resolved, grant["key_id"],
            self._allocation_for(grant["key_id"]))
        if agent_budget["tasks_completed"] >= agent_budget["task_allocation"]:
            raise L.ClaimRefused(
                f"agent budget exhausted: {agent_budget['tasks_completed']} of"
                f" {agent_budget['task_allocation']} tasks completed on pool"
                f" {resolved} — push refused")
        if not manifest:
            raise ValueError("push deliver needs the contributor-signed manifest (--manifest <json>)")
        if manifest.get("transfer_ticket") != transfer_ticket:
            raise PermissionError(
                "push deliver refused: manifest transfer_ticket does not match the delivered ticket")
        if manifest.get("repo_id") != ticket["repo_id"]:
            raise PermissionError(
                "push deliver refused: manifest repo does not match the claimed ticket")
        if manifest.get("submitter_key_id") != grant["key_id"]:
            raise PermissionError(
                "push deliver refused: manifest submitter is not the grant claimant")
        prop = self._propagator(resolved)
        submitter = prop._members.get(manifest.get("submitter_key_id", ""))
        if submitter is None:
            raise PermissionError(
                "push deliver refused: submitter is not a pool member")
        from propagation import _verify_key
        if not C.verify_manifest(_verify_key(submitter), manifest):
            raise RuntimeError("push manifest signature does not verify")
        maintainer = prop._members.get(prop._maintainer_key_id)
        if maintainer is None:
            raise RuntimeError(
                f"maintainer {prop._maintainer_key_id!r} is not a pool member")
        aud = prop.audit(manifest=manifest, maintainer=maintainer)
        report = aud["report"]
        if report.get("verdict") != "clean":
            hits = report.get("hits", [])
            L.record_event(self._conn, resolved, "push_flagged",
                           {"ticket_id": ticket["ticket_id"],
                            "repo_id": manifest["repo_id"],
                            "version": manifest.get("version"),
                            "report_hash": aud["report_hash"], "hits": hits})
            notice = (f"push refused for ticket {ticket['ticket_id']}: "
                      f"audit flagged {len(hits)} hit(s); task stays claimed")
            self._queue_notice(resolved, notice, kind="push_flagged")
            return {"ok": False, "error": notice, "ticket_id": ticket["ticket_id"],
                    "state": ticket["state"], "verdict": report.get("verdict"),
                    "report_hash": aud["report_hash"], "hits": hits,
                    "notice": notice}
        from propagation import manifest_hash as _manifest_hash
        m_hash = _manifest_hash(manifest)
        L.record_submission(self._conn, manifest["repo_id"],
                            int(manifest["version"]), m_hash,
                            json.dumps(manifest, sort_keys=True))
        notice = (f"push for ticket {ticket['ticket_id']} recorded as "
                  f"submitted ({manifest['repo_id']} "
                  f"v{manifest['version']}, {m_hash[:12]}); awaiting "
                  f"approval — acceptance and completion follow the "
                  f"agent-driven approval path, never automatically")
        self._queue_notice(resolved, notice, kind="push_submitted")
        agent_prompt = self._notify_push_submitted(
            resolved, ticket["ticket_id"], manifest, aud)
        return {"ok": True, "pool_id": resolved,
                "ticket_id": ticket["ticket_id"],
                "state": "submitted", "awaiting_approval": True,
                "repo_id": manifest["repo_id"],
                "version": manifest["version"],
                "manifest_hash": m_hash,
                "report_hash": aud["report_hash"],
                "audit_id": aud["audit_id"], "verdict": report.get("verdict"),
                "clamav": (report.get("clamav") or {}).get("verdict"),
                "notice": notice, "agent_prompt": agent_prompt}

    def _notify_push_submitted(self, pool_id, ticket_id, manifest,
                               aud) -> dict:
        """Ephemeral (gateway-only) payload for the Hermes agent when a
        push lands as submitted.

        The plugin is passive: it never posts to Discord. This payload is
        returned in the push_deliver result so the agent that invoked the
        command receives it and is prompted to notify the operator that
        the change is awaiting approval, ask for the decision, and act
        on it: on approval invoke the agent-side accept path
        (``scripts/pool-approve.py --ticket <id>`` → :meth:`push_approve`,
        which accepts and completes the ticket); on denial run
        ``scripts/pool-approve.py --ticket <id> --deny`` to record the
        denial (:meth:`push_deny`). No approval slash command exists by
        design — approval is agent-driven and service-level.
        """
        report = aud.get("report") or {}
        clamav = report.get("clamav") or {}
        prompt = (
            f"A push for ticket {ticket_id} ({manifest.get('repo_id')} "
            f"v{manifest.get('version')}) was audited clean "
            f"(verdict {report.get('verdict')}, ClamAV "
            f"{clamav.get('verdict', 'unknown')} via "
            f"{clamav.get('via', 'unknown')}, audit {aud.get('audit_id')}) "
            f"and is awaiting approval. Notify the operator now, quoting "
            f"the verdict and audit id, and ask whether to approve. On "
            f"approval, invoke the agent-side accept path: "
            f"scripts/pool-approve.py --ticket {ticket_id} (accepts the "
            f"version and completes the ticket); then merge into the "
            f"canonical checkout yourself with git. On denial, run "
            f"scripts/pool-approve.py --ticket {ticket_id} --deny to "
            f"record the denial; the ticket stays claimed.")
        return {
            "event": "push_submitted",
            "ephemeral": True,
            "pool_id": pool_id,
            "ticket_id": ticket_id,
            "repo_id": manifest.get("repo_id"),
            "version": manifest.get("version"),
            "audit_id": aud.get("audit_id"),
            "verdict": report.get("verdict"),
            "clamav_verdict": clamav.get("verdict"),
            "prompt": prompt,
        }

    def push_approve(self, ticket_id, *, pool_id=None, source=None) -> dict:
        """Agent-driven approval of a submitted push (owner-only).

        Accepts the submitted version (DEK rotation + chain append via
        the propagator), consumes the ticket's push grants, and completes
        the ticket. Invoked by the gateway agent after the operator
        approves (see the ``agent_prompt`` on push_deliver); there is no
        approval slash command by design.
        """
        ticket = self.show_ticket(ticket_id)
        resolved = self._resolve_pool(pool_id or ticket["pool_id"], source=source)
        self._check_not_frozen(resolved)
        self._require_owner(resolved, source)
        self._assert_local_owned(resolved, source=source)
        if ticket["state"] != "claimed":
            raise PermissionError(
                f"push approve refused: ticket {ticket_id!r} is "
                f"{ticket['state']!r} (not claimed)")
        sub = L.get_submission(self._conn, ticket["repo_id"])
        if sub is None:
            raise ValueError(
                f"push approve refused: no submitted version for "
                f"{ticket['repo_id']!r}")
        manifest = json.loads(sub["manifest_json"])
        audit_row = self._conn.execute(
            "SELECT report_hash, verdict FROM audits WHERE repo_id=? AND version=?"
            " ORDER BY created_at DESC LIMIT 1",
            (ticket["repo_id"], manifest["version"])).fetchone()
        if audit_row is None:
            raise RuntimeError(
                "push approve refused: no audit on file for this version")
        if audit_row[1] != "clean":
            raise RuntimeError(
                f"push approve refused: audit verdict is {audit_row[1]!r}, "
                "not clean")
        prop = self._propagator(resolved)
        maintainer = prop._members.get(prop._maintainer_key_id)
        if maintainer is None:
            raise RuntimeError(
                f"maintainer {prop._maintainer_key_id!r} is not a pool member")
        accepted = prop.accept(
            manifest=manifest, report_hash=audit_row[0],
            maintainer=maintainer,
            maintainer_signing_key=self._caller_sk)
        for grant in L.list_push_grants_for_ticket(self._conn, ticket_id):
            if grant.get("consumed_at") is None:
                L.consume_push_grant(self._conn, grant["token"])
        updated = L.complete_ticket(self._conn, ticket_id,
                                    ticket.get("claimant_key_id"))
        notice = (f"{STATE_MARKER.format(state='completed')} ticket "
                  f"{updated['ticket_id']} completed by approved push of "
                  f"{manifest['repo_id']} v{accepted['manifest']['version']} "
                  f"({accepted['manifest_hash'][:12]})")
        self._queue_notice(resolved, notice, kind="ticket_completed")
        agent_prompt = self._notify_push_approved(
            resolved, updated["ticket_id"], accepted["manifest"])
        return {"ok": True, "pool_id": resolved,
                "ticket_id": updated["ticket_id"], "state": updated["state"],
                "repo_id": manifest["repo_id"],
                "version": accepted["manifest"]["version"],
                "manifest_hash": accepted["manifest_hash"],
                "transfer_id": accepted.get("transfer_id", ""),
                "dek_rotated": accepted.get("dek_rotated", False),
                "report_hash": audit_row[0], "notice": notice,
                "agent_prompt": agent_prompt}

    def push_deny(self, ticket_id, *, pool_id=None, source=None) -> dict:
        """Record an owner denial of a submitted push (owner-only).

        Drops the submitted version (it leaves the approval queue) and
        records a ``push_denied`` event; the ticket stays claimed so the
        contributor can re-push a fixed diff.
        """
        ticket = self.show_ticket(ticket_id)
        resolved = self._resolve_pool(pool_id or ticket["pool_id"], source=source)
        self._check_not_frozen(resolved)
        self._require_owner(resolved, source)
        self._assert_local_owned(resolved, source=source)
        sub = L.get_submission(self._conn, ticket["repo_id"])
        if sub is None:
            raise ValueError(
                f"push deny refused: no submitted version for "
                f"{ticket['repo_id']!r}")
        manifest = json.loads(sub["manifest_json"])
        self._conn.execute(
            "DELETE FROM repo_versions WHERE repo_id=? AND version=? AND state='submitted'",
            (ticket["repo_id"], manifest["version"]))
        self._conn.commit()
        L.record_event(self._conn, resolved, "push_denied",
                       {"ticket_id": ticket_id,
                        "repo_id": ticket["repo_id"],
                        "version": manifest["version"]})
        notice = (f"push for ticket {ticket_id} denied by owner; ticket "
                  f"stays claimed")
        self._queue_notice(resolved, notice, kind="push_denied")
        return {"ok": True, "pool_id": resolved, "ticket_id": ticket_id,
                "state": "denied", "repo_id": ticket["repo_id"],
                "version": manifest["version"], "notice": notice}

    def add_repo_deny(self, repo_id, *, version=None, pool_id=None,
                      source=None) -> dict:
        """Record an owner denial of a submitted repo version (owner-only).

        Drops the submitted version and records a
        ``repo_submission_denied`` event.
        """
        resolved = self._resolve_pool(pool_id, source=source)
        self._check_not_frozen(resolved)
        self._require_owner(resolved, source)
        self._assert_local_owned(resolved, source=source)
        sub = L.get_submission(self._conn, repo_id, version=version)
        if sub is None:
            raise ValueError(
                f"add-repo deny refused: no submitted version for {repo_id!r}"
                + (f" v{version}" if version else ""))
        manifest = json.loads(sub["manifest_json"])
        self._conn.execute(
            "DELETE FROM repo_versions WHERE repo_id=? AND version=? AND state='submitted'",
            (repo_id, manifest["version"]))
        self._conn.commit()
        L.record_event(self._conn, resolved, "repo_submission_denied",
                       {"repo_id": repo_id, "version": manifest["version"]})
        notice = (f"add-repo submission {repo_id} "
                  f"v{manifest['version']} denied by owner")
        self._queue_notice(resolved, notice, kind="repo_submission_denied")
        return {"ok": True, "pool_id": resolved, "repo_id": repo_id,
                "version": manifest["version"], "state": "denied",
                "notice": notice}

    def _notify_push_approved(self, pool_id, ticket_id, accepted_manifest) -> dict:
        """Ephemeral (gateway-only) payload for the Hermes agent once an
        approved push is accepted and the ticket completes.

        The plugin is passive: it never posts to Discord. The agent that
        ran the approval is prompted to merge into the canonical
        checkout itself with git (the plugin never merges). The
        transfer_ticket is the Iroh-over-Tor download link for the new
        accepted version; ``/fetch repo <id>`` on the pool gateway
        returns the full accepted manifest when the trimmed reply does
        not carry it.
        """
        version = accepted_manifest.get("version")
        transfer_ticket = accepted_manifest.get("transfer_ticket", "")
        prompt = (
            f"Tasks for ticket {ticket_id} are complete. New version {version} "
            f"is available over Iroh (transfer_ticket above). The operator "
            f"has approved; merge it into the canonical checkout now with "
            f"git (agent executes): git fetch <local bundle path> && "
            f"git merge --ff-only <branch>.")
        return {
            "event": "push_approved",
            "ephemeral": True,
            "pool_id": pool_id,
            "ticket_id": ticket_id,
            "transfer_ticket": transfer_ticket,
            "version": version,
            "prompt": prompt,
        }

    def merge_approved(self, ticket_id, *, approved_by_user_id,
                       pool_id=None, source=None) -> dict:
        """Record a maintainer merge decision (gateway-owner only).

        The plugin never merges and never auto-merges: the agent runs the
        actual local git merge after explicit user approval; this records
        the approval and emits merge_completed.
        """
        ticket = self.show_ticket(ticket_id)
        resolved = self._resolve_pool(pool_id or ticket["pool_id"], source=source)
        self._check_not_frozen(resolved)
        if not approved_by_user_id or not L.is_owner(
                self._conn, resolved, str(approved_by_user_id)):
            raise PermissionError(
                f"not authorized for pool {resolved} — ask the maintainer")
        row = self._conn.execute(
            "SELECT owner_gateway_key_id, maintainer_key_id FROM pools WHERE pool_id=?",
            (resolved,)).fetchone()
        owner_gateway = (row[0] if row and row[0] else
                         row[1] if row else None)
        if owner_gateway != self._caller_key_id:
            raise PermissionError(
                "gateway identity changed: caller is not the pool's owner gateway — "
                "re-register the pool to continue")
        version = None
        try:
            _vrow = self._conn.execute(
                "SELECT MAX(version) FROM repo_versions WHERE repo_id=? AND state='accepted'",
                (ticket["repo_id"],)).fetchone()
            version = _vrow[0] if _vrow else None
        except Exception:
            version = None
        L.record_event(self._conn, resolved, "merge_approved",
                       {"ticket_id": ticket_id, "version": version,
                        "approved_by_user_id": str(approved_by_user_id)})
        L.record_event(self._conn, resolved, "merge_completed",
                       {"ticket_id": ticket_id, "version": version,
                        "approved_by_user_id": str(approved_by_user_id)})
        return {"ok": True, "merged": True, "ticket_id": ticket_id,
                "version": version}

    # -- budget usage reporting (secondary, agent-reported) --------------------
    # Turns/tokens/wall-clock are recorded only when the agent reports them.
    # check_budget/enforce_budget act on those rows; when nothing was
    # reported only the task-count limit applies. No auto-release ever:
    # exhaustion raises and the claimant must /release or push.

    def report_usage(self, claim_id, turns=0, tokens=0, seconds=0) -> dict:
        """Record agent-reported usage on a claim (increments, never resets)."""
        row = self._conn.execute(
            "SELECT claim_id FROM claims WHERE claim_id=?",
            (str(claim_id),)).fetchone()
        if row is None:
            raise ValueError(f"unknown claim: {claim_id!r}")
        self._conn.execute(
            "UPDATE claims SET turns_used=turns_used+?, tokens_used=tokens_used+?,"
            " wall_seconds_used=wall_seconds_used+? WHERE claim_id=?",
            (int(turns or 0), int(tokens or 0), float(seconds or 0), str(claim_id)))
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM claims WHERE claim_id=?", (str(claim_id),)).fetchone()
        return dict(row)

    def check_budget(self, claim_id) -> dict:
        """Secondary fields only: {ok, exceeded} (+ used/limit when exceeded)."""
        row = self._conn.execute(
            "SELECT * FROM claims WHERE claim_id=?", (str(claim_id),)).fetchone()
        if row is None:
            raise ValueError(f"unknown claim: {claim_id!r}")
        claim = dict(row)
        budget = json.loads(claim.get("budget_json") or "{}")
        for field, used_key, limit_key in (
                ("turns", "turns_used", "max_turns"),
                ("tokens", "tokens_used", "token_cap"),
                ("seconds", "wall_seconds_used", "wall_clock_seconds")):
            used = claim.get(used_key, 0) or 0
            limit = budget.get(limit_key)
            if limit is not None and used > limit:
                return {"ok": False, "exceeded": field, "used": used,
                        "limit": limit}
        return {"ok": True, "exceeded": None}

    def enforce_budget(self, claim_id) -> dict:
        """Raise ClaimRefused when a secondary field is exceeded."""
        result = self.check_budget(claim_id)
        if not result["ok"]:
            raise L.ClaimRefused(
                f"budget exhausted: {result['exceeded']}"
                f" ({_fmt_num(result['used'])}/{_fmt_num(result['limit'])})"
                " — release or push now")
        return result

    # -- pool stewardship ------------------------------------------

    def _members_for_propagator(self, pool_id) -> dict:
        """Member map from the members table + identities; private box key only
        for the local caller (never persisted here)."""
        members = {}
        for row in self._conn.execute(
                "SELECT m.key_id, m.role, i.verify_key_hex, m.box_pubkey_hex,"
                " i.box_pubkey_hex, m.verify_pubkey_hex"
                " FROM members m LEFT JOIN identities i ON i.key_id = m.key_id"
                " WHERE m.pool_id=?", (pool_id,)):
            box_sk = self._caller_box_sk if row[0] == self._caller_key_id else None
            members[row[0]] = Member(key_id=row[0], role=row[1] or "member",
                                     verify_key_hex=row[5] or row[2] or "",
                                     box_pubkey_hex=row[3] or row[4] or "", box_sk=box_sk)
        if self._caller_key_id and self._caller_key_id not in members:
            # The live caller identity IS a valid wrap target: it has
            # this gateway's box key in memory. The old fallback used
            # an EMPTY box_pubkey_hex, so submit/build_push_bundle/
            # accept skipped wrapping for the caller and fetch
            # refused them despite being whitelisted+bound.
            members[self._caller_key_id] = Member(
                key_id=self._caller_key_id, role="member",
                verify_key_hex=self._caller_verify_key_hex(),
                box_pubkey_hex=self._caller_box_pubkey_hex(),
                box_sk=self._caller_box_sk)
        return members

    def _propagator(self, ticket_or_pool) -> RepoPropagator:
        """Build the propagator for a pool. Accepts a ticket dict (legacy)
        or a pool_id string (used by /add-repo, which has no ticket)."""
        if self._transport is None:
            raise RuntimeError("push / submit need a transport backend")
        pool_id = (ticket_or_pool["pool_id"] if isinstance(ticket_or_pool, dict)
                   else str(ticket_or_pool))
        pool = self._conn.execute("SELECT * FROM pools WHERE pool_id=?",
                                  (pool_id,)).fetchone()
        if pool is None:
            raise RuntimeError(f"unknown pool: {pool_id!r}")
        maintainer_id = pool["maintainer_key_id"]
        members = self._members_for_propagator(pool_id)
        if maintainer_id not in members:
            raise RuntimeError(f"maintainer {maintainer_id!r} is not a pool member")
        maintainer_sk = self._caller_sk
        # RepoPropagator only uses maintainer_signing_key in accept().
        # push_deliver gates accept() to the pool maintainer gateway (Phase
        # 2), so by the time accept() runs the caller IS the maintainer;
        # submit/audit legitimately run as the (possibly non-maintainer)
        # caller.
        return RepoPropagator(
            self._conn, self._transport,
            pool_id=pool_id,
            maintainer_key_id=maintainer_id, maintainer_signing_key=maintainer_sk,
            members=members,
            clamd_socket=self._clamd_socket,
            auto_provision=self._auto_provision)

    # -- audit -------------------------------------------------------

    def audit_show(self, ticket_id=None, repo_id=None, version=None) -> dict:
        if ticket_id:
            ticket = self.show_ticket(ticket_id)
            repo_id, version = ticket["repo_id"], None
        if not repo_id:
            raise ValueError("audit show needs a ticket or repo")
        audit = L.get_audit(self._conn, repo_id, version)
        if audit is None:
            raise ValueError(f"no audit recorded for repo {repo_id!r}")
        report = json.loads(audit["report_json"] or "{}")
        return {"ok": True, "audit_id": audit["audit_id"], "repo_id": audit["repo_id"],
                "version": audit["version"], "verdict": audit["verdict"],
                "report_hash": audit["report_hash"], "report": report}
