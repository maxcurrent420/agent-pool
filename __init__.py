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
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _env_get(name, default=""):
    """Read a plugin env var via the profile secret scope.

    Falls back to the process env when no scope/module is available
    (tests / standalone); ``UnscopedSecretError`` and missing setups
    are treated as unset rather than crashing command handling.
    """
    try:
        from agent.secret_scope import get_secret

        val = get_secret(name, default)
        return default if val is None else val
    except Exception:
        return os.environ.get(name, default)


def _cfg(ctx, key, default):
    try:
        return ctx.get_config(key, default=default)
    except Exception:
        return default


def _legacy_relay_pools(ctx):
    """Pool ids (plus ``"<global>"``) whose config still names the
    removed relay transport. Those entries are ignored (treated as
    tor) and reported as a migration warning by ``_pool_status``."""
    legacy = []
    raw = _cfg(ctx, "transport_mode", "tor") if ctx else "tor"
    if str(raw or "").strip().lower() == "relay":
        legacy.append("<global>")
    pools_cfg = _cfg(ctx, "pools", {}) if ctx else {}
    if isinstance(pools_cfg, dict):
        for pid, cfg in pools_cfg.items():
            if isinstance(cfg, dict) and str(
                    cfg.get("transport_mode", "") or "").strip().lower() == "relay":
                legacy.append(str(pid))
    return legacy


def _transport_config(ctx):
    """Resolve (mode, relay_url, per_pool) from plugin config.

    Iroh-over-Tor is the only byte transport: mode is always ``tor``.
    A legacy top-level ``transport_mode=relay`` is ignored (treated
    as tor, see ``_legacy_relay_pools``); any other non-tor value
    raises via ``normalize_mode``. Per-pool ``transport_mode`` /
    ``relay_url`` overrides are ignored entirely — ``per_pool`` is
    always empty and ``relay_url`` is always ``""`` (retained in the
    return shape for callers; the key is deprecated/ignored).
    ``HERMES_IROH_RELAY`` is ignored: no relay flags are ever emitted.
    """
    try:
        from .transport import normalize_mode
    except ImportError:
        from transport import normalize_mode
    raw = _cfg(ctx, "transport_mode", "tor") if ctx else "tor"
    if str(raw or "").strip().lower() == "relay":
        mode = "tor"  # legacy relay config: ignored, migration warning
    else:
        mode = normalize_mode(raw)
    return mode, "", {}


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


_TOR_STATUS_CACHE = {"at": 0.0, "block": None}
_TOR_STATUS_TTL = 60.0


def _tor_status_block(ctx):
    """Tor readiness block (config + local probe), cached ~60s.

    The probe is a localhost socket check by the plugin itself — a local
    read, not a gateway round-trip. Never raises.
    """
    now = time.time()
    cached = _TOR_STATUS_CACHE.get("block")
    if cached is not None and (now - _TOR_STATUS_CACHE.get("at", 0.0)) < _TOR_STATUS_TTL:
        return dict(cached)
    control, socks, tor_binary = _tor_config(ctx)
    block = {"control_port": control, "socks_port": socks,
             "binary": tor_binary or "send_hermes (PATH)"}
    try:
        try:
            from .transport import tor_ready as _tor_ready
        except ImportError:
            from transport import tor_ready as _tor_ready
        block["ready"] = _tor_ready(
            control_port=control, socks_port=socks, binary=tor_binary)
    except Exception as exc:
        block["ready"] = {"ok": False, "error": str(exc)[:300]}
    _TOR_STATUS_CACHE["at"] = now
    _TOR_STATUS_CACHE["block"] = dict(block)
    return block


def _status_snapshot(ctx=None):
    """Shared read-only status snapshot used by ``pool_status`` and the
    dashboard read route. Service/plugin_db reads plus a cached local Tor
    probe (localhost sockets only — no gateway round-trip). Returns the
    status dict (never raises)."""
    mode, _relay_url, per_pool = _transport_config(ctx)
    # relay_url is deprecated/ignored: no relay in the connection
    # path. Kept as "" for shape compatibility.
    relay_url = ""
    env_relay = _env_get("HERMES_IROH_RELAY", "").strip()
    legacy_relay = _legacy_relay_pools(ctx)
    budgets = {k: _cfg(ctx, k, d) if ctx else d for k, d in (
        ("max_turns", 25), ("max_tasks_per_day", 5),
        ("token_cap", 50000), ("wall_clock_seconds", 1800))}
    # NOTE: the "version" string below must be kept in sync with
    # plugin.yaml's version. It identifies the release, not a phase.
    status = {"ok": True, "plugin": "agent-pool", "version": "2.8.0",
              "relay_url": relay_url, "transport_mode": mode,
              "transport_privacy": (
                  "tor: onion-routed via Iroh Tor custom transport "
                  "(sidecar + tor daemon); peer learns no IP, no "
                  "relay in the connection path; both endpoints "
                  "must enable Tor; Iroh-over-Tor is the only byte "
                  "transport"),
              "pool_transports": per_pool,
              "budgets": budgets,
              # Budgets belong to agents, not pools: max_tasks_per_day
              # is the default per-agent task allocation
              # (agent_task_allocations overrides per agent, env
              # AGENT_POOL_TASK_ALLOCATIONS beats config); max_turns /
              # token_cap / wall_clock_seconds are optional secondary
              # limits enforced only on agent-reported usage
              # (pool_report_usage). There are no pool-level budgets.
              "agent_task_allocations": (
                  _cfg(ctx, "agent_task_allocations", {}) if ctx else {}),
              "budget_model": (
                  "agent-level task allocation: max_tasks_per_day is "
                  "the default per-agent allocation; "
                  "agent_task_allocations overrides per agent; "
                  "max_turns/token_cap/wall_clock_seconds are optional "
                  "secondary limits enforced only on agent-reported "
                  "usage (pool_report_usage); no pool-level budgets"),
              "ledger": "ready", "identity": "ready",
              "queued_notices": []}
    if legacy_relay:
        status["migration_warning"] = (
            "ignored legacy relay transport config for: "
            + ", ".join(legacy_relay)
            + " — treated as tor; remove transport_mode=relay / "
            "relay_url from config (Iroh-over-Tor is the only byte "
            "transport).")
    if env_relay:
        status["transport_warning"] = (
            (status.get("transport_warning", "") + " ")
            .strip()
            + f"HERMES_IROH_RELAY={env_relay} is ignored: no relay "
            "flags are emitted; Iroh-over-Tor is the only byte "
            "transport.").strip()
    try:
        svc = _service_for(ctx)
        # Read-only: peek, never drain (draining advances the ledger
        # cursor, which would make this path a write).
        try:
            status["queued_notices"] = svc.peek_notices()
        except Exception:
            status["queued_notices"] = []
        # Discovery: what is awaiting owner approval on both
        # ingestion paths (submitted versions), so the agent can
        # tell the operator without any approval slash command.
        try:
            status["awaiting_approval"] = svc.awaiting_approval()[
                "awaiting_approval"]
        except Exception:
            status["awaiting_approval"] = []
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
    try:
        status["tor"] = _tor_status_block(ctx)
    except Exception:
        pass
    return status


def _pool_status(args=None, **kwargs):
    """Report plugin config + ledger/identity readiness. Always returns JSON."""
    ctx = kwargs.get("ctx")
    try:
        status = _status_snapshot(ctx)
        # Tor is the only transport: always surface readiness.
        tor = status.get("tor") or _tor_status_block(ctx)
        status["tor"] = tor
        if isinstance(tor.get("ready"), dict) and tor["ready"].get("ok") is False and tor["ready"].get("error"):
            status["transport_warning"] = (
                f"Tor anonymity layer is not ready: {str(tor['ready'].get('error'))[:200]} — "
                "byte transfers fail closed; Iroh-over-Tor is the only byte "
                "transport (no relay fallback). Start the unit named in the "
                "error (systemctl start agent-pool-tor) and install the tor "
                "sidecar (scripts/install-tor.sh).")
        return json.dumps(status)
    except Exception as exc:  # never raise from a handler
        return json.dumps({"ok": False, "code": "status_unavailable",
                           "error_code": "status_unavailable",
                           "error": str(exc),
                           "fix": "check pool_status again after the Tor "
                                 "unit is running (systemctl start "
                                 "agent-pool-tor); byte transfers fail "
                                 "closed until tor_ready() passes"})


# ---------------------------------------------------------------------------
# Service construction (lazy: no DB/identity work at register() time)
# ---------------------------------------------------------------------------

# Exact remediation for a missing declared Python dependency. Python deps
# are installed by Hermes into the gateway interpreter's environment, never
# by the plugin: the plugin must not run its own package installer (it
# would target the wrong interpreter and violates the fast/offline
# registration contract). Fail closed naming the Hermes commands.
DEP_REMEDIATION = (
    "install the declared Python dependencies into the gateway "
    "interpreter's environment with Hermes: "
    "`hermes plugins validate <plugin-path> --install-deps` or "
    "`hermes pm install`, then restart the gateway. System services "
    "(ClamAV, Tor daemon, tor-enabled sidecar) are provisioned separately "
    "with `bash scripts/install-deps.sh` (system services only)."
)


def _missing_dep_error(exc) -> RuntimeError:
    """Fail-closed error for a missing declared Python dependency."""
    return RuntimeError(
        f"agent-pool python dependency unavailable ({exc}); "
        f"{DEP_REMEDIATION}")


def _whitelist_user_ids(ctx):
    """Resolve the pool whitelist (Phase 1): env beats config.

    Source order: ``AGENT_POOL_ALLOWED_USERS`` (comma-separated Discord
    user ids) when non-empty, else the ``allowed_users`` config key (list,
    or a comma-separated string). The pool owner is NOT added here —
    ``L.sync_whitelist`` always ensures the owner. Empty result means the
    owner is the only authorized user.
    """
    env = _env_get("AGENT_POOL_ALLOWED_USERS", "")
    if env.strip():
        return [u.strip() for u in str(env).split(",") if u.strip()]
    cfg = _cfg(ctx, "allowed_users", []) if ctx else []
    if isinstance(cfg, str):
        return [u.strip() for u in cfg.split(",") if u.strip()]
    if isinstance(cfg, (list, tuple, set)):
        return [str(u).strip() for u in cfg if str(u).strip()]
    return []


def _maintainer_discord_user_id(ctx):
    """Resolve maintainer Discord id: env beats config.

    Source order: ``AGENT_POOL_MAINTAINER_DISCORD_USER_ID`` when
    non-empty, else the ``maintainer_discord_user_id`` config key.
    """
    env = _env_get("AGENT_POOL_MAINTAINER_DISCORD_USER_ID", "")
    if env.strip():
        return env.strip()
    cfg = _cfg(ctx, "maintainer_discord_user_id", "") if ctx else ""
    return str(cfg).strip() if cfg else ""


def _maintainer_bot_discord_user_id(ctx):
    """Resolve maintainer BOT Discord id: env beats config.

    Source order: ``AGENT_POOL_MAINTAINER_BOT_DISCORD_USER_ID`` when
    non-empty, else the ``maintainer_bot_discord_user_id`` config key.
    Configuration resolution only — registration hard-fails when empty.
    """
    env = _env_get("AGENT_POOL_MAINTAINER_BOT_DISCORD_USER_ID", "")
    if env.strip():
        return env.strip()
    cfg = _cfg(ctx, "maintainer_bot_discord_user_id", "") if ctx else ""
    return str(cfg).strip() if cfg else ""


def _service_for(ctx, source=None):
    """Build a PoolService from the plugin context. Raises with a clear msg."""
    try:
        from . import ledger as L
        from .identity import ensure_identity
        from .tickets import PoolService
        from .transport import FakeTransport, RealSendHermesTransport
    except ModuleNotFoundError as exc:
        # A *declared Python dependency* (nacl/blake3) missing from the
        # gateway interpreter: fail closed naming the Hermes install
        # commands. Never self-install. (A ModuleNotFoundError naming the
        # plugin's own package means the relative import itself failed —
        # fall through to the flat import below.)
        if exc.name and exc.name.split(".")[0] in ("nacl", "blake3"):
            raise _missing_dep_error(exc) from exc
        import ledger as L
        from identity import ensure_identity
        from tickets import PoolService
        from transport import FakeTransport, RealSendHermesTransport
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
    mode, _relay_url, _per_pool = _transport_config(ctx)
    control, socks, tor_binary = _tor_config(ctx)
    if transport_name == "fake":
        # Test-only backend: exempt from the tor gate (documented).
        transport = FakeTransport(transport_mode=mode)
    else:
        # Hard gate at service construction: tor_ready() must pass
        # before any real transport is built. Raises TorNotReady
        # naming the unit to start — fail closed, no fallback.
        try:
            from .transport import tor_ready as _gate_tor_ready
        except ImportError:
            from transport import tor_ready as _gate_tor_ready
        _gate_tor_ready(control_port=control, socks_port=socks,
                        binary=tor_binary)
        transport = RealSendHermesTransport(
            transport_mode=mode,
            tor_control_port=control, tor_socks_port=socks,
            tor_binary=tor_binary)
    overrides: dict = {}
    budgets = {k: _cfg(ctx, k, d) if ctx else d for k, d in (
        ("max_turns", 25), ("max_tasks_per_day", 5),
        ("token_cap", 50000), ("wall_clock_seconds", 1800))}
    pool_resolver = (lambda: source) if source is not None else None
    agent_task_allocations = _cfg(ctx, "agent_task_allocations", {}) if ctx else {}
    if not isinstance(agent_task_allocations, dict):
        agent_task_allocations = {}
    return PoolService(conn, transport, caller_key_id=identity.key_id,
                       caller_signing_key=identity.signing_key,
                       caller_box_sk=identity.box_sk, budget_defaults=budgets,
                       pool_resolver=pool_resolver,
                       transport_overrides=overrides,
                       agent_task_allocations=agent_task_allocations,
                       maintainer_discord_user_id=_maintainer_discord_user_id(ctx),
                       maintainer_bot_discord_user_id=_maintainer_bot_discord_user_id(ctx),
                       clamd_socket=_cfg(ctx, "clamd_socket", "/run/clamav/clamd.ctl") if ctx else "/run/clamav/clamd.ctl",
                       auto_provision=_cfg(ctx, "auto_provision", True) if ctx else True)


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


# Only Tor failures get the Tor-readiness fix string; stamping every
# exception with it misdiagnoses unrelated errors (e.g. a ledger
# FOREIGN KEY IntegrityError) as Tor outages.
_TOR_FIX = ("check pool_status for Tor readiness; if Tor is "
            "down, start the agent-pool-tor unit and retry")
_GENERIC_FIX = "see the error above; retry, or check pool_status if it persists"


def _is_tor_error(exc) -> bool:
    import re
    text = " ".join([type(exc).__name__, str(exc)]).lower()
    return bool(re.search(r"\btor\b", text)) or "onion" in text \
        or "agent-pool-tor" in text


def _error(exc) -> str:
    code = getattr(exc, "error_code", None) or "error"
    detail = getattr(exc, "detail", None) or str(exc)
    payload = {"ok": False, "code": code, "error": str(exc),
               "fix": _TOR_FIX if _is_tor_error(exc) else _GENERIC_FIX}
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
                                    source=source,
                                    maintainer_discord_user_id=getattr(svc, "_maintainer_discord_user_id", ""),
                                    maintainer_bot_discord_user_id=getattr(svc, "_maintainer_bot_discord_user_id", ""))
        elif sub == "members":
            pool_id = rest[0] if rest else opts.get("pool_id", "")
            if "enroll" in opts:
                # /pool members --enroll <key_id> <box_hex> <verify_hex>
                # --discord-user <id>  (owner-only; public key material,
                # safe over Discord). The Discord id is REQUIRED and is
                # bound to the enrolled key at enrollment — no inference.
                key_id = opts.get("enroll", "")
                box_hex = rest[0] if len(rest) > 0 else opts.get("box", "")
                verify_hex = rest[1] if len(rest) > 1 else opts.get("verify", "")
                discord_user_id = opts.get("discord_user", "")
                if not key_id or not box_hex or not verify_hex or not discord_user_id:
                    return _result({"ok": False,
                                    "error": "usage: /pool members --enroll "
                                             "<key_id> <box_pubkey_hex> "
                                             "<verify_pubkey_hex> "
                                             "--discord-user <discord_user_id>"})
                out = svc.enroll_member(
                    opts.get("pool") or opts.get("pool_id") or None,
                    key_id, box_hex, verify_hex,
                    discord_user_id=discord_user_id, source=source)
            elif "mykeys" in flags or "mykeys" in opts:
                # Any gateway: print this gateway's public key material
                # for enrollment on the pool maintainer.
                out = svc.my_keys()
            else:
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
    """``/add-repo`` has three modes:

    - ``/add-repo <repo_id> --path <abs/path> [--pool <pool_id>] [--name <display name>]``
      — the owner-only fast path: register the project and submit its
      bytes as the next version (self-accepting). --path is required.
    - ``/add-repo --deliver <transfer_ticket> --manifest <json> [--repo <repo_id>]``
      — the link flow: fetch the contributor's bytes over Iroh-over-Tor,
      verify signature/ACL/BLAKE3, audit, and record the version as
      submitted (awaiting approval). Bytes move only over the transport.
    - ``/add-repo approve <repo_id> [--version N]`` — owner-only promotion
      of a submitted version to accepted (DEK rotation + chain append).
    - ``/add-repo submissions [--repo <repo_id>]`` — the approval queue.
    """
    try:
        pos, opts, _flags = _parse_args(raw)
        if "link" in opts or "link" in _flags:
            raise ValueError(
                "add-repo: --link is not supported; use /add-repo --path <checkout>"
            )
        svc = _service_for(ctx, source=source)
        pool_id = opts.get("pool", "") or opts.get("pool_id", "")
        sub = pos[0] if pos else ""
        if opts.get("deliver") is not None:
            transfer_ticket = opts.get("deliver") or (pos[0] if pos and pos[0] != "deliver" else "")
            if not transfer_ticket:
                return _result({"ok": False,
                                "error": "usage: /add-repo --deliver <transfer_ticket> "
                                         "--manifest <json> [--repo <repo_id>]"})
            if not opts.get("manifest"):
                return _result({"ok": False,
                                "error": "add-repo deliver needs --manifest <json> "
                                         "(the contributor-signed submitted manifest)"})
            try:
                manifest = json.loads(opts["manifest"])
            except Exception as exc:
                return _result({"ok": False, "error": f"bad manifest JSON: {exc}"})
            out = svc.add_repo_deliver(transfer_ticket, manifest,
                                       pool_id=pool_id or None,
                                       source=source,
                                       repo_id=opts.get("repo") or None)
            return _result(out, svc, source)
        if sub == "approve":
            repo_id = pos[1] if len(pos) > 1 else opts.get("repo", "")
            if not repo_id:
                return _result({"ok": False,
                                "error": "usage: /add-repo approve <repo_id> [--version N]"})
            version = opts.get("version")
            out = svc.add_repo_approve(repo_id,
                                       version=int(version) if version else None,
                                       pool_id=pool_id or None, source=source)
            return _result(out, svc, source)
        if sub == "submissions":
            out = svc.add_repo_submissions(repo_id=opts.get("repo") or None,
                                           pool_id=pool_id or None, source=source)
            return _result(out, svc, source)
        repo_id = pos[0] if pos else opts.get("repo", "")
        path = opts.get("path", "")
        if not repo_id and path:
            repo_id = _slugify_path(path)
        out = svc.add_repo(pool_id, repo_id, path,
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
    ``/fetch repo <repo_id>`` / ``/fetch repo --manifest <json> [--dest DIR] [--ticket FRESH]``."""
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
                out = svc.fetch_repo_download(manifest, dest=opts.get("dest", "/tmp/fetch"),
                                               transfer_ticket=opts.get("ticket") or None,
                                               source=source)
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
    <grant> --manifest <json>`` (the pool gateway audits the pushed bundle
    and records the version as submitted, awaiting owner approval;
    acceptance and completion follow the agent-driven approval path —
    ``scripts/pool-approve.py --ticket <id>``). The bundle itself is built
    and sent by the contributor's own gateway (``push_build_bundle``);
    only the ticket + grant + signed manifest cross to the pool gateway."""
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
            return _result({"ok": False, "error": "usage: /audit show <ticket> | /audit show --repo <repo_id>"})
        if opts.get("repo"):
            # Repo form: latest audit row for the repo (both ingestion
            # paths write audits keyed by repo/version).
            return _result(svc.audit_show(repo_id=opts["repo"]), svc, source)
        ticket_id = pos[1] if len(pos) > 1 else opts.get("ticket", "")
        return _result(svc.audit_show(ticket_id=ticket_id), svc, source)
    except Exception as exc:
        return _error(exc)


def _pane_state_file():
    """Path of the agent→pane sidecar (profile-aware plugin data dir)."""
    try:
        from . import ledger as _L
    except ImportError:
        import ledger as _L
    return Path(_L._plugin_base_dir()) / "pane-state.json"


def _pane_state_read():
    """Read the sidecar; never raises. Empty state on any problem."""
    try:
        path = _pane_state_file()
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("sidecar is not a JSON object")
        data.setdefault("ops", [])
        data.setdefault("pool_status", {})
        return data
    except Exception:
        return {"ops": [], "pool_status": {}, "updated_at": None}


def _pane_read_conn():
    """PANE read path: ALWAYS the maintainer ledger (default profile's
    plugin data dir), read-only. Override with AGENT_POOL_PANE_LEDGER.

    Never writable; a contributor-profile dashboard cannot mutate the
    maintainer ledger. Never raises — callers handle None.
    """
    p = _env_get("AGENT_POOL_PANE_LEDGER") or \
        str(Path.home() / ".hermes" / "plugin-data" / "agent-pool" / "data.db")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _pane_ledger_path():
    return _env_get("AGENT_POOL_PANE_LEDGER") or \
        str(Path.home() / ".hermes" / "plugin-data" / "agent-pool" / "data.db")


def _pane_worktrees():
    """Enumerate work/integrate/<repo_id>/ under the pane ledger's base dir:
    worktree-v<N> dirs and bundle-v<N>.git per repo. Pure filesystem listing
    of the plugin's own records. Never raises."""
    try:
        base = Path(_pane_ledger_path()).parent / "work" / "integrate"
        out = []
        if not base.is_dir():
            return out
        for repo_dir in sorted(base.iterdir()):
            if not repo_dir.is_dir():
                continue
            if repo_dir.name.startswith("branch-t-"):
                # stale remnants of the deleted integration gate (ec7e9c5)
                continue
            bundles = sorted(p.name for p in repo_dir.glob("bundle-v*.git") if p.is_dir() or p.is_file())
            worktrees = sorted(p.name for p in repo_dir.glob("worktree-v*") if p.is_dir())
            out.append({"repo_id": repo_dir.name, "bundles": bundles, "worktrees": worktrees})
        return out
    except Exception:
        return []


def _pane_lists_snapshot(conn=None):
    """Read repos + tasks from the local ledger for the pane sidecar.

    Mirrors the shapes produced by ``fetch_repo_list`` / ``fetch_task_list``
    but performs no authz/freeze checks and never raises — the pane is a
    read-only dashboard, not a command path. When ``conn`` is None, reads
    the maintainer ledger via ``_pane_read_conn()``. On any problem returns
    ``{"repos": [], "tasks": [], "lists_error": str(exc)[:200]}``; zero pools
    with no error adds a ``lists_warning``.
    """
    own_conn = False
    try:
        try:
            from . import ledger as L
        except ImportError:
            import ledger as L
        if conn is None:
            conn = _pane_read_conn()
            own_conn = True
        pools = L.list_local_pools(conn)
        repos, tasks = [], []
        for pool_id in pools:
            for r in L.list_repos(conn, pool_id):
                available = [dict(t) for t in L.list_tickets(
                    conn, pool_id=pool_id, state="open", repo_id=r["repo_id"])]
                repos.append({
                    "repo_id": r["repo_id"], "pool_id": r["pool_id"],
                    "display_name": r["display_name"], "name": r["name"],
                    "accepted_version": r["accepted_version"],
                    "available_tasks": [
                        {"ticket_id": t["ticket_id"], "title": t["title"],
                         "claimed": False,
                         "claimant_key_id": t.get("claimant_key_id")}
                        for t in available],
                    "open_tickets": r["open_tickets"],
                    "claimed_tickets": r["claimed_tickets"],
                    "total_tickets": r["total_tickets"],
                })
            rows = [dict(t) for t in L.list_tickets(conn, pool_id=pool_id, state="open")]
            rows += [dict(t) for t in L.list_tickets(conn, pool_id=pool_id, state="claimed")]
            rows += [dict(t) for t in L.list_tickets(conn, pool_id=pool_id, state="completed")]
            tasks += [{"ticket_id": t["ticket_id"], "title": t["title"],
                       "repo_id": t["repo_id"], "state": t["state"],
                       "claimed": t["state"] == "claimed",
                       "claimant_key_id": t.get("claimant_key_id"),
                       "pool_id": pool_id}
                      for t in rows]
        result = {"repos": repos, "tasks": tasks,
                  "worktrees": _pane_worktrees()}
        if not pools:
            result["lists_warning"] = "no pools visible in the pane ledger"
        return result
    except Exception as exc:
        logger.warning("pane lists snapshot failed: %s", exc)
        return {"repos": [], "tasks": [], "worktrees": [],
                "lists_error": str(exc)[:200]}
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass


def _pane_state_computed():
    """Live computed pane state: ops log from the sidecar (agent-written,
    historical); every piece of STATE computed fresh per request. Never
    raises."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    iso = now.isoformat()
    at_ms = int(now.timestamp() * 1000)
    sidecar = _pane_state_read()
    try:
        pool_status = _status_snapshot(None)
    except Exception as exc:
        pool_status = {"ok": False, "error": str(exc)[:200]}
    try:
        pool_status["tor"] = _tor_status_block(None)
    except Exception:
        pass
    state = {"ops": sidecar.get("ops", []),
             "pool_status": pool_status,
             "updated_at": iso,
             "at": at_ms}
    state.update(_pane_lists_snapshot())
    return state


_POOL_REPORT_USAGE_SCHEMA = {
    "name": "pool_report_usage",
    "description": ("Record agent-reported usage (turns/tokens/seconds) on a "
                    "pool claim. The plugin records what the agent reports; "
                    "exhaustion is checked separately and never auto-releases."),
    "parameters": {"type": "object", "properties": {
        "claim_id": {"type": "string"},
        "turns": {"type": "integer", "default": 0},
        "tokens": {"type": "integer", "default": 0},
        "seconds": {"type": "integer", "default": 0}}, "required": ["claim_id"]},
}


def _pool_report_usage(args=None, **kwargs):
    """Agent-facing usage reporting tool (next to pool_status; no slash command)."""
    ctx = kwargs.get("ctx")
    args = args or {}
    try:
        svc = _service_for(ctx)
        claim_id = str(args.get("claim_id", "") or "").strip()
        if not claim_id:
            return json.dumps({"ok": False, "code": "missing_argument",
                               "error_code": "missing_argument",
                               "error": "pool_report_usage needs claim_id",
                               "fix": "pass claim_id from the /claim result"})
        row = svc.report_usage(
            claim_id, turns=int(args.get("turns", 0) or 0),
            tokens=int(args.get("tokens", 0) or 0),
            seconds=int(args.get("seconds", 0) or 0))
        return json.dumps({"ok": True, "claim_id": claim_id,
                           "turns_used": row.get("turns_used", 0),
                           "tokens_used": row.get("tokens_used", 0),
                           "wall_seconds_used": row.get("wall_seconds_used", 0)})
    except Exception as exc:  # never raise from a handler
        code = getattr(exc, "error_code", None) or "error"
        return json.dumps({"ok": False, "code": code,
                           "error_code": code,
                           "error": str(exc),
                           "fix": "pass a valid claim_id from an active "
                                 "claim; check pool_status for Tor "
                                 "readiness if the claim lookup failed"})


_POOL_PANE_REPORT_SCHEMA = {
    "name": "pool_pane_report",
    "description": ("Record an operation outcome for the Agent Pool desktop "
                    "pane. Upserts the op into the pane-state sidecar; the "
                    "pane polls it. Writes ONLY the pane-state sidecar — "
                    "never the ledger."),
    "parameters": {"type": "object", "properties": {
        "op_id": {"type": "string"},
        "kind": {"type": "string"},
        "status": {"type": "string"},
        "detail": {"type": "string", "default": ""}},
        "required": ["op_id", "kind", "status"]},
}


def _pool_pane_report(args=None, **kwargs):
    """Agent→pane state bridge. Writes only the pane-state sidecar."""
    ctx = kwargs.get("ctx")
    args = args or {}
    try:
        import datetime
        op_id = str(args.get("op_id", "") or "").strip()
        kind = str(args.get("kind", "") or "").strip()
        status = str(args.get("status", "") or "").strip()
        detail = str(args.get("detail", "") or "")
        if not op_id or not kind or not status:
            return json.dumps({"ok": False, "code": "missing_argument",
                               "error_code": "missing_argument",
                               "error": "pool_pane_report needs op_id, kind and status"})
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        state = _pane_state_read()
        ops = [o for o in state.get("ops", []) if isinstance(o, dict)
               and o.get("op_id") != op_id]
        ops.insert(0, {"op_id": op_id, "kind": kind, "status": status,
                       "detail": detail, "updated_at": now})
        ops = ops[:200]
        try:
            pool_status = _status_snapshot(ctx)
        except Exception:
            pool_status = state.get("pool_status") or {}
        lists = _pane_lists_snapshot()
        new_state = {"ops": ops, "pool_status": pool_status,
                     "updated_at": now,
                     "at": int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)}
        new_state.update(lists)
        path = _pane_state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(new_state))
        return json.dumps({"ok": True, "op_id": op_id, "ops": len(ops),
                           "updated_at": now})
    except Exception as exc:  # never raise from a handler
        return json.dumps({"ok": False, "code": "pane_state_unavailable",
                           "error_code": "pane_state_unavailable",
                           "error": str(exc)[:300]})


_POOL_STATUS_SCHEMA = {
    "name": "pool_status",
    "description": ("Report Agent Pool plugin status: tor transport, agent-level "
                    "task allocation and optional reported-usage limits, "
                    "and ledger/identity readiness."),
    "parameters": {"type": "object", "properties": {}},
}

_COMMANDS = (
    ("pool", _handle_pool, "Agent Pool: register|members [--enroll <key> <box> <verify> | --mykeys]|remove|freeze|list, status",
     "register|members|remove|freeze|list [options]"),
    ("fetch", _handle_fetch, "Fetch repo list (projects + available tasks + gateway addressing), task list (with claimed status), or one repo manifest / download form. Discord carries only paths and manifest JSON — the agent on the gateway performs the download",
     "repo list | task list [--ticket <id>] | repo <repo_id> | repo --manifest <json> [options]"),
    ("task", _handle_task, "Agent Pool tickets: add|list|show",
     "add|list|show [options]"),
    ("add-repo", _handle_add_repo, "Register a project and submit its repo bytes as a new version (owner fast path --path; link flow --deliver; approve; submissions)",
     "<repo_id> --path <abs/path> | --deliver <transfer_ticket> --manifest <json> [--repo <repo_id>] | approve <repo_id> [--version N] | submissions [--repo <repo_id>] [options]"),
    ("task_add", _handle_task_add, "Create a ticket for a registered project (alias of /task add)",
     "--repo <repo_id> --title \"...\" [--brief \"...\"] [--pool <pool_id>]"),
    ("task_list", _handle_task_list, "List tickets, optionally for one project (alias of /task list)",
     "[--repo <repo_id>] [--pool <pool_id>] [--state open]"),
    ("claim", _handle_claim, "Claim a ticket (paths optional: omit --paths for a whole-ticket claim) or take up to N open tickets with --any-project; the reply carries the accepted repo's Tor download link (transfer_ticket) and manifest directly",
     "[ticket] [--paths <paths>] [--any-project --max-tasks N] [options]"),
    ("release", _handle_release, "Release a claimed ticket back to open",
     "[ticket]"),
    ("push", _handle_push, "Push a diff for a claimed ticket: issue the push link (grant), or deliver a pushed bundle (audits and records the version as submitted, awaiting approval; acceptance and completion follow the agent-driven approval path). Pushes are never completed from Discord: the agent builds the bundle locally (push_build_bundle), and Discord carries only the manifest JSON + transfer ticket + grant",
     "repo <repo_id> --ticket <id> | repo --deliver <transfer_ticket> --token <grant> --manifest <json>"),
    ("audit", _handle_audit, "Show the audit report for a ticket or a repo",
     "show [ticket] | show --repo <repo_id>"),
)


def _wrap_command(handler, ctx):
    """Dispatch seam: freeze guard + Discord-whitelist authz (2109 P1).

    Authorization is the whitelisted Discord user id only (owner or
    pool_authorized). Keys never decide access.
    """
    def wrapped(raw="", *, source=None, session_id="", session_key="", platform=""):
        try:
            from . import ledger as _L
        except ImportError:
            try:
                import ledger as _L
            except Exception:
                _L = None
        if _L is None:
            # Ledger unavailable: fail closed — never run the handler
            # without the freeze/whitelist guard.
            return _result({"ok": False, "error_code": "guard_unavailable",
                            "code": "guard_unavailable",
                            "error": "pool guard unavailable: ledger could "
                                     "not be loaded; refusing to run the "
                                     "command without freeze/whitelist "
                                     "checks",
                            "fix": "check pool_status / plugin-data "
                                   "readiness and retry"})
        if _L is not None:
            try:
                svc = _service_for(ctx, source=source)
                pos, opts, flags = _parse_args(raw or "")
                sub = pos[0] if pos else ""
                # Freeze control is owner-only (see _require_owner below);
                # unfreeze runs while frozen, freeze/unfreeze never bypass authz.
                is_freeze_ctl = (handler.__name__ == "_handle_pool" and sub == "freeze")
                # Look up the hosted pool best-effort (or None).
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
                    except Exception as exc:
                        # Freeze state could not be determined: fail
                        # closed via the outer guard (never run unfrozen).
                        raise RuntimeError(
                            f"freeze check unavailable: {exc}") from exc
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
                            elif handler.__name__ == "_handle_pool" and sub == "members" and "enroll" in opts:
                                _owner_only = True
                            elif handler.__name__ == "_handle_task" and sub in ("add",):
                                _owner_only = True
                            elif handler.__name__ == "_handle_task_add":
                                _owner_only = True
                            elif handler.__name__ == "_handle_add_repo":
                                # --deliver is not owner-only: the
                                # maintainer-gateway identity gate inside
                                # add_repo_deliver is the real authorization.
                                # approve/submissions/--path stay owner-only.
                                _owner_only = not (
                                    opts.get("deliver") is not None)
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
            except Exception as exc:
                # Fail closed: a guard failure (service/pool resolution,
                # DB error, Tor-gate refusal) must never let the handler
                # run without freeze + whitelist checks.
                return _result({"ok": False, "error_code": "guard_unavailable",
                                "code": "guard_unavailable",
                                "error": f"pool guard unavailable: {exc}",
                                "fix": "check pool_status for Tor/ledger "
                                       "readiness; if Tor is down, start "
                                       "the agent-pool-tor unit and retry"})
        return handler(ctx, raw, source=source, session_id=session_id,
                       session_key=session_key, platform=platform)
    return wrapped


def _register_tool_compat(ctx, **kwargs):
    """Call ``ctx.register_tool`` with only kwargs its signature accepts.

    Filters via ``inspect.signature`` so an unsupported kwarg on a
    future host cannot turn into a TypeError that drops all tools.
    """
    import inspect

    try:
        params = inspect.signature(ctx.register_tool).parameters
    except (TypeError, ValueError):
        params = {}
    if params and not any(p.kind == inspect.Parameter.VAR_KEYWORD
                          for p in params.values()):
        kwargs = {k: v for k, v in kwargs.items() if k in params}
    return ctx.register_tool(**kwargs)


def register(ctx):
    """Wire the full gateway-native surface.

    Registration is pure wiring (fast, offline): it must load with
    all tools/commands even when the Tor sidecar is absent. The Tor
    gate is lazy — ``_service_for`` (and the transport's per-transfer
    ``_require_tor_ready``) fails closed at first use / actual byte
    transfer, and ``pool_status.tor.ready`` surfaces readiness.
    """
    try:
        _register_tool_compat(
            ctx, name="pool_status", toolset="agent_pool",
            schema=_POOL_STATUS_SCHEMA,
            handler=lambda args=None, **kw: _pool_status(args, ctx=ctx, **kw))
        _register_tool_compat(
            ctx, name="pool_report_usage", toolset="agent_pool",
            schema=_POOL_REPORT_USAGE_SCHEMA,
            handler=lambda args=None, **kw: _pool_report_usage(args, ctx=ctx, **kw))
        _register_tool_compat(
            ctx, name="pool_pane_report", toolset="agent_pool",
            schema=_POOL_PANE_REPORT_SCHEMA,
            handler=lambda args=None, **kw: _pool_pane_report(args, ctx=ctx, **kw))
    except Exception as exc:
        logger.warning("agent-pool: tool registration failed: %s", exc)
    for name, handler, description, args_hint in _COMMANDS:
        try:
            ctx.register_command(name, _wrap_command(handler, ctx),
                                 description=description, args_hint=args_hint,
                                 argument_mode="text")
        except Exception as exc:
            logger.warning("agent-pool: command %s registration failed: %s", name, exc)
