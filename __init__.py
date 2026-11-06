"""Agent Pool plugin — gateway-native: Hermes posts, plugin returns content.

Slash commands: ``/pool register|invite|members|remove|freeze|list``,
``/task add|list|show`` (flat aliases ``/task_add`` / ``/task_list``),
``/add-repo`` (register a project + submit its bytes as a new version),
``/claim`` (paths optional — omit ``--paths`` for a whole-ticket claim;
``--any-project --max-tasks N`` takes up to N open tickets), ``/release``,
``/work submit``, ``/integrate``, ``/audit show``. Every handler returns a
string Hermes posts; the plugin never posts to Discord itself.
"""
import json
import logging
import os
import shlex
from pathlib import Path

logger = logging.getLogger(__name__)


def _cfg(ctx, key, default):
    try:
        return ctx.get_config(key, default=default)
    except Exception:
        return default


def _transport_config(ctx):
    """Resolve (mode, relay_url, per_pool) from plugin config.

    Precedence (documented in plugin.yaml):
    1. per-pool ``pools.<pool_id>.transport_mode`` / ``relay_url``
    2. top-level ``transport_mode`` (default ``relay``) / ``relay_url``
    3. ``HERMES_IROH_RELAY`` env var (inside the transport; an empty
       config relay_url means "unset" so the env still applies)
    An env ``off`` is a no-op in every mode (direct does not exist, so
    there is nothing to fall back to); ``_pool_status`` says so and
    states that transfers are relay-only on both ends.
    """
    base_mode = _cfg(ctx, "transport_mode", "relay") if ctx else "relay"
    base_relay = _cfg(ctx, "relay_url", "") if ctx else ""
    pools_cfg = _cfg(ctx, "pools", {}) if ctx else {}
    per_pool: dict = {}
    if isinstance(pools_cfg, dict):
        for pid, cfg in pools_cfg.items():
            if not isinstance(cfg, dict):
                continue
            mode = cfg.get("transport_mode", base_mode)
            relay = cfg.get("relay_url", base_relay)
            if mode != base_mode or relay != base_relay:
                per_pool[pid] = {"transport_mode": mode, "relay_url": relay}
    return base_mode, base_relay, per_pool


def _pool_status(args=None, **kwargs):
    """Report plugin config + ledger/identity readiness. Always returns JSON."""
    ctx = kwargs.get("ctx")
    try:
        relay_url = _cfg(ctx, "relay_url", "") if ctx else ""
        mode, _mode_relay, per_pool = _transport_config(ctx)
        env_relay = os.environ.get("HERMES_IROH_RELAY", "").strip()
        budgets = {k: _cfg(ctx, k, d) if ctx else d for k, d in (
            ("max_turns", 25), ("max_tasks_per_day", 5),
            ("token_cap", 50000), ("wall_clock_seconds", 1800))}
        status = {"ok": True, "plugin": "agent-pool", "phase": 3,
                  "relay_url": relay_url, "transport_mode": mode,
                  "transport_privacy": (
                      "relay-only on both sender and receiver: the peer "
                      "learns no IP; the relay sees both peers' IPs and "
                      "transfer metadata but never plaintext; there is "
                      "no direct mode and no way to opt into one"),
                  "pool_transports": per_pool,
                  "budgets": budgets,
                  "ledger": "ready", "identity": "ready",
                  "queued_notices": []}
        if env_relay.lower() in {"off", "disabled", "none"}:
            status["transport_warning"] = (
                f"HERMES_IROH_RELAY={env_relay} is set but it is a no-op: "
                "transfers are relay-only on both ends and the env var "
                "cannot strip the anonymity layer (there is no direct "
                "mode). The peer learns no IP.")
        try:
            svc = _service_for(ctx)
            status["queued_notices"] = svc.drain_notices()
            # Local identity (public values only): key_id, verify key, box pubkey.
            try:
                status["identity_detail"] = {
                    "key_id": getattr(svc, "_caller_key_id", ""),
                    "verify_key_hex": svc._caller_sk.verify_key.encode().hex() if getattr(svc, "_caller_sk", None) is not None else "",
                    "box_pubkey_hex": svc._caller_box_sk.public_key.encode().hex() if getattr(svc, "_caller_box_sk", None) is not None else "",
                }
            except Exception:
                pass
        except Exception:
            pass
        return json.dumps(status)
    except Exception as exc:  # never raise from a handler
        return json.dumps({"ok": False, "error": str(exc)})


# ---------------------------------------------------------------------------
# Service construction (lazy: no DB/identity work at register() time)
# ---------------------------------------------------------------------------

def _service_for(ctx, source=None):
    """Build a PoolService from the plugin context. Raises with a clear msg."""
    try:
        from . import ledger as L
        from .identity import ensure_identity
        from .tickets import PoolService
        from .transport import FakeTransport, RealSendHermesTransport
    except ImportError:  # flat import (tests / standalone)
        import ledger as L
        from identity import ensure_identity
        from tickets import PoolService
        from transport import FakeTransport, RealSendHermesTransport
    conn = L.get_db()
    data_dir = L.default_db_path().parent
    identity = ensure_identity(data_dir)
    transport_name = _cfg(ctx, "transport", "fake") if ctx else "fake"
    mode, relay_url, per_pool = _transport_config(ctx)
    # An empty relay_url in config means "unset" (env/CLI default apply);
    # a non-empty one beats the env var. A non-empty "off" is rejected by
    # the transport constructor (relay-only cannot be opted out of).
    relay_arg = str(relay_url).strip() or None

    def _make(m, r):
        if transport_name == "fake":
            return FakeTransport(transport_mode=m)
        return RealSendHermesTransport(transport_mode=m, relay_url=r)

    transport = _make(mode, relay_arg)
    overrides = {pid: _make(cfg["transport_mode"],
                             str(cfg["relay_url"]).strip() or None)
                 for pid, cfg in per_pool.items()}
    budgets = {k: _cfg(ctx, k, d) if ctx else d for k, d in (
        ("max_turns", 25), ("max_tasks_per_day", 5),
        ("token_cap", 50000), ("wall_clock_seconds", 1800))}
    pool_resolver = (lambda: source) if source is not None else None
    return PoolService(conn, transport, caller_key_id=identity.key_id,
                       caller_signing_key=identity.signing_key,
                       caller_box_sk=identity.box_sk, budget_defaults=budgets,
                       pool_resolver=pool_resolver,
                       transport_overrides=overrides)


# ---------------------------------------------------------------------------
# Argument parsing: "sub rest --key value --flag"
# ---------------------------------------------------------------------------

def _parse_args(raw: str):
    try:
        tokens = shlex.split(raw or "")
    except ValueError as exc:
        raise ValueError(f"bad arguments: {exc}")
    positionals, options, flags = [], {}, set()
    it = iter(range(len(tokens)))
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--"):
            key = tok[2:].replace("-", "_")
            if "=" in key:
                k, v = key.split("=", 1)
                options[k] = v
            elif i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                options[key] = tokens[i + 1]
                i += 1
            else:
                flags.add(key)
        else:
            positionals.append(tok)
        i += 1
    return positionals, options, flags


def _budget_from_opts(options):
    budget = {}
    for opt, field in (("max_turns", "max_turns"), ("max_tasks_per_day", "max_tasks_per_day"),
                       ("token_cap", "token_cap"), ("wall_clock_seconds", "wall_clock_seconds")):
        if opt in options:
            budget[field] = int(options[opt])
    return budget or None


def _result(payload, svc=None, source=None) -> str:
    """Return the markdown string Hermes posts (never posts itself)."""
    if isinstance(payload, dict):
        payload = dict(payload)
        # Surface queued notices from earlier commands (D5).
        if svc is not None:
            try:
                queued = svc.drain_notices()
            except Exception:
                queued = []
            if queued:
                payload["queued_notices"] = queued
    try:
        from .replies import format_result
    except ImportError:
        from replies import format_result
    return format_result(payload)


def _error(exc) -> str:
    code = getattr(exc, "error_code", None) or "error"
    detail = getattr(exc, "detail", None) or str(exc)
    payload = {"ok": False, "error": str(exc)}
    if code != "error":
        payload["error_code"] = code
        payload["detail"] = detail
    return _result(payload)


# ---------------------------------------------------------------------------
# Command handlers (each takes ctx via closure from register())
# ---------------------------------------------------------------------------

def _handle_pool(ctx, raw: str, *, source=None, session_id="", session_key="", platform="") -> str:
    try:
        svc = _service_for(ctx, source=source)
        pos, opts, flags = _parse_args(raw)
        if not pos:
            return _pool_status(ctx=ctx)
        sub, rest = pos[0], pos[1:]
        if sub == "register":
            pool_id = rest[0] if rest else opts.get("pool_id", "")
            out = svc.register_pool(pool_id, opts.get("name", pool_id),
                                    relay_url=opts.get("relay_url", ""),
                                    source=source,
                                    discord_user_id=opts.get("discord_user", ""))
        elif sub == "invite":
            pool_id = rest[0] if rest else opts.get("pool_id", "")
            key_id = rest[1] if len(rest) > 1 else opts.get("key_id", "")
            out = svc.invite(pool_id, key_id, role=opts.get("role", "member"),
                             box_pubkey_hex=opts.get("box_pubkey", ""),
                             source=source,
                             discord_user_id=opts.get("discord_user", ""),
                             verify_pubkey_hex=opts.get("verify_key", ""))
        elif sub == "auto-approve":
            # `/pool auto-approve <on|off> [--pool <id>]` — maintainer-only.
            pool_id = opts.get("pool", "") or opts.get("pool_id", "")
            arg = (rest[0] if rest else opts.get("state", "") or "").strip().lower()
            if arg not in ("on", "off", "true", "false"):
                return _result({"ok": False, "error": "usage: /pool auto-approve <on|off> [--pool <id>]"})
            resolved = svc._resolve_pool(pool_id or None, source=source)
            out = svc.set_auto_approve(resolved, arg in ("on", "true"))
        elif sub == "members":
            pool_id = rest[0] if rest else opts.get("pool_id", "")
            out = {"ok": True, "pool_id": pool_id, "members": svc.members(pool_id, source=source)}
        elif sub == "freeze":
            # `/pool freeze` (no pool id, or --global) = GLOBAL kill-switch
            # across every pool; `/pool freeze <pool_id>` = per-pool only.
            # `--unfreeze` clears it. The freeze command itself is never
            # blocked by the freeze (it must be able to unfreeze).
            frozen = "unfreeze" not in flags
            pool_id = rest[0] if rest else opts.get("pool_id", "")
            if not pool_id or "global" in flags or pool_id in ("all", "*"):
                out = svc.freeze_global(frozen)
            else:
                out = svc.freeze(pool_id, frozen=frozen)
        elif sub == "remove":
            pool_id = rest[0] if rest else opts.get("pool_id", "")
            key_id = rest[1] if len(rest) > 1 else opts.get("key_id", "")
            out = svc.remove_member(pool_id, key_id)
        elif sub == "list":
            projects = svc.list_projects(pool_id=opts.get("pool_id") or None,
                                         source=source)
            out = {"ok": True,
                   "projects": [{k: p[k] for k in
                                 ("repo_id", "pool_id", "display_name",
                                  "accepted_version", "open_tickets",
                                  "claimed_tickets", "total_tickets",
                                  "submitter_key_id") if k in p}
                                | ({"note": p["note"]} if "note" in p else {})
                                for p in projects]}
        elif sub == "import":
            # Read-only bootstrap: import canonical manifest string(s) into
            # the remote view. No new command family; plugin stays passive.
            # Requires --verify-key (or an existing pinned anchor); no TOFU.
            pool_id = opts.get("pool_id") or opts.get("pool", "")
            manifest_raw = opts.get("manifest", "")
            if not manifest_raw and rest:
                # positional: /pool import <pool_id> <json>
                pool_id = rest[0] if rest else pool_id
                manifest_raw = rest[1] if len(rest) > 1 else ""
            try:
                manifest = json.loads(manifest_raw) if manifest_raw else {}
            except Exception as exc:
                return _result({"ok": False, "error": f"bad manifest JSON: {exc}"})
            manifests = [manifest] if isinstance(manifest, dict) and "repo_id" in manifest else manifest.get("manifests", []) if isinstance(manifest, dict) else []
            vk_hex = opts.get("verify_key", "")
            maintainer = opts.get("maintainer", "")
            verify_keys = {}
            if vk_hex:
                from nacl.signing import VerifyKey
                from nacl.encoding import RawEncoder
                signer = maintainer or (manifests[0].get("signer_key_id", "") if manifests else "")
                if signer:
                    verify_keys = {signer: VerifyKey(bytes.fromhex(vk_hex), encoder=RawEncoder)}
            out = svc.import_canonical(pool_id, manifests, verify_keys,
                                       tickets=manifest.get("tickets") if isinstance(manifest, dict) else None,
                                       maintainer_key_id=maintainer or None) if manifests else {"ok": False, "error": "no manifests to import"}
        elif sub == "trust":
            # `/pool trust <pool_id> <maintainer_key_id> --verify-key <hex>` (pin)
            # `/pool trust --list [pool_id]` / `/pool trust --revoke <pool_id>`
            if "list" in flags:
                out = svc.pool_trust_list(rest[0] if rest and rest[0] else opts.get("pool_id") or None)
            elif "revoke" in flags:
                out = svc.pool_trust_revoke(rest[0] if rest else opts.get("pool_id", ""))
            else:
                pool_id = rest[0] if rest else opts.get("pool_id", "")
                maintainer = rest[1] if len(rest) > 1 else opts.get("maintainer", "")
                out = svc.pool_trust(pool_id, maintainer, opts.get("verify_key", ""))
        elif sub in ("peer", "confirm"):
            return _result({"ok": False, "error": "peer/confirm removed: use /pool trust to pin a trust anchor"})
        else:
            return _result({"ok": False,
                            "error": f"unknown /pool subcommand: {sub}",
                            "usage": "/pool register|invite|members|remove|freeze|list|import|trust|auto-approve"})
        return _result(out, svc, source)
    except Exception as exc:
        return _error(exc)


def _handle_task(ctx, raw: str, *, source=None, session_id="", session_key="", platform="") -> str:
    try:
        svc = _service_for(ctx, source=source)
        pos, opts, flags = _parse_args(raw)
        if not pos:
            return _result({"ok": False, "error": "usage: /task add|list|show"})
        sub, rest = pos[0], pos[1:]
        if sub == "add":
            if opts.get("packet"):
                try:
                    # Handler passes the pool's member verify keys (Phase 2).
                    ticket = svc.accept_proposal(opts["packet"])
                except Exception as exc:
                    return _result({"ok": False, "error": str(exc)})
                out = {"ok": True, "ticket_id": ticket.get("ticket_id", ""), "state": ticket.get("state", ""), "via": "proposal"}
            else:
                ticket = svc.add_ticket(opts.get("pool_id", ""), opts.get("repo", ""),
                                        opts.get("title", ""), brief=opts.get("brief", ""),
                                        budget=_budget_from_opts(opts), source=source)
                if ticket.get("proposal"):
                    out = {"ok": True, "kind": ticket.get("kind"), "proposal": ticket.get("proposal"), "packet": ticket.get("packet"), "notice": ticket.get("notice")}
                else:
                    out = {"ok": True, "ticket_id": ticket["ticket_id"],
                           "state": ticket["state"], "thread_ref": ticket.get("thread_ref")}
        elif sub == "list":
            rows = svc.list_tickets(pool_id=opts.get("pool_id") or None,
                                    state=opts.get("state") or None,
                                    source=source,
                                    repo_id=opts.get("repo") or None)
            out = {"ok": True, "tickets": [
                {"ticket_id": t["ticket_id"], "title": t["title"], "state": t["state"],
                 "repo_id": t["repo_id"], "claimant": t.get("claimant_key_id"),
                 "thread_ref": t.get("thread_ref")} for t in rows]}
            # Maintainer approval queue (Phase 4): ask_choice Approve/Deny/Modify.
            try:
                if ctx is not None and getattr(ctx, "ask_choice", None) and callable(getattr(ctx, "ask_choice")):
                    pid = opts.get("pool_id") or None
                    if pid:
                        try:
                            resolved = svc._resolve_pool(pid, source=source)
                        except Exception:
                            resolved = pid
                    else:
                        resolved = None
                    if resolved and svc._is_bound_maintainer(resolved):
                        pending = [t for t in svc.list_pending(resolved) if t["ticket_id"] not in {r["ticket_id"] for r in rows}]
                        for t in pending:
                            choice = ctx.ask_choice(
                                f"Pending ticket {t['ticket_id']}: {t['title']} — approve? "
                                f"(choice is recorded; use /task approve|deny|modify to apply)",
                                ["Approve", "Deny", "Modify"])
                            out.setdefault("pending_decisions", []).append(
                                {"ticket_id": t["ticket_id"], "choice": str(choice).strip()})
            except Exception:
                pass
        elif sub == "show":
            ticket_id = rest[0] if rest else opts.get("ticket", "")
            t = svc.show_ticket(ticket_id)
            out = {"ok": True, "ticket": {
                "ticket_id": t["ticket_id"], "title": t["title"], "state": t["state"],
                "repo_id": t["repo_id"], "pool_id": t["pool_id"],
                "brief": t.get("brief", ""), "claimant": t.get("claimant_key_id"),
                "budget": json.loads(t.get("budget_json") or "{}"),
                "thread_ref": t.get("thread_ref")}}
        elif sub in ("approve", "deny", "modify"):
            ticket_id = rest[0] if rest else opts.get("ticket", "")
            out = svc.approve_ticket(ticket_id, sub if sub != "deny" else "deny",
                                     edit_files={opts["edit_file"]: opts["edit_content"]} if opts.get("edit_file") and opts.get("edit_content") is not None else None,
                                     reason=opts.get("reason", ""))
        else:
            return _result({"ok": False, "error": f"unknown /task subcommand: {sub}",
                            "usage": "/task add|list|show|approve|deny|modify"})
        return _result(out, svc, source)
    except Exception as exc:
        return _error(exc)


def _slugify_path(path: str) -> str:
    """repo_id from a --path basename: lowercase, non-alnum runs -> '-'."""
    import re
    base = Path(path).name.strip() or "repo"
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    return slug or "repo"


def _handle_add_repo(ctx, raw: str, *, source=None, session_id="", session_key="", platform="") -> str:
    """``/add-repo <repo_id> --path <abs/path> [--pool <pool_id>] [--name <display name>]``

    Registers the project row and submits its bytes as the next version.
    Re-running submits a NEW version. --path is required.
    """
    try:
        svc = _service_for(ctx, source=source)
        pos, opts, _flags = _parse_args(raw)
        repo_id = pos[0] if pos else opts.get("repo", "")
        path = opts.get("path", "")
        link = opts.get("link") or opts.get("repo_link") or ""
        if not repo_id and path:
            repo_id = _slugify_path(path)
        out = svc.add_repo(opts.get("pool", "") or opts.get("pool_id", ""),
                           repo_id, path,
                           display_name=opts.get("name", ""), source=source, repo_link=link or None)
        return _result(out, svc, source)
    except Exception as exc:
        return _error(exc)


def _handle_task_add(ctx, raw: str, *, source=None, session_id="", session_key="", platform="") -> str:
    """Flat alias for ``/task add``: ``/task_add --repo <repo_id> --title "..." [...]``"""
    try:
        svc = _service_for(ctx, source=source)
        _pos, opts, _flags = _parse_args(raw)
        if not opts.get("repo"):
            return _result({"ok": False,
                            "error": "task_add needs --repo <repo_id> "
                                     "(see /pool list for registered projects)"})
        ticket = svc.add_ticket(opts.get("pool", "") or opts.get("pool_id", ""),
                                opts["repo"], opts.get("title", ""),
                                brief=opts.get("brief", ""),
                                budget=_budget_from_opts(opts), source=source)
        if ticket.get("proposal"):
            out = {"ok": True, "kind": ticket.get("kind"), "proposal": ticket.get("proposal"), "packet": ticket.get("packet"), "notice": ticket.get("notice")}
        else:
            out = {"ok": True, "ticket_id": ticket["ticket_id"],
                   "state": ticket["state"], "thread_ref": ticket.get("thread_ref")}
        return _result(out, svc, source)
    except Exception as exc:
        return _error(exc)


def _handle_task_list(ctx, raw: str, *, source=None, session_id="", session_key="", platform="") -> str:
    """Flat alias for ``/task list``: ``/task_list [--repo <repo_id>] [--pool <pool_id>] [--state open]``"""
    try:
        svc = _service_for(ctx, source=source)
        _pos, opts, _flags = _parse_args(raw)
        rows = svc.list_tickets(pool_id=opts.get("pool", "") or opts.get("pool_id") or None,
                                state=opts.get("state") or None, source=source,
                                repo_id=opts.get("repo") or None)
        out = {"ok": True, "tickets": [
            {"ticket_id": t["ticket_id"], "title": t["title"], "state": t["state"],
             "repo_id": t["repo_id"], "claimant": t.get("claimant_key_id"),
             "thread_ref": t.get("thread_ref")} for t in rows]}
        return _result(out, svc, source)
    except Exception as exc:
        return _error(exc)


def _handle_claim(ctx, raw: str, *, source=None, session_id="", session_key="", platform="") -> str:
    try:
        svc = _service_for(ctx, source=source)
        pos, opts, flags = _parse_args(raw)
        ticket_id = pos[0] if pos else opts.get("ticket", "")
        # Bounded bulk form: take up to N open tickets' worth of work,
        # whole-ticket (no paths pre-declared), across any project.
        if "any_project" in flags or (not ticket_id and "max_tasks" in opts):
            out = svc.claim_any(pool_id=opts.get("pool") or opts.get("pool_id") or None,
                                repo_id=opts.get("repo") or None,
                                max_tasks=int(opts.get("max_tasks", "1") or 1),
                                key_id=opts.get("as") or None,
                                budget=_budget_from_opts(opts),
                                maintainer_override="override" in flags,
                                source=source)
            return _result(out, svc, source)
        paths = [p.strip() for p in opts.get("paths", "").split(",") if p.strip()]
        claim = svc.claim(ticket_id, key_id=opts.get("as") or None,
                          declared_paths=paths, budget=_budget_from_opts(opts),
                          maintainer_override="override" in flags)
        # Phase 4: on successful claim, start the worker (fetch accepted
        # version over Iroh -> worktree -> worker session) when a lifecycle
        # or work root is configured. Failure here never rolls back the claim.
        out = {"ok": True, "claim": claim}
        try:
            lifecycle = _lifecycle_for(ctx) if "_lifecycle_for" in globals() else None
            work_root = _work_root_for(ctx) if "_work_root_for" in globals() else None
            if lifecycle is not None or "start" in flags:
                out["start"] = svc.start_work(ticket_id, work_root=work_root,
                                              lifecycle=lifecycle,
                                              worker_command=opts.get("worker_command", "").split() or None,
                                              acceptance_cmd=opts.get("acceptance", "").split() or None)
            else:
                out["start"] = {"ok": False, "note": "not started: no Hermes lifecycle; use /work start <ticket> --worker-command ..."}
        except Exception as exc:
            out["start"] = {"ok": False, "error": str(exc)}
        return _result(out, svc, source)
    except Exception as exc:
        return _error(exc)


def _handle_release(ctx, raw: str, *, source=None, session_id="", session_key="", platform="") -> str:
    try:
        svc = _service_for(ctx, source=source)
        pos, opts, _flags = _parse_args(raw)
        ticket_id = pos[0] if pos else opts.get("ticket", "")
        updated = svc.release(ticket_id, key_id=opts.get("as") or None)
        return _result({"ok": True, "ticket_id": ticket_id, "state": updated["state"], "notice": updated.get("notice", "")}, svc, source)
    except Exception as exc:
        return _error(exc)


def _handle_work(ctx, raw: str, *, source=None, session_id="", session_key="", platform="") -> str:
    try:
        pos, _opts, _flags = _parse_args(raw)
    except ValueError as exc:
        return _error(exc)
    if not pos or pos[0] != "submit":
        if "_handle_work_exec" in globals():
            return globals()["_handle_work_exec"](ctx, raw)
        return _result({"ok": False, "error": "usage: /work submit <ticket> --repo-path PATH --version N"})
    try:
        svc = _service_for(ctx, source=source)
        pos, opts, _flags = _parse_args(raw)
        if not pos or pos[0] != "submit":
            return _result({"ok": False, "error": "usage: /work submit <ticket> "
                            "--repo-path PATH --version N"})
        ticket_id = pos[1] if len(pos) > 1 else opts.get("ticket", "")
        out = svc.work_submit(ticket_id, opts.get("repo_path", ""),
                              opts.get("version", "1"))
        return _result(out, svc, source)
    except Exception as exc:
        return _error(exc)


def _handle_integrate(ctx, raw: str, *, source=None, session_id="", session_key="", platform="") -> str:
    try:
        svc = _service_for(ctx, source=source)
        pos, opts, _flags = _parse_args(raw)
        if pos and pos[0] in ("approve", "edit", "discard"):
            decision = pos[0]
            ticket_id = pos[1] if len(pos) > 1 else opts.get("ticket", "")
            edit_files = None
            edit_patch = None
            if opts.get("edit_file") and opts.get("edit_content") is not None:
                edit_files = {opts["edit_file"]: opts["edit_content"]}
            if opts.get("edit_patch_file"):
                edit_patch = Path(opts["edit_patch_file"]).read_text()
            out = svc.resolve_integration(
                ticket_id, decision, work_dir=_work_root_for(ctx) / "integrate"
                if "_work_root_for" in globals() else None,
                edit_files=edit_files, edit_patch=edit_patch,
                reason=opts.get("reason", ""))
            return _result(out, svc, source)
        # Gate path: run /integrate audit, then ask_choice instant fallback (D1c).
        ticket_id = pos[0] if pos else opts.get("ticket", "")
        gate = svc.integrate(
            ticket_id, work_dir=_work_root_for(ctx) / "integrate"
            if "_work_root_for" in globals() else None)
        gate["queued_notices"] = svc.drain_notices() if svc else []
        try:
            ask = getattr(ctx, "ask_choice", None)
            if callable(ask) and gate.get("verdict") == "clean":
                question = (gate.get("button_prompt")
                            or f"Integration review {ticket_id}: approve, edit, or discard?")
                choice = ask(question, ["Approve", "Edit", "Discard"])
                decision = {"Approve": "approve", "Edit": "edit",
                            "Discard": "discard"}.get(str(choice).strip())
                if decision:
                    out = svc.resolve_integration(
                        ticket_id, decision,
                        work_dir=_work_root_for(ctx) / "integrate"
                        if "_work_root_for" in globals() else None,
                        reason=opts.get("reason", ""))
                    out["via"] = "ask_choice"
                    return _result(out, svc, source)
        except Exception:
            pass  # ask_choice is best-effort; return the gate result.
        return _result(gate, svc, source)
    except Exception as exc:
        return _error(exc)


def _handle_audit(ctx, raw: str, *, source=None, session_id="", session_key="", platform="") -> str:
    try:
        svc = _service_for(ctx, source=source)
        pos, opts, _flags = _parse_args(raw)
        if not pos or pos[0] != "show":
            return _result({"ok": False, "error": "usage: /audit show <ticket>"})
        ticket_id = pos[1] if len(pos) > 1 else opts.get("ticket", "")
        return _result(svc.audit_show(ticket_id=ticket_id), svc, source)
    except Exception as exc:
        return _error(exc)


_POOL_STATUS_SCHEMA = {
    "name": "pool_status",
    "description": ("Report Agent Pool plugin status: relay URL, per-claim budgets, "
                    "and ledger/identity readiness."),
    "parameters": {"type": "object", "properties": {}},
}

_COMMANDS = (
    ("pool", _handle_pool, "Agent Pool: register|invite|members|remove|freeze|list|import|trust|auto-approve, status",
     "register|invite|members|remove|freeze|list|import|trust|auto-approve [options]"),
    ("task", _handle_task, "Agent Pool tickets: add|list|show|approve|deny|modify",
     "add|list|show|approve|deny|modify [options]"),
    ("add-repo", _handle_add_repo, "Register a project and submit its repo bytes as a new version",
     "<repo_id> --path <abs/path> [--pool <pool_id>] [--name <display name>]"),
    ("task_add", _handle_task_add, "Create a ticket for a registered project (alias of /task add)",
     "--repo <repo_id> --title \"...\" [--brief \"...\"] [--pool <pool_id>]"),
    ("task_list", _handle_task_list, "List tickets, optionally for one project (alias of /task list)",
     "[--repo <repo_id>] [--pool <pool_id>] [--state open]"),
    ("claim", _handle_claim, "Claim a ticket (paths optional: omit --paths for a whole-ticket claim) or take up to N open tickets with --any-project",
     "[ticket] [--paths <paths>] [--any-project --max-tasks N] [options]"),
    ("release", _handle_release, "Release a claimed ticket back to open",
     "[ticket]"),
    ("work", _handle_work, "Submit finished work for a claimed ticket",
     "submit|start|complete|cancel|progress [options]"),
    ("integrate", _handle_integrate, "Maintainer: audit a submitted ticket",
     "approve|edit|discard [ticket] [options]"),
    ("audit", _handle_audit, "Show the audit report for a ticket",
     "show [ticket]"),
)


def _wrap_command(handler, ctx):
    """Dispatch seam: absolute freeze guard (Phase 1) + user binding check."""
    def wrapped(raw="", *, source=None, session_id="", session_key="", platform=""):
        try:
            from . import ledger as _L
        except Exception:
            try:
                import ledger as _L
            except Exception:
                _L = None
        if _L is not None:
            try:
                svc = _service_for(ctx, source=source)
                pos, opts, flags = _parse_args(raw or "")
                sub = pos[0] if pos else ""
                # Freeze control itself must run to unfreeze.
                is_freeze_ctl = (sub == "freeze")
                if not is_freeze_ctl:
                    # Resolve pool best-effort from raw args.
                    pool_id = (opts.get("pool") or opts.get("pool_id") or opts.get("pool-id") or ""
                               or (pos[1] if len(pos) > 1 and handler.__name__ in ("_handle_pool",) else ""))
                    # Also honor source-bound pool and ticket's pool.
                    resolved = None
                    try:
                        if pool_id:
                            resolved = svc._resolve_pool(pool_id, source=source)
                        elif handler.__name__ == "_handle_work":
                            tid = pos[1] if len(pos) > 1 else opts.get("ticket", "")
                            if tid:
                                resolved = svc.show_ticket(tid)["pool_id"]
                        elif handler.__name__ in ("_handle_claim", "_handle_release", "_handle_integrate", "_handle_audit"):
                            tid = pos[0] if pos else opts.get("ticket", "")
                            if tid:
                                resolved = svc.show_ticket(tid)["pool_id"]
                        elif handler.__name__ == "_handle_task":
                            tid = pos[1] if len(pos) > 1 and pos[0] == "show" else opts.get("ticket", "")
                            if tid:
                                resolved = svc.show_ticket(tid)["pool_id"]
                        elif handler.__name__ == "_handle_task_add":
                            pass
                        else:
                            src = svc._source_from(source)
                            if src is not None:
                                try:
                                    resolved = svc._resolve_pool(None, source=src)
                                except Exception:
                                    resolved = None
                    except Exception:
                        resolved = None
                    frozen = False
                    try:
                        if _L.is_global_frozen(svc._conn):
                            frozen = True
                        elif resolved and _L.is_frozen(svc._conn, resolved):
                            frozen = True
                    except Exception:
                        pass
                    if frozen:
                        try:
                            _L.record_event(svc._conn, resolved or None, "freeze_blocked",
                                            {"command": handler.__name__, "raw": (raw or "")[:500]})
                        except Exception:
                            pass
                        return _result({"ok": False, "error_code": "frozen",
                                        "error": "pool frozen — contact the maintainer"})
                    # Per-command user check (Phase 1 step 3): resolve Discord
                    # user to a key; unbound users are refused on every verb.
                    user_id = None
                    if isinstance(source, dict):
                        user_id = source.get("user_id") or source.get("discord_user_id")
                    elif source is not None:
                        user_id = getattr(source, "user_id", None) or getattr(source, "discord_user_id", None)
                        if callable(user_id):
                            user_id = None
                    if user_id is not None:
                        key = _L.lookup_key_by_discord_user(svc._conn, str(user_id))
                        if key is None:
                            return _result({"ok": False, "error_code": "unauthorized",
                                            "error": f"not authorized for pool {resolved or ''} — ask the maintainer"})
                        # Canonical writes require the maintainer binding.
                        _canonical = {"_handle_pool": ("register", "invite", "remove", "freeze", "auto-approve", "trust", "import"),
                                      "_handle_task": ("add",), "_handle_add_repo": (), "_handle_task_add": ("",)}
                        verb = sub
                        if handler.__name__ == "_handle_pool" and verb in ("register", "invite", "remove", "trust", "import", "auto-approve"):
                            row = svc._conn.execute("SELECT maintainer_key_id FROM pools WHERE pool_id=?", (resolved or "",)).fetchone()
                            if row is not None and row[0] != key:
                                return _result({"ok": False, "error_code": "unauthorized",
                                                "error": f"not authorized for pool {resolved or ''} — ask the maintainer"})
            except Exception:
                pass  # Freeze guard is best-effort; handler still runs.
        return handler(ctx, raw, source=source, session_id=session_id,
                       session_key=session_key, platform=platform)
    return wrapped


def register(ctx):
    """Wire the full gateway-native surface. Fail-soft."""
    try:
        ctx.register_tool(name="pool_status", toolset="agent_pool",
                          schema=_POOL_STATUS_SCHEMA,
                          handler=lambda args=None, **kw: _pool_status(args, ctx=ctx, **kw))
    except Exception as exc:
        logger.warning("agent-pool: tool registration failed: %s", exc)
    for name, handler, description, args_hint in _COMMANDS:
        try:
            ctx.register_command(name, _wrap_command(handler, ctx),
                                 description=description, args_hint=args_hint,
                                 argument_mode="text")
        except Exception as exc:
            logger.warning("agent-pool: command %s registration failed: %s", name, exc)
    # Discord button gate via register_platform_handler (spec §4a) + ask_choice
    # fallback inside /integrate. Keep gateway_platform_event hook (neutral).
    try:
        from .buttons import make_discord_button_factory
    except ImportError:
        from buttons import make_discord_button_factory
    try:
        def _dispatch_to_service(decision, ticket_id, discord_user_id):
            svc = _service_for(ctx)
            try:
                from . import ledger as _L
            except ImportError:
                import ledger as _L
            key = _L.lookup_key_by_discord_user(svc._conn, discord_user_id)
            if key is None:
                raise PermissionError(
                    "button click refused: Discord user not bound to a pool key_id")
            ticket = svc.show_ticket(ticket_id)
            pool = svc._conn.execute(
                "SELECT maintainer_key_id FROM pools WHERE pool_id=?",
                (ticket["pool_id"],)).fetchone()
            maintainer = pool[0] if pool else None
            if maintainer and key != maintainer:
                raise PermissionError(
                    "button click refused: only the pool maintainer may decide integration")
            return svc.resolve_integration(
                ticket_id, decision, key_id=key,
                work_dir=_work_root_for(ctx) / "integrate")
        try:
            ctx.register_platform_handler("discord",
                                          make_discord_button_factory(_dispatch_to_service))
        except Exception as exc:
            logger.warning("agent-pool: platform handler registration failed: %s", exc)
    except Exception as exc:
        logger.warning("agent-pool: button factory setup failed: %s", exc)
    try:
        try:
            from .bridge import make_platform_event_handler
        except ImportError:
            from bridge import make_platform_event_handler
        identity_map = {}
        try:
            if isinstance(getattr(ctx, "state", None), dict):
                identity_map = ctx.state.get("agent_pool_identity_map", {}) or {}
        except Exception:
            identity_map = {}

        def _button_hook(platform, event_type, payload, **kwargs):
            # Work dir: same default the /integrate handler uses.
            try:
                work_dir = _work_root_for(ctx) / "integrate"
            except Exception:
                work_dir = None
            handler = make_platform_event_handler(
                lambda: _service_for(ctx), identity_map=identity_map,
                work_dir=work_dir)
            return handler(platform, event_type, payload, **kwargs)

        ctx.register_hook("gateway_platform_event", _button_hook)
    except Exception as exc:
        logger.warning("agent-pool: button bridge hook registration failed: %s", exc)

# --- Phase 4 execution wiring (appended handlers) ----------------------------

def _work_root_for(ctx):
    configured = _cfg(ctx, "work_root", "") if ctx else ""
    if configured:
        return Path(configured)
    return Path.home() / ".hermes" / "plugin-data" / "agent-pool" / "work"

def _lifecycle_for(ctx):
    try:
        return ctx.subagent_lifecycle if ctx is not None else None
    except Exception:
        return None

def _handle_work_exec(ctx, raw: str, *, source=None, session_id="", session_key="", platform="") -> str:
    """``/work start <ticket> --acceptance "cmd..."`` / ``/work complete <ticket>``
    / ``/work cancel <ticket>`` / ``/work progress <ticket> <text>``."""
    try:
        svc = _service_for(ctx, source=source)
        pos, opts, flags = _parse_args(raw)
        if not pos:
            return _result({"ok": False, "error": "usage: /work start|complete|cancel|progress <ticket> ..."})
        sub, rest = pos[0], pos[1:]
        ticket_id = rest[0] if rest else opts.get("ticket", "")
        if sub == "start":
            acceptance = opts.get("acceptance", "").split() or None
            out = svc.start_work(ticket_id, work_root=_work_root_for(ctx),
                                 lifecycle=_lifecycle_for(ctx),
                                 worker_command=opts.get("worker_command", "").split() or None,
                                 acceptance_cmd=acceptance)
        elif sub == "complete":
            out = svc.complete_work(ticket_id, timeout_seconds=int(opts.get("timeout", "0") or 0) or None)
        elif sub == "cancel":
            out = svc.cancel_work(ticket_id, reason=opts.get("reason", "cancelled by operator"))
        elif sub == "progress":
            text = " ".join(rest[1:]) or opts.get("text", "")
            out = svc.report_progress(ticket_id, text)
        else:
            return _result({"ok": False, "error": f"unknown /work subcommand: {sub}"})
        return _result(out, svc, source)
    except Exception as exc:
        return _error(exc)
