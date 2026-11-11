"""Agent Pool — read-only dashboard/desktop backend.

READ-ONLY CONTRACT: every route in this module is GET and serves
plugin-owned state only. No route writes the ledger, the plugin data
dir, or any other state; there are no POST/PUT/PATCH/DELETE handlers.
The pane is passive UI: all mutations go through the agent (slash
commands / tools), never through this router. ``/pane-state`` and
``/status`` include a cached (~60s) localhost Tor readiness probe —
a local socket check by the plugin itself, not a gateway round-trip.

State shared with the agent side flows exclusively through the
``pane-state.json`` sidecar under ``plugin_data_dir("agent-pool")``
(written by the ``pool_pane_report`` tool) and Hermes ``plugin_db``
reads — never through in-process globals.
"""
import importlib.util
import json
import sys
from pathlib import Path

from fastapi import APIRouter

router = APIRouter()

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _load_plugin_module():
    """Load the plugin's __init__.py by path (the dashboard mounts this
    file standalone, not as part of the gateway's plugin package)."""
    root = str(_PLUGIN_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    spec = importlib.util.spec_from_file_location(
        "agent_pool_plugin", _PLUGIN_ROOT / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@router.get("/status")
def status():
    """The same read path pool_status uses (service/plugin_db reads)."""
    try:
        mod = _load_plugin_module()
        return mod._status_snapshot(None)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


@router.get("/pane-state")
def pane_state():
    """Live computed pane state (read-only): ops log from the sidecar,
    everything else computed fresh — maintainer ledger (mode=ro),
    repo_versions manifests, work/integrate bundles/worktrees, local Tor
    probe. Falls back to the raw sidecar with an ``error`` key if the
    computed path raises."""
    try:
        mod = _load_plugin_module()
        try:
            return mod._pane_state_computed()
        except Exception as exc:
            fallback = mod._pane_state_read()
            fallback["error"] = str(exc)[:300]
            return fallback
    except Exception as exc:
        return {"ops": [], "pool_status": {}, "updated_at": None,
                "error": str(exc)[:300]}
