"""WO-005: operator notification + audit read surface.

- add-repo deliver replies carry the ephemeral agent_prompt instructing
  the agent to notify the operator (verdict + audit id), ask for the
  decision, and act on it (approve via the agent-side path, deny to
  record).
- /audit show --repo returns the latest report incl. the ClamAV
  sub-report (service-level; the handler parse is covered by the
  registered==declared audit in test_workflow_c_claim.py).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.test_add_repo_semantics import OWNER, REPO, world  # noqa: E402


def test_add_repo_deliver_agent_prompt_notifies_operator(tmp_path):
    w = world(tmp_path)
    bundle = w["member_svc"].add_repo_build_bundle(w["src"], REPO)
    manifest = bundle["manifest"]
    out = w["maint_svc"].add_repo_deliver(
        manifest["transfer_ticket"], manifest, source={"user_id": OWNER})
    assert out["ok"] is True and out["state"] == "submitted"
    prompt = out["agent_prompt"]
    assert prompt["event"] == "repo_submitted"
    assert prompt["ephemeral"] is True
    assert prompt["repo_id"] == REPO
    assert prompt["audit_id"] == out["audit_id"]
    assert prompt["verdict"] == "clean"
    assert prompt["clamav_verdict"] == "clean"
    text = prompt["prompt"]
    assert "awaiting approval" in text
    assert out["audit_id"] in text
    assert "Notify the operator" in text
    assert "approve" in text and "--deny" in text
    assert len(json.dumps(out)) < 4096


def test_audit_show_by_repo_includes_clamav(tmp_path):
    w = world(tmp_path)
    bundle = w["member_svc"].add_repo_build_bundle(w["src"], REPO)
    manifest = bundle["manifest"]
    w["maint_svc"].add_repo_deliver(
        manifest["transfer_ticket"], manifest, source={"user_id": OWNER})
    shown = w["maint_svc"].audit_show(repo_id=REPO)
    assert shown["ok"] is True and shown["repo_id"] == REPO
    assert shown["verdict"] == "clean"
    report = shown["report"]
    assert report["clamav"]["engine"] == "clamav"
    assert report["clamav"]["verdict"] == "clean"
    assert "static" in report


def test_awaiting_approval_lists_both_paths(tmp_path):
    """The discovery surface lists submitted versions from the add-repo
    path (push path coverage lives in test_push_path)."""
    w = world(tmp_path)
    bundle = w["member_svc"].add_repo_build_bundle(w["src"], REPO)
    manifest = bundle["manifest"]
    w["maint_svc"].add_repo_deliver(
        manifest["transfer_ticket"], manifest, source={"user_id": OWNER})
    awaiting = w["maint_svc"].awaiting_approval(source={"user_id": OWNER})
    items = awaiting["awaiting_approval"]
    assert len(items) == 1
    item = items[0]
    assert item["repo_id"] == REPO and item["version"] == 1
    assert item["state"] == "submitted"
    assert item["verdict"] == "clean"
    assert item["audit_id"]
    # Deny records the denial and clears the queue.
    denied = w["maint_svc"].add_repo_deny(REPO, source={"user_id": OWNER})
    assert denied["ok"] is True and denied["state"] == "denied"
    assert w["maint_svc"].awaiting_approval(
        source={"user_id": OWNER})["awaiting_approval"] == []
    ev = w["conn"].execute(
        "SELECT payload_json FROM events WHERE kind='repo_submission_denied'"
    ).fetchone()
    assert ev is not None
