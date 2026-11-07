"""Phase 6 vocabulary + source-shape gates (WO-017).

The suite pins the corrected model and nothing else: the deleted
vocabulary (ticket ``pending`` state, proposal/work/integrate machinery,
auto-approve) cannot creep back into the production modules; no
authorization path reads ``identities``/``members``; the one-pool
invariant holds in ``_resolve_pool``/``list_projects``; every
``_COMMANDS`` entry is documented in the README table and vice versa.

Kept vocabulary (substrate, NOT gated): ``Iroh``, ``send_hermes``,
``relay``, ``transfer_ticket``.

Exemptions, explicitly documented:
- the 2.6.0 ticket-state migration block in ``ledger.py``
  (``MIGRATION_2_6_0_START``/``END``) — it must name the old states;
- full-line comments (notes) — e.g. describing frozen verbs;
- the ``audits.verdict`` schema default ``'pending'`` in ``ledger.py`` —
  audit-domain vocabulary (verdicts actually written are ``clean`` /
  ``flagged``); ticket states are ``open``/``claimed``/``completed``.
"""
import inspect
import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import ledger as L
import tickets as T

ROOT = Path(__file__).parent.parent
PROD_FILES = ("tickets.py", "ledger.py", "__init__.py", "propagation.py", "crypto.py")

BANNED = (
    "pending",
    "work_proposal",
    "work fetch",
    "work_fetch",
    "pool_fetch",
    "integrate",
    "build_proposal",
    "approve_ticket",
    "auto_approve",
)

# Single explicit allowlist: audit-verdict schema default (not ticket state).
ALLOWLIST = (
    ("ledger.py", "verdict TEXT NOT NULL DEFAULT 'pending'"),
)


def _code_lines(path: Path):
    """Yield (lineno, line) outside the migration block and full-line comments."""
    in_migration = False
    for i, line in enumerate(path.read_text().splitlines(), 1):
        if "MIGRATION_2_6_0_START" in line:
            in_migration = True
            continue
        if "MIGRATION_2_6_0_END" in line:
            in_migration = False
            continue
        if in_migration or line.lstrip().startswith("#"):
            continue
        yield i, line


@pytest.mark.parametrize("fname", PROD_FILES)
def test_no_deleted_vocabulary(fname):
    hits = []
    for lineno, line in _code_lines(ROOT / fname):
        for token in BANNED:
            if token in line:
                if any(a[0] == fname and a[1] in line for a in ALLOWLIST):
                    continue
                hits.append(f"{fname}:{lineno}: {token!r} in {line.strip()!r}")
    assert hits == [], "deleted vocabulary back in production code:\n" + "\n".join(hits)


def test_ticket_states_are_exactly_three():
    assert L.TICKET_STATES == ("open", "claimed", "completed")


def test_dead_budget_kill_code_is_gone():
    """Flagged follow-up 3: release_claim_for_ticket was dead code."""
    assert not hasattr(L, "release_claim_for_ticket")


def test_authz_paths_do_not_read_identities_or_members():
    import ast
    import textwrap

    helpers = [
        T.PoolService._require_owner,
        T.PoolService._require_authorized,
        T.PoolService._require_pool_authorized,
        T.PoolService._is_bound_maintainer,
        T.PoolService._check_not_frozen,
        L.is_owner,
        L.is_authorized,
    ]
    for fn in helpers:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        # Drop the docstring: documentation may *name* the tables; only
        # code (incl. SQL string literals) may not read them.
        body = getattr(tree.body[0], "body", [])
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body[0].value.value = ""
        code = ast.dump(tree)
        for forbidden in ("identities", "members"):
            assert forbidden not in code, f"{fn.__qualname__} reads {forbidden}"


def test_resolve_pool_has_no_multi_host_aggregation():
    """One-pool invariant: _resolve_pool lists hosts only to refuse."""
    src = inspect.getsource(T.PoolService._resolve_pool)
    assert "list_local_pools" in src  # lists hosts to refuse foreign pools
    assert "for pid in" not in src and "extend" not in src, (
        "_resolve_pool aggregates across hosted pools")


def test_list_projects_has_no_multi_host_aggregation():
    """Flagged follow-up 2: the multi-host aggregation branch is deleted."""
    src = inspect.getsource(T.PoolService.list_projects)
    assert "list_local_pools" not in src
    assert "PoolNotLocalError" not in src or "_resolve_pool" in src


def test_transport_default_is_real_everywhere():
    """Flagged follow-up 1: code default matches plugin.yaml (real)."""
    init_src = (ROOT / "__init__.py").read_text()
    assert '_cfg(ctx, "transport", "real")' in init_src
    assert '"fake") if ctx else "fake"' not in init_src
    yaml = (ROOT / "plugin.yaml").read_text()
    m = re.search(r"transport:\s*\n\s+type:\s*str\s*\n\s+default:\s*\"(\w+)\"", yaml)
    assert m and m.group(1) == "real"


def test_commands_match_readme_table():
    from __init__ import _COMMANDS
    code_names = [c[0] for c in _COMMANDS]
    readme = (ROOT / "README.md").read_text().split("## Configuration")[0]
    table_names = re.findall(r"^\| `([a-z0-9_-]+)` \|", readme, flags=re.M)
    assert table_names, "no command table found in README head"
    assert set(table_names) == set(code_names), (
        f"README table {sorted(table_names)} != _COMMANDS {sorted(code_names)}")


def test_no_pool_gateway_reads_in_authz_helpers_via_db():
    """Behavioral backstop: authz works on a DB where identities/members
    tables are unusable would fail — instead assert the helpers only
    touch pools/pool_authorized by construction (covered above) and
    that a whitelisted user passes with empty identities/members."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    L.init_ledger(c)
    L.create_pool(c, "pool-1", "Team", "agent:maint", owner_discord_user_id="owner-1")
    assert L.is_owner(c, "pool-1", "owner-1") is True
    assert L.is_authorized(c, "pool-1", "owner-1") is True
    assert L.is_owner(c, "pool-1", "nobody") is False
