"""Agent Pool plugin — gateway-native: Hermes posts, plugin returns content.

Slash commands: ``/pool register|members|remove|freeze|list, status``,
``/task add|list|show`` (flat aliases ``/task_add`` / ``/task_list``),
``/add-repo`` (register a project + submit its bytes as a new version),
``/claim`` (paths optional — omit ``--paths`` for a whole-ticket claim;
``--any-project --max-tasks N`` takes up to N open tickets), ``/release``,
``/fetch repo list`` (projects + available tasks + gateway addressing),
``/fetch task list`` (tasks with claimed status), ``/fetch repo <id>``
(latest accepted signed manifest), ``/fetch repo --manifest <json>``
(download + decrypt + clone on the contributor's own gateway),
``/push repo <repo_id> --ticket <id>`` (claimant-only push link),
``/push repo --deliver <ticket> --token <grant> --manifest <json>``
(the pool gateway audits the pushed bundle and completes the task),
``/audit show``. Every handler returns a string Hermes posts; the plugin
never posts to Discord itself.

Authorization is the whitelisted Discord user id ONLY (owner or
pool_authorized). Keys are crypto-only: an ed25519 signing key proves
authorship of an artifact, a box keypair wraps per-version DEKs. No key
ever decides who may act.
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


def _tor_config(ctx):
    """Resolve tor settings (control_port, socks_port, tor_binary).

    Defaults match ops/torrc: ControlPort 9051, SocksPort 9055, both on
    127.0.0.1. ``tor_binary`` empty = tor-enabled send_hermes on PATH
    (option (A)); non-empty = option (B) standalone sidecar binary.
    """
    control = _cfg(ctx, "tor_control_port", 9051) if ctx else 9051
    socks = _cfg(ctx, "tor_socks_port", 9055) if ctx else 9055
    binary = _cfg(ctx, "tor_binary", "") if ctx else ""
    try:
        control = int(control)
    except (TypeError, ValueError):
        control = 9051
    try:
        socks = int(socks)
    except (TypeError, ValueError):
        socks = 9055
    return control, socks, (str(binary).strip() or None)


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
                      "tor: onion-routed via Iroh Tor custom transport "
                      "(sidecar + tor daemon); peer learns no IP, no "
                      "relay in the connection path; both endpoints "
                      "must enable Tor"
                      if mode == "tor" else
                      "relay-only on both sender and receiver: the peer "
                      "learns no IP; the relay sees both peers' IPs and "
                      "transfer metadata but never plaintext; there is "
                      "no direct mode and no way to opt into one"),
                  "pool_transports": per_pool,
                  "budgets": budgets,
                  "ledger": "ready", "identity": "ready",
                  "queued_notices": []}
        if mode == "tor":
            # Surface tor readiness (probe is bounded, never raises).
            control, socks, tor_binary = _tor_config(ctx)
            status["tor"] = {"control_port": control, "socks_port": socks,
                             "binary": tor_binary or "send_hermes (PATH)"}
            try:
                try:
                    from .transport import tor_ready as _tor_ready
                except ImportError:
                    from transport import tor_ready as _tor_ready
                status["tor"]["ready"] = _tor_ready(
                    control_port=control, socks_port=socks,
                    binary=tor_binary)
            except Exception as exc:
                status["tor"]["ready"] = {"ok": False,
                                          "error": str(exc)[:300]}
                status["transport_warning"] = (
                    f"transport_mode=tor but the Tor anonymity layer is "
                    f"not ready: {str(exc)[:200]} — tor transfers fail "
                    "closed; tor mode never falls back to relay.")
        if mode != "tor" and env_relay.lower() in {"off", "disabled", "none"}:
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

def _whitelist_user_ids(ctx):
    """Resolve the pool whitelist (Phase 1): env beats config.

    Source order: ``AGENT_POOL_ALLOWED_USERS`` (comma-separated Discord
    user ids) when non-empty, else the ``allowed_users`` config key (list,
    or a comma-separated string). The pool owner is NOT added here —
    ``L.sync_whitelist`` always ensures the owner. Empty result means the
    owner is the only authorized user.
    """
    env = os.environ.get("AGENT_POOL_ALLOWED_USERS", "")
    if env.strip():
        return [u.strip() for u in str(env).split(",") if u.strip()]
    cfg = _cfg(ctx, "allowed_users", []) if ctx else []
    if isinstance(cfg, str):
        return [u.strip() for u in cfg.split(",") if u.strip()]
    if isinstance(cfg, (list, tuple, set)):
        return [str(u).strip() for u in cfg if str(u).strip()]
    return []


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
    # Phase 1 whitelist sync: once per service construction, for the single
    # hosted pool, insert env/config whitelist users into pool_authorized
    # (owner always ensured; existing rows never deleted).
    try:
        _hosts = L.list_local_pools(conn)
        if len(_hosts) == 1:
            L.sync_whitelist(conn, _hosts[0], _whitelist_user_ids(ctx))
    except Exception:
        pass
    data_dir = L.default_db_path().parent
    identity = ensure_identity(data_dir)
    transport_name = _cfg(ctx, "transport", "real") if ctx else "real"
    mode, relay_url, per_pool = _transport_config(ctx)
    # An empty relay_url in config means "unset" (env/CLI default apply);
    # a non-empty one beats the env var. A non-empty "off" is rejected by
    # the transport constructor (relay-only cannot be opted out of).
    relay_arg = str(relay_url).strip() or None

    def _make(m, r):
        if transport_name == "fake":
            return FakeTransport(transport_mode=m)
        control, socks, tor_binary = _tor_config(ctx)
        return RealSendHermesTransport(
            transport_mode=m, relay_url=r,
            tor_control_port=control, tor_socks_port=socks,
            tor_binary=tor_binary)

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
        elif sub == "members":
            pool_id = rest[0] if rest else opts.get("pool_id", "")
            out = {"ok": True, "pool_id": pool_id, "members": svc.members(pool_id, source=source)}
        elif sub == "freeze":
            # `/pool freeze` (no pool id, or --global) = GLOBAL kill-switch
            # across every hosted pool; `/pool freeze <pool_id>` = per-pool.
            frozen = "unfreeze" not in flags
            pool_id = rest[0] if rest else opts.get("pool_id", "")
            if not pool_id or "global" in flags or pool_id in ("all", "*"):
                out = svc.freeze_global(frozen, source=source)
            else:
                out = svc.freeze(pool_id, frozen=frozen, source=source)
        elif sub == "remove":
            pool_id = rest[0] if rest else opts.get("pool_id", "")
            key_id = rest[1] if len(rest) > 1 else opts.get("key_id", "")
            out = svc.remove_member(pool_id, key_id, source=source)
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
        elif sub in ("import", "trust", "peer", "confirm"):
            return _result({"ok": False, "error": "import/trust/peers removed: use /fetch repo list / /fetch task list / /fetch repo (reads are fetch commands on the pool gateway)"})
        else:
            return _result({"ok": False,
                            "error": f"unknown /pool subcommand: {sub}",
                            "usage": "/pool register|members|remove|freeze|list"})
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
            ticket = svc.add_ticket(opts.get("pool_id", ""), opts.get("repo", ""),
                                    opts.get("title", ""), brief=opts.get("brief", ""),
                                    budget=_budget_from_opts(opts), source=source)
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
        elif sub == "show":
            ticket_id = rest[0] if rest else opts.get("ticket", "")
            t = svc.show_ticket(ticket_id)
            out = {"ok": True, "ticket": {
                "ticket_id": t["ticket_id"], "title": t["title"], "state": t["state"],
                "repo_id": t["repo_id"], "pool_id": t["pool_id"],
                "brief": t.get("brief", ""), "claimant": t.get("claimant_key_id"),
                "budget": json.loads(t.get("budget_json") or "{}"),
                "thread_ref": t.get("thread_ref")}}
        else:
            return _result({"ok": False, "error": f"unknown /task subcommand: {sub}",
                            "usage": "/task add|list|show"})
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
        pos, opts, _flags = _parse_args(raw)
        if "link" in opts or "link" in _flags:
            raise ValueError(
                "add-repo: --link is not supported; use /add-repo --path <checkout>"
            )
        svc = _service_for(ctx, source=source)
        repo_id = pos[0] if pos else opts.get("repo", "")
        path = opts.get("path", "")
        if not repo_id and path:
            repo_id = _slugify_path(path)
        out = svc.add_repo(opts.get("pool", "") or opts.get("pool_id", ""),
                           repo_id, path,
                           display_name=opts.get("name", ""), source=source)
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
                          maintainer_override="override" in flags, source=source)
        out = {"ok": True, "claim": claim}
        return _result(out, svc, source)
    except Exception as exc:
        return _error(exc)


def _handle_release(ctx, raw: str, *, source=None, session_id="", session_key="", platform="") -> str:
    try:
        svc = _service_for(ctx, source=source)
        pos, opts, _flags = _parse_args(raw)
        ticket_id = pos[0] if pos else opts.get("ticket", "")
        updated = svc.release(ticket_id, key_id=opts.get("as") or None, source=source)
        return _result({"ok": True, "ticket_id": ticket_id, "state": updated["state"], "notice": updated.get("notice", "")}, svc, source)
    except Exception as exc:
        return _error(exc)


def _handle_fetch(ctx, raw: str, *, source=None, session_id="", session_key="", platform="") -> str:
    """``/fetch repo list`` / ``/fetch task list [--ticket <id>]`` /
    ``/fetch repo <repo_id>`` / ``/fetch repo --manifest <json> [--dest DIR]``."""
    try:
        svc = _service_for(ctx, source=source)
        pos, opts, _flags = _parse_args(raw)
        if not pos:
            return _result({"ok": False, "error": "usage: /fetch repo list | /fetch task list [--ticket <id>] | /fetch repo <repo_id> | /fetch repo --manifest <json>"})
        kind = pos[0]
        if kind == "repo":
            if opts.get("manifest"):
                try:
                    manifest = json.loads(opts["manifest"])
                except Exception as exc:
                    return _result({"ok": False, "error": f"bad manifest JSON: {exc}"})
                out = svc.fetch_repo_download(manifest, dest=opts.get("dest", "/tmp/fetch"), source=source)
            elif len(pos) > 1 and pos[1] == "list":
                out = svc.fetch_repo_list(pool_id=opts.get("pool") or opts.get("pool_id") or None, source=source)
            else:
                repo_id = pos[1] if len(pos) > 1 else opts.get("repo", "")
                if not repo_id:
                    return _result({"ok": False, "error": "usage: /fetch repo <repo_id> or /fetch repo list or /fetch repo --manifest <json>"})
                out = svc.fetch_repo(repo_id, pool_id=opts.get("pool") or opts.get("pool_id") or None, source=source)
        elif kind == "task":
            sub = pos[1] if len(pos) > 1 else ""
            if sub != "list":
                return _result({"ok": False, "error": "usage: /fetch task list [--ticket <id>]"})
            out = svc.fetch_task_list(pool_id=opts.get("pool") or opts.get("pool_id") or None,
                                      source=source, ticket=opts.get("ticket") or None)
        else:
            return _result({"ok": False, "error": f"unknown /fetch subcommand: {kind}",
                            "usage": "/fetch repo list | /fetch task list [--ticket <id>] | /fetch repo <repo_id> | /fetch repo --manifest <json>"})
        return _result(out, svc, source)
    except Exception as exc:
        return _error(exc)


def _handle_push(ctx, raw: str, *, source=None, session_id="", session_key="", platform="") -> str:
    """``/push repo <repo_id> --ticket <id>`` (claimant-only grant; returns
    the push link) and ``/push repo --deliver <transfer_ticket> --token
    <grant> --manifest <json>`` (the pool gateway audits and completes the
    task). The bundle itself is built and sent by the contributor's own
    gateway (``push_build_bundle``); only the ticket + grant + signed
    manifest cross to the pool gateway."""
    try:
        svc = _service_for(ctx, source=source)
        pos, opts, _flags = _parse_args(raw)
        if not pos or pos[0] != "repo":
            return _result({"ok": False,
                            "error": "usage: /push repo <repo_id> --ticket <id> | "
                                     "/push repo --deliver <transfer_ticket> --token <grant> --manifest <json>"})
        pool_id = opts.get("pool") or opts.get("pool_id") or None
        if opts.get("deliver") is not None or opts.get("token") is not None:
            transfer_ticket = opts.get("deliver") or (pos[1] if len(pos) > 1 else "")
            if not transfer_ticket or not opts.get("token"):
                return _result({"ok": False,
                                "error": "usage: /push repo --deliver <transfer_ticket> --token <grant> --manifest <json>"})
            if not opts.get("manifest"):
                return _result({"ok": False,
                                "error": "push deliver needs --manifest <json> "
                                         "(the contributor-signed push manifest)"})
            try:
                manifest = json.loads(opts["manifest"])
            except Exception as exc:
                return _result({"ok": False, "error": f"bad manifest JSON: {exc}"})
            out = svc.push_deliver(transfer_ticket, opts["token"], manifest,
                                   pool_id=pool_id, source=source)
        else:
            repo_id = pos[1] if len(pos) > 1 else opts.get("repo", "")
            ticket_id = opts.get("ticket", "")
            if not ticket_id:
                return _result({"ok": False,
                                "error": "usage: /push repo <repo_id> --ticket <id>"})
            out = svc.push_repo(repo_id, ticket_id, pool_id=pool_id, source=source)
        return _result(out, svc, source)
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
    ("pool", _handle_pool, "Agent Pool: register|members|remove|freeze|list, status",
     "register|members|remove|freeze|list [options]"),
    ("fetch", _handle_fetch, "Fetch repo list (projects + available tasks + gateway addressing), task list (with claimed status), or one repo manifest / download form",
     "repo list | task list [--ticket <id>] | repo <repo_id> | repo --manifest <json> [options]"),
    ("task", _handle_task, "Agent Pool tickets: add|list|show",
     "add|list|show [options]"),
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
    ("push", _handle_push, "Push a diff for a claimed ticket: issue the push link (grant), or deliver a pushed bundle (audits and completes the task)",
     "repo <repo_id> --ticket <id> | repo --deliver <transfer_ticket> --token <grant> --manifest <json>"),
    ("audit", _handle_audit, "Show the audit report for a ticket",
     "show [ticket]"),
)


def _wrap_command(handler, ctx):
    """Dispatch seam: freeze guard + Discord-whitelist authz (2109 P1).

    Authorization is the whitelisted Discord user id only (owner or
    pool_authorized). Keys never decide access.
    """
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
                # Freeze control is owner-only (see _require_owner below);
                # unfreeze runs while frozen, freeze/unfreeze never bypass authz.
                is_freeze_ctl = (handler.__name__ == "_handle_pool" and sub == "freeze")
                # Resolve pool best-effort from raw args.
                pool_id = (opts.get("pool") or opts.get("pool_id") or opts.get("pool-id") or "")
                resolved = None
                try:
                    if pool_id:
                        resolved = svc._resolve_pool(pool_id, source=source)
                    elif handler.__name__ in ("_handle_claim", "_handle_release", "_handle_audit"):
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
                        try:
                            resolved = svc._resolve_pool(None, source=source)
                        except Exception:
                            resolved = None
                except Exception:
                    resolved = None
                # Extract driving user id (Discord whitelist is the auth).
                user_id = None
                if isinstance(source, dict):
                    user_id = source.get("user_id") or source.get("discord_user_id")
                elif source is not None:
                    user_id = getattr(source, "user_id", None) or getattr(source, "discord_user_id", None)
                    if callable(user_id):
                        user_id = None
                # Freeze/unfreeze: owner-only on every path.
                if is_freeze_ctl:
                    # Need pool context to check ownership; global freeze
                    # requires ownership of every hosted pool (or the single one).
                    _mp = None
                    if pool_id:
                        try:
                            _mp = svc._resolve_pool(pool_id, source=source)
                        except Exception:
                            _mp = None
                    elif resolved:
                        _mp = resolved
                    else:
                        try:
                            _mp = svc._resolve_pool(None, source=source)
                        except Exception:
                            _mp = None
                    if _mp is None:
                        # Global freeze with no resolvable pool: require
                        # ownership of all local pools (fail-closed).
                        hosts = _L.list_local_pools(svc._conn)
                        if not hosts or user_id is None or not all(
                                _L.is_owner(svc._conn, h, str(user_id)) for h in hosts):
                            return _result({"ok": False, "error_code": "unauthorized",
                                            "error": f"not authorized for pool {_mp or ''} — ask the maintainer"})
                    elif user_id is None or not _L.is_owner(svc._conn, _mp, str(user_id)):
                        # Also allow global-freeze owner check below; per-pool
                        # owner-only.
                        if sub == "freeze" and ("global" in flags or not pool_id):
                            hosts = _L.list_local_pools(svc._conn)
                            if not hosts or not all(_L.is_owner(svc._conn, h, str(user_id)) for h in hosts):
                                return _result({"ok": False, "error_code": "unauthorized",
                                                "error": f"not authorized for pool {_mp} — ask the maintainer"})
                        else:
                            return _result({"ok": False, "error_code": "unauthorized",
                                            "error": f"not authorized for pool {_mp} — ask the maintainer"})
                    # Owner passed; freeze/unfreeze proceeds (never blocked by freeze).
                else:
                    # Absolute freeze: every other verb from every user.
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
                    # Whitelist checks: unauthorized refused on every verb;
                    # authorized non-owner refused on owner-only verbs.
                    if user_id is not None:
                        if resolved is not None:
                            if not _L.is_authorized(svc._conn, resolved, str(user_id)):
                                return _result({"ok": False, "error_code": "unauthorized",
                                                "error": f"not authorized for pool {resolved} — ask the maintainer"})
                            _owner_only = False
                            if handler.__name__ == "_handle_pool" and sub in ("remove", "register"):
                                # register has no resolved pool yet (creation);
                                # treated as owner-only only when resolved.
                                if sub != "register":
                                    _owner_only = True
                            elif handler.__name__ == "_handle_task" and sub in ("add",):
                                _owner_only = True
                            elif handler.__name__ in ("_handle_add_repo", "_handle_task_add"):
                                _owner_only = True
                            elif handler.__name__ == "_handle_pool" and sub in ("list", "members"):
                                _owner_only = False
                            if _owner_only and not _L.is_owner(svc._conn, resolved, str(user_id)):
                                return _result({"ok": False, "error_code": "unauthorized",
                                                "error": f"not authorized for pool {resolved} — ask the maintainer"})
                        # user_id present but no resolvable pool: fail-closed
                        # for owner-only verbs, else let handler run (it will
                        # refuse pool resolution itself).
                        elif handler.__name__ == "_handle_pool" and sub in ("remove",):
                            return _result({"ok": False, "error_code": "unauthorized",
                                            "error": "not authorized — ask the maintainer"})
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
