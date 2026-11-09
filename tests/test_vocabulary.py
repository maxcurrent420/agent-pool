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
    # The operator's canonical pool checkout dir is literally
    # ``work/integrate/<repo>`` (live: work/integrate/canonical-demo);
    # resolve_pool_checkout_dir names that path, not the deleted
    # proposal/integrate machinery.
    ("propagation.py", "``work/integrate/<repo>``), else the default"),
    ("propagation.py", "``<plugin-data>/work/integrate/<repo_id>`` under the ledger DB's"),
    ("propagation.py", 'return default_db_path().parent / "work" / "integrate" / str(repo_id)'),
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


# --- WO-024 Phase 5 doc vocabulary gates ----------------------------------
# Docs must describe the current behaviour only: no onboarding command,
# no pool-configured budget keys, no stale reply-contract / throughput /
# phase / freeze / acceptance vocabulary, and no brittle line citations.

DOC_FILES = (
    "README.md",
    "docs/architecture.md",
    "docs/threat-model.md",
    "docs/fallback-transport.md",
    "scripts/live-discord-check.md",
)

DOC_BANNED = (
    r"\binvit\w*\b",                      # no onboarding/invitation command
    r"ctx\.post_message",                 # not part of the reply contract
    r"97\s*MB/s",                         # unverifiable throughput figure
    r"~2 MB/s|\b2 MB/s\b",               # unverifiable throughput figure
    r'"phase"\s*:\s*3',                   # pool_status emits a version string
    r"maintainer-signed",                 # acceptance is by the maintainer gateway
    r"per-pool overrides \(budgets|pools\.[^\s)]*\.budgets|per-pool budget",
    r"read-only queries still run|queries still run while frozen",
    r"\d+ tests passed\b",                # no pinned test count
    r"\.(?:py|md|yaml):\d+",              # no brittle file:line citations
    r"Discord-only push|Discord-only operator can finish|"
    r"can finish a push|can complete a push from Discord",
)


def _doc_texts():
    return {f: (ROOT / f).read_text() for f in DOC_FILES}


def test_docs_have_no_stale_vocabulary():
    hits = []
    for fname, text in _doc_texts().items():
        for pattern in DOC_BANNED:
            for m in re.finditer(pattern, text):
                hits.append(f"{fname}: {pattern!r} at {m.start()}")
    assert hits == [], "stale vocabulary back in docs:\n" + "\n".join(hits)


def test_docs_link_mentions_are_rejections_only():
    for fname, text in _doc_texts().items():
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if "--link" not in line:
                continue
            window = "\n".join(lines[max(0, i - 1): i + 3])
            assert ("not supported" in window or "rejected" in window), (
                f"{fname}:{i + 1}: --link mentioned without a rejection:\n{line}")


def test_docs_state_current_contracts():
    combined = "\n".join(_doc_texts().values())
    readme = _doc_texts()["README.md"]
    required = (
        "not authorized for pool",
        "claim refused: user",
        "register refused: maintainer must be the gateway owner",
        "gateway identity changed: caller is not the pool's owner gateway",
        "push deliver refused: only the pool maintainer gateway may accept",
        "add-repo deliver refused: only the pool maintainer gateway may accept",
        "agent budget exhausted:",
        "budget exhausted:",
        "fetch refused: manifest signature does not verify",
        "reply exceeds 4096-byte gateway limit",
        "add-repo: --link is not supported",
        "pool frozen — contact the maintainer",
        "release worktree",
        "AGENT_POOL_ALLOWED_USERS",
        "AGENT_POOL_TASK_ALLOCATIONS",
        "AGENT_POOL_WORKTREE_ROOT",
        "push_build_bundle",
        "worktree-<ticket_id>",
        "pool/<ticket_id>",
        "sync_whitelist",
        "never completed from Discord",
    )
    for needle in required:
        assert needle in combined, f"docs missing current contract: {needle!r}"
    assert "pool_report_usage" in readme
    assert "pool_status" in readme
    # Full chain verification is scoped to the hosted-pool fetch.
    assert "Full chain verification happens on the hosted-pool" in combined


# --- WO-025 gates: 2.7.0 release, agent-budget wording, catalog ----------

# ledger.py is excluded: its meta.state_migration == "2.6.0" is the
# historical ticket-state migration guard, asserted by
# tests/test_ledger.py + tests/test_migration.py. It names the migration,
# not the current release version. Those two test files are also excluded.
RELEASE_REF_FILES = (
    "README.md",
    "docs/architecture.md",
    "docs/threat-model.md",
    "docs/fallback-transport.md",
    "scripts/live-discord-check.md",
    "plugin.yaml",
    "plugin-catalog/agent-pool.yaml",
    "__init__.py",
    "scripts/demo.py",
)


def test_release_version_is_2_7_0_everywhere_except_migration_guard():
    for fname in RELEASE_REF_FILES:
        assert "2.6.0" not in (ROOT / fname).read_text(), (
            f"{fname} still references the 2.6.0 release version")
    assert (ROOT / "plugin.yaml").read_text().splitlines()[1].strip() == 'version: "2.7.0"'


def test_budget_wording_matches_agent_allocation_model():
    yaml = (ROOT / "plugin.yaml").read_text()
    for key in ("max_turns", "token_cap", "wall_clock_seconds"):
        m = re.search(
            rf"{key}:\s*\n\s+type:\s*int\s*\n\s+default:\s*\d+\s*\n"
            rf"\s+description: \"([^\"]+)\"", yaml)
        assert m, f"plugin.yaml: {key} description shape changed"
        assert "only when" in m.group(1), f"{key} must state its limit is conditional"
        assert "pool_report_usage" in m.group(1), f"{key} must name pool_report_usage"
    m = re.search(
        r"max_tasks_per_day:\s*\n\s+type:\s*int\s*\n\s+default:\s*\d+\s*\n"
        r"\s+description: \"([^\"]+)\"", yaml)
    assert m, "plugin.yaml: max_tasks_per_day description shape changed"
    assert "per-agent task allocation" in m.group(1).lower()
    init = (ROOT / "__init__.py").read_text()
    m = re.search(
        r'_POOL_STATUS_SCHEMA = \{\s*"name": "pool_status",\s*'
        r'"description": \(([^)]+)\)', init, re.S)
    assert m, "_POOL_STATUS_SCHEMA description shape changed"
    assert "per-claim" not in m.group(1).lower()
    assert "per-claim" not in yaml.lower(), "stale per-claim wording in plugin.yaml"
    assert "per-claim" not in init.lower(), "stale per-claim wording in __init__.py"


def test_catalog_lists_both_tools_and_current_version():
    data = (ROOT / "plugin-catalog" / "agent-pool.yaml").read_text()
    assert re.search(r'^version: "2\.7\.0"$', data, re.M), (
        "catalog version must be 2.7.0")
    caps = data.split("provides_tools:")[1].split("provides_hooks:")[0]
    assert "pool_status" in caps and "pool_report_usage" in caps


# --- Corrections gates: phrasing, pool wording, ephemeral push -------------

def test_no_platform_routes_phrasing():
    for fname in ("README.md", "tickets.py", "docs/architecture.md",
                  "docs/threat-model.md"):
        text = (ROOT / fname).read_text()
        assert "platform routes" not in text, f"{fname} still says platform routes"


def test_no_resolve_a_pool_in_docs():
    for fname in DOC_FILES:
        text = (ROOT / fname).read_text()
        assert "resolve a pool" not in text, f"{fname} still says resolve a pool"


def test_no_discord_user_flag_in_docs_or_commands():
    for fname in ("README.md", "docs/architecture.md", "docs/threat-model.md"):
        assert "--discord-user" not in (ROOT / fname).read_text(), fname
    init_src = (ROOT / "__init__.py").read_text()
    assert "--discord-user" not in init_src
    assert 'opts.get("discord_user"' not in init_src


def test_no_plugin_side_discord_post():
    for fname in PROD_FILES + ("replies.py",):
        src = (ROOT / fname).read_text()
        assert "post_message" not in src, f"{fname} posts to Discord"
        assert "ctx.post" not in src, f"{fname} posts via ctx"


# --- add-repo link-flow gates ----------------------------------------------

def test_add_repo_link_flow_documented_and_catalog_shaped():
    """The add-repo link flow modes (--deliver / approve / submissions)
    are documented in the README; the command catalog keeps the single
    add-repo entry (the command count does not change)."""
    readme = (ROOT / "README.md").read_text()
    for needle in ("/add-repo --deliver", "/add-repo approve",
                   "/add-repo submissions", "awaiting approval",
                   "contributor-add.py", "add_repo_build_bundle",
                   "add_repo_deliver", "add_repo_approve",
                   "add_repo_submissions", "repo_submission_refused"):
        assert needle in readme, f"README missing add-repo link flow: {needle!r}"
    data = (ROOT / "plugin-catalog" / "agent-pool.yaml").read_text()
    caps = data.split("provides_commands:")[1].split("provides_hooks:")[0]
    entries = [ln.strip("- ").strip() for ln in caps.splitlines()
               if ln.strip().startswith("-")]
    assert entries.count("add-repo") == 1, entries
    assert len(entries) == 10, entries
    yaml = (ROOT / "plugin.yaml").read_text()
    assert "add-repo" in yaml
