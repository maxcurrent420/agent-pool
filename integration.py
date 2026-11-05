"""Phase 5 — integration gate: diff scan, merge, maintainer edit, publish.

The maintainer's canonical checkout lives under the service work dir as
``canonical-<repo_id>`` (a git clone of the latest accepted bundle, advanced
only by :func:`merge_branch`). The contributor branch is fetched as a clone
(``RepoPropagator.fetch_pending``). The gate steps:

1. :func:`changed_files` — paths changed on the branch since the merge base.
2. :func:`scan_diff` — the Phase 2 keyword scanner (``_sandbox_scan``, bwrap
   network-off) run over a staging dir holding ONLY the changed files at the
   branch version (containment-by-construction: the scanner never executes
   repo code, only reads bytes, with the network unshared).
3. :func:`merge_branch` — ``git merge --no-ff`` of the contributor branch
   into canonical. On conflict the merge is aborted and canonical HEAD is
   left exactly as found; the caller bounces the ticket (``rebase needed`` +
   both refs).
4. :func:`apply_edit` — the maintainer's pre-merge edit, applied to the
   merged tree and committed as a follow-up commit on canonical:
   ``edit_files`` = ``{relpath: new_text}`` full-content overrides, or
   ``edit_patch`` = unified diff fed to ``git apply``. At least one must be
   given for an edit decision. The edited tree is re-scanned before publish.
5. Publish is ``RepoPropagator.publish_merged``: ``git bundle --all`` of the
   merged canonical, fresh DEK, maintainer-signed accepted manifest.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

try:  # package import
    from .propagation import _sandbox_scan
except ImportError:
    from propagation import _sandbox_scan


def _git(cwd: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, timeout=timeout)


def _git_ok(cwd: Path, *args: str) -> str:
    proc = _git(cwd, *args)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:400]}")
    return proc.stdout.strip()


def _head(cwd: Path) -> str:
    return _git_ok(Path(cwd), "rev-parse", "HEAD")


def _branch_of(clone_dir: Path) -> str:
    name = _git_ok(Path(clone_dir), "branch", "--show-current")
    return name or "HEAD"


def changed_files(branch_clone: Path, canonical_dir: Path) -> list[str]:
    """Paths changed on the branch tip vs the merge base with canonical.

    The merge base is found by walking canonical's history newest-first and
    taking the first commit that also exists in the branch clone (canonical
    may have advanced with merge commits the branch has never seen).
    """
    branch_clone, canonical_dir = Path(branch_clone), Path(canonical_dir)
    branch_tip = _head(branch_clone)
    base = None
    for sha in _git_ok(canonical_dir, "rev-list", "HEAD").splitlines():
        sha = sha.strip()
        if not sha:
            continue
        probe = _git(branch_clone, "cat-file", "-e", f"{sha}^{{commit}}")
        if probe.returncode == 0:
            base = sha
            break
    if base is None:
        raise RuntimeError("no common history between canonical and branch clone")
    out = _git_ok(branch_clone, "diff", "--name-only", f"{base}...{branch_tip}")
    return sorted(p for p in out.splitlines() if p.strip())


def scan_diff(branch_clone: Path, canonical_dir: Path, paths: list[str] | None = None) -> dict:
    """Sandboxed static scan of the branch DIFF (changed files only).

    Changed files are staged at the branch version into a temp dir (file
    deletions excluded) and scanned with the network-off Phase 2 scanner.
    Returns the scanner report plus ``diff_files`` and the branch tip.
    """
    branch_clone = Path(branch_clone)
    files = paths if paths is not None else changed_files(branch_clone, canonical_dir)
    with tempfile.TemporaryDirectory(prefix="agentpool-diffscan-") as tmp:
        staging = Path(tmp) / "diff"
        staging.mkdir()
        for rel in files:
            proc = _git(branch_clone, "show", f"{_head(branch_clone)}:{rel}")
            if proc.returncode != 0:
                continue  # deleted on the branch: nothing to scan
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(proc.stdout.encode("utf-8", errors="ignore"))
        report = _sandbox_scan(staging)
    report["diff_files"] = files
    report["branch_head"] = _head(branch_clone)
    return report


def rescan_tree(tree_dir: Path, paths: list[str]) -> dict:
    """Re-scan specific working-tree files after a maintainer edit."""
    tree_dir = Path(tree_dir)
    with tempfile.TemporaryDirectory(prefix="agentpool-rescan-") as tmp:
        staging = Path(tmp) / "tree"
        staging.mkdir()
        for rel in paths:
            src = tree_dir / rel
            if src.is_file():
                dest = staging / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(src.read_bytes())
        report = _sandbox_scan(staging)
    report["diff_files"] = list(paths)
    return report


def merge_branch(canonical_dir: Path, branch_clone: Path) -> dict:
    """Merge the contributor branch into the canonical checkout.

    Adds the branch clone as remote ``agent-pool-branch``, fetches, and
    merges its HEAD branch with ``--no-ff``. On conflict: aborts and returns
    ``{ok: False, conflict: True, ...}`` with canonical HEAD untouched.
    """
    canonical_dir, branch_clone = Path(canonical_dir), Path(branch_clone)
    canonical_dir.mkdir(parents=True, exist_ok=True)
    canonical_ref = _head(canonical_dir)
    branch_ref = _head(branch_clone)
    branch_name = _branch_of(branch_clone)
    if branch_name == "HEAD":
        branch_name = "HEAD"
    _git(canonical_dir, "remote", "remove", "agent-pool-branch")
    _git_ok(canonical_dir, "remote", "add", "agent-pool-branch", str(branch_clone))
    _git_ok(canonical_dir, "fetch", "agent-pool-branch")
    proc = _git(canonical_dir, "merge", "--no-ff", branch_ref,
                "-m", f"Merge branch {branch_name} into canonical (agent-pool gate)")
    if proc.returncode != 0:
        _git(canonical_dir, "merge", "--abort")
        status = _git_ok(canonical_dir, "status", "--porcelain")
        assert _head(canonical_dir) == canonical_ref, "canonical moved on conflict!"
        return {"ok": False, "conflict": True, "canonical_ref": canonical_ref,
                "branch_ref": branch_ref, "branch_name": branch_name,
                "detail": proc.stderr.strip()[:500], "porcelain": status}
    new_head = _head(canonical_dir)
    return {"ok": True, "conflict": False, "canonical_ref": canonical_ref,
            "branch_ref": branch_ref, "branch_name": branch_name,
            "merge_commit": new_head}


def apply_edit(canonical_dir: Path, *, edit_files: dict | None = None,
               edit_patch: str | None = None) -> dict:
    """Apply the maintainer's pre-merge edit to the merged canonical tree.

    ``edit_files={relpath: text}`` writes full file overrides; ``edit_patch``
    is a unified diff applied with ``git apply`` (stdin). Changes are staged
    and committed. At least one edit is required.
    """
    canonical_dir = Path(canonical_dir)
    if not edit_files and not edit_patch:
        raise ValueError("edit decision requires edit_files and/or edit_patch")
    touched: list[str] = []
    if edit_files:
        for rel, text in edit_files.items():
            rel = str(rel).strip()
            if not rel or rel.startswith("/") or ".." in rel:
                raise ValueError(f"bad edit path: {rel!r}")
            dest = canonical_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text)
            touched.append(rel)
    if edit_patch:
        patch_file = canonical_dir / ".agentpool-edit.patch"
        patch_file.write_text(edit_patch)
        try:
            proc = _git(canonical_dir, "apply", "--whitespace=nowarn", str(patch_file))
            # NOTE: `git apply` output captured for the error path below.
            if proc.returncode != 0:
                raise ValueError(f"edit patch does not apply: {proc.stderr.strip()[:500]}")
        finally:
            patch_file.unlink(missing_ok=True)
        touched.append("edit_patch")
    _git_ok(canonical_dir, "add", "-A")
    _git_ok(canonical_dir, "commit", "-q", "-m",
            "Maintainer edit before merge (agent-pool gate)")
    return {"edit_commit": _head(canonical_dir), "touched": touched}
