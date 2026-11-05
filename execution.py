"""Phase 4 — agent execution: worktrees, worker sessions, budget enforcement.

Real mechanisms (no stubs):

- Worktree isolation: ``git worktree add ../wt-<ticket> -b ticket/<id>`` on
  the clone produced by :meth:`RepoPropagator.fetch` (real Iroh transport).
- Worker sessions: Hermes' plugin-safe ``ctx.subagent_lifecycle`` service
  (``launch``/``status``/``result``/``cancel`` over
  ``SubagentLaunchRequest``). That API has no ``working_directory``, no
  per-launch timeout and no per-tool blocking — budgeted enforcement is
  therefore done by *polling* the handle (wall clock + turn counter from
  ``usage_metadata.api_calls``) and hard-killing via ``lifecycle.cancel()``
  (which issues a hard interrupt on the child agent).
- Local workers (tests, CLI, and the exit-criterion demo) run as a real
  child *process group* (``LocalProcessRunner``): ``start_new_session=True``
  + ``os.killpg`` SIGTERM→SIGKILL on cancel. That is the documented
  hard-kill equivalent of Hermes' ``process_manage`` kill for spawned
  processes; the lifecycle path uses ``lifecycle.cancel()`` instead.
- Sandbox: worker commands and acceptance tests run under
  ``bwrap --unshare-net`` (network off) with the worktree as the only
  writable bind when bubblewrap is available; otherwise the report says
  so explicitly (``fallback:unsandboxed``) — never silently claimed.
- Progress goes to the ticket thread via the gateway only
  (``post_notice``, 4 KB guard, no repo bytes).
"""
from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


class BudgetExhausted(Exception):
    """Persisted budget (claims.budget_json) was exceeded."""
    def __init__(self, reason: str, usage: dict | None = None):
        super().__init__(reason)
        self.reason = reason
        self.usage = usage or {}


@dataclass(frozen=True)
class SandboxPolicy:
    """Untrusted-repo sandbox: network off is mandatory, not advisory."""
    network_off: bool = True
    backend: str = "bwrap"

    def describe(self) -> str:
        return ("sandbox: bubblewrap, network OFF (--unshare-net), worktree is the "
                "only writable location, untrusted repo content must not be executed "
                "outside this sandbox")

    def available(self) -> bool:
        return shutil.which("bwrap") is not None


def sandboxed_command(cmd: list[str], worktree: Path) -> tuple[list[str], str]:
    """Wrap *cmd* in bubblewrap (network off) if available.

    Returns ``(argv, mode)`` where mode is ``bwrap:network-off`` or
    ``fallback:unsandboxed``. The worktree is bound read-write at the same
    path; system dirs are read-only binds (same layout as the Phase 2 audit
    scanner, which this mirrors so both gates behave identically).
    """
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return list(cmd), "fallback:unsandboxed (no sandbox binary available)"
    import tempfile
    tmp_base = Path(tempfile.gettempdir())
    argv = [bwrap, "--unshare-net", "--unshare-ipc", "--unshare-pid",
            "--die-with-parent",
            "--ro-bind", "/usr", "/usr", "--ro-bind", "/bin", "/bin",
            "--ro-bind", "/lib", "/lib", "--ro-bind", "/lib64", "/lib64",
            "--ro-bind", "/etc", "/etc", "--ro-bind", str(tmp_base), str(tmp_base),
            "--dev-bind", "/dev", "/dev", "--proc", "/proc",
            "--bind", str(worktree), str(worktree),
            "--chdir", str(worktree)]
    # Bind interpreter site-packages outside /usr (e.g. pytest installed in
    # ~/.local/lib/pythonX.Y/site-packages) read-only at the same path, so
    # acceptance commands can import their test runner inside the sandbox.
    try:
        import site as _site
        site_dirs = []
        try:
            site_dirs += _site.getsitepackages()
        except Exception:
            pass
        try:
            site_dirs.append(_site.getusersitepackages())
        except Exception:
            pass
        for d in site_dirs:
            p = Path(str(d))
            if p.is_dir() and str(p) not in ("/usr", "/etc") and \
                    not str(p).startswith("/usr/"):
                argv += ["--ro-bind", str(p), str(p)]
    except Exception:
        pass
    argv += list(cmd)
    return argv, "bwrap:network-off"


def create_worktree(clone_dir, ticket_id: str, base_dir) -> dict:
    """``git worktree add <base>/wt-<ticket> -b ticket/<ticket>`` from the clone."""
    clone_dir, base_dir = Path(clone_dir), Path(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    wt = base_dir / f"wt-{ticket_id}"
    subprocess.run(["git", "-C", str(clone_dir), "worktree", "prune"],
                   capture_output=True, text=True, timeout=60)
    if wt.exists():
        subprocess.run(["git", "-C", str(clone_dir), "worktree", "remove",
                        "--force", str(wt)], capture_output=True, text=True, timeout=60)
    branch = f"ticket/{ticket_id}"
    proc = subprocess.run(
        ["git", "-C", str(clone_dir), "worktree", "add", "-b", branch, str(wt)],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        # Branch may already exist from a previous attempt: reuse it.
        proc2 = subprocess.run(
            ["git", "-C", str(clone_dir), "worktree", "add", str(wt), branch],
            capture_output=True, text=True, timeout=120)
        if proc2.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {proc.stderr.strip()[:400]} "
                               f"{proc2.stderr.strip()[:400]}")
    return {"worktree": str(wt), "branch": branch, "clone_dir": str(clone_dir)}


def build_mandate_brief(*, ticket: dict, budget: dict, worktree: str, branch: str,
                        sandbox: SandboxPolicy) -> str:
    """The worker seed: mandate + budget + sandbox policy, all injected.

    This is the exact ``goal`` handed to the worker session / process.
    """
    return "\n".join([
        "You are an Agent Pool worker. Work ONLY in the git worktree below.",
        f"ticket_id: {ticket['ticket_id']}",
        f"repo_id: {ticket['repo_id']}",
        f"worktree: {worktree}",
        f"branch: {branch}",
        f"title: {ticket['title']}",
        f"mandate brief: {ticket.get('brief', '')}",
        f"budget: max_turns={budget.get('max_turns')} "
        f"max_tasks_per_day={budget.get('max_tasks_per_day')} "
        f"token_cap={budget.get('token_cap')} "
        f"wall_clock_seconds={budget.get('wall_clock_seconds')}",
        f"sandbox policy: {sandbox.describe()}",
        "Rules: stay within the declared file paths for this ticket; do not "
        "access the network; commit your changes on the ticket branch; report "
        "progress only as short milestone notices (no code/diffs in messages).",
    ])


@dataclass
class BudgetTracker:
    """Turn counter + wall clock + token cap over the persisted budget."""
    budget: dict
    started_at: float = field(default_factory=time.time)

    def usage(self, reported: dict | None = None) -> dict:
        reported = reported or {}
        return {
            "turns": int(reported.get("turns", reported.get("api_calls", 0))),
            "tokens": int(reported.get("tokens", 0)),
            "wall_clock_seconds": round(time.time() - self.started_at, 3),
        }

    def check(self, reported: dict | None = None) -> dict:
        """Raise BudgetExhausted if any budget dimension is exceeded."""
        u = self.usage(reported)
        b = self.budget
        if u["turns"] > int(b.get("max_turns", 10**9)):
            raise BudgetExhausted(
                f"turn budget exhausted: {u['turns']} turns > max_turns={b['max_turns']}", u)
        if u["tokens"] > int(b.get("token_cap", 10**18)):
            raise BudgetExhausted(
                f"token budget exhausted: {u['tokens']} tokens > token_cap={b['token_cap']}", u)
        if u["wall_clock_seconds"] > float(b.get("wall_clock_seconds", 10**18)):
            raise BudgetExhausted(
                f"wall-clock budget exhausted: {u['wall_clock_seconds']}s > "
                f"wall_clock_seconds={b['wall_clock_seconds']}", u)
        return u


class WorkerRunner(ABC):
    """Worker-session backend. Handles are opaque to PoolService."""

    @abstractmethod
    def launch(self, mandate: str, *, worktree: str) -> object:
        """Launch a worker seeded with *mandate*; return an opaque handle."""

    @abstractmethod
    def poll(self, handle) -> dict:
        """Return {state, ready, summary, usage}. state in running/done/failed."""

    @abstractmethod
    def cancel(self, handle, reason: str) -> bool:
        """Hard-kill the worker (process-group kill or lifecycle cancel)."""


class LocalProcessRunner(WorkerRunner):
    """Run a worker command as a real child process group.

    The command runs inside the bwrap sandbox (network off) when bwrap is
    present. ``cancel`` sends SIGTERM to the whole process group, then
    SIGKILL — a real hard kill, verified by the caller via ``poll``.
    """

    def __init__(self, command: list[str], *, sandbox: SandboxPolicy | None = None):
        if not command:
            raise ValueError("LocalProcessRunner needs a worker command")
        self._command = list(command)
        self._sandbox = sandbox or SandboxPolicy()

    def launch(self, mandate: str, *, worktree: str):
        argv, mode = sandboxed_command(self._command, Path(worktree))
        env = dict(os.environ)
        env["AGENT_POOL_MANDATE"] = mandate
        proc = subprocess.Popen(argv, cwd=str(worktree), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        return {"proc": proc, "pid": proc.pid, "sandbox_mode": mode,
                "worktree": worktree}

    def poll(self, handle) -> dict:
        proc = handle["proc"]
        rc = proc.poll()
        if rc is None:
            return {"state": "running", "ready": False, "summary": None,
                    "usage": {"turns": 0, "tokens": 0}}
        out, err = proc.communicate()
        ok = rc == 0
        return {"state": "done" if ok else "failed", "ready": True,
                "summary": (out or err or "").strip()[-2000:],
                "usage": {"turns": 1 if ok else 0, "tokens": 0},
                "returncode": rc}

    def cancel(self, handle, reason: str) -> bool:
        proc = handle["proc"]
        if proc.poll() is not None:
            return False
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return False
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait(timeout=5)
        return proc.poll() is not None


class LifecycleWorkerRunner(WorkerRunner):
    """Hermes worker sessions via ``ctx.subagent_lifecycle``.

    ``launch`` uses SubagentLaunchRequest(goal=mandate, ...). That public
    API rejects ``working_directory``/``timeout_seconds``/``blocked_tools``,
    so the worktree path and sandbox policy are inside the goal text and
    budget enforcement is the polling loop in :func:`run_worker_loop`.
    ``cancel`` is ``lifecycle.cancel(handle, reason=...)`` (hard interrupt).
    """

    def __init__(self, lifecycle, *, allowed_toolsets=None):
        self._lifecycle = lifecycle
        self._allowed_toolsets = tuple(allowed_toolsets or ())

    def launch(self, mandate: str, *, worktree: str):
        request = {"goal": mandate, "role": "leaf",
                   "correlation_id": f"agent-pool-{int(time.time())}-{os.getpid()}"}
        if self._allowed_toolsets:
            request["allowed_toolsets"] = self._allowed_toolsets
        return self._lifecycle.launch(request)

    def poll(self, handle) -> dict:
        status = self._lifecycle.status(handle)
        state = str(getattr(status, "state", status))
        result = None
        try:
            result = self._lifecycle.result(handle)
        except Exception:
            result = None
        ready = bool(getattr(result, "ready", False)) if result is not None else False
        usage = {}
        summary = None
        if result is not None:
            usage = {"turns": int((getattr(result, "usage_metadata", None) or {})
                                   .get("api_calls", 0)),
                     "tokens": int((getattr(result, "usage_metadata", None) or {})
                                   .get("tokens", 0))}
            summary = getattr(result, "summary", None)
        terminal = state in ("SUCCEEDED", "FAILED", "INTERRUPTED", "CANCELLED")
        return {"state": "done" if state == "SUCCEEDED" else
                ("failed" if terminal else "running"),
                "ready": ready or terminal, "summary": summary, "usage": usage,
                "lifecycle_state": state}

    def cancel(self, handle, reason: str) -> bool:
        result = self._lifecycle.cancel(handle, reason=reason)
        return bool(getattr(result, "accepted", result))


def run_worker_loop(runner: WorkerRunner, handle, tracker: BudgetTracker, *,
                    poll_interval: float = 0.05, max_polls: int | None = None) -> dict:
    """Poll until terminal; enforce budget; hard-kill on exhaustion.

    On BudgetExhausted the worker is cancelled (hard kill) *before* the
    exception propagates, so no runaway worker survives enforcement.
    """
    polls = 0
    while True:
        polls += 1
        if max_polls is not None and polls > max_polls:
            runner.cancel(handle, "poll limit reached")
            raise BudgetExhausted("poll limit reached", tracker.usage())
        status = runner.poll(handle)
        try:
            tracker.check(status.get("usage") or {})
        except BudgetExhausted as exc:
            runner.cancel(handle, exc.reason)  # hard kill in all backends
            raise
        if status.get("state") in ("done", "failed"):
            return status
        time.sleep(max(0.001, poll_interval))


def run_acceptance(worktree, cmd: list[str], *, timeout: int = 300):
    """Run the ticket's acceptance command sandboxed (network off)."""
    if not cmd:
        raise ValueError("acceptance command is required")
    argv, mode = sandboxed_command(list(cmd), Path(worktree))
    proc = subprocess.run(argv, cwd=str(worktree), capture_output=True,
                          text=True, timeout=timeout)
    return {"ok": proc.returncode == 0, "returncode": proc.returncode,
            "sandbox": mode, "output": (proc.stdout + proc.stderr)[-4000:]}
