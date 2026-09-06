"""Per-task worktree isolation for decompose siblings.

Decompose children used to inherit the root's literal ``workspace_path``,
so every sibling of a worktree-kind root pointed at the SAME checkout —
and ``_resolve_worktree_workspace``'s existing-checkout shortcut reused it
on whatever branch was there, letting sibling workers run concurrently in
one directory on one branch (cross-task provenance corruption, no lock).

Two-part fix under test:
- ``decompose_triage_task`` leaves worktree children's ``workspace_path``
  unset so each child materializes its own ``<repo>/.worktrees/<child-id>``.
- ``_resolve_worktree_workspace`` falls back to a fresh per-task worktree
  when the requested path is occupied by another task's branch (heals
  pre-existing rows that still carry a shared path).
"""

from __future__ import annotations

import subprocess
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [
            "git", "-C", str(cwd),
            "-c", "user.name=Test User",
            "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        check=True, capture_output=True, text=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True, capture_output=True, text=True,
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


def _add_worktree(repo: Path, target: Path, branch: str) -> Path:
    _git(repo, "worktree", "add", str(target), "-b", branch, "HEAD")
    return target


@pytest.mark.parametrize("case", ["prepared", "wrong-branch", "missing", "legacy", "unpinned", "changed-commit", "dirty-input"])
def test_controller_requires_exact_prepared_worktree(kanban_home, tmp_path, case):
    repo = _make_repo(tmp_path)
    target = repo / ".worktrees" / "assigned"
    expected_branch = "codex/controller-assigned"
    if case != "missing":
        _add_worktree(repo, target, expected_branch if case != "wrong-branch" else "codex/other")
        (target / "saved-work.txt").write_text("preserve this", encoding="utf-8")
    input_commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    if case in ("changed-commit", "dirty-input"):
        (target / "README.md").write_text("changed after controller preparation\n", encoding="utf-8")
        if case == "changed-commit":
            _git(target, "add", "README.md")
            _git(target, "commit", "-m", "unexpected replacement input")
    metadata = {"controllerRunId": "disposable-controller"}
    if case != "legacy":
        metadata["preparedWorkspace"] = {"version": 1 if case == "unpinned" else 2, "path": str(target), "branch": expected_branch, "inputCommit": input_commit}
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="controller assignment", assignee="default", body="<!-- codex-bmad-lifecycle " + json.dumps(metadata) + " -->",
                             workspace_kind="worktree", workspace_path=str(target), branch_name=expected_branch)
        task = kb.get_task(conn, tid)
    before = subprocess.check_output(["git", "-C", str(repo), "worktree", "list", "--porcelain"], text=True)
    if case == "prepared":
        assert kb._resolve_worktree_workspace(task, verify_input=True) == (target.resolve(), expected_branch)
    else:
        with pytest.raises(ValueError, match="controller.*prepared worktree"):
            kb._resolve_worktree_workspace(task, verify_input=True)
        # Exercise the real database/claim/workspace dispatch path, not only
        # its resolver. Invalid input must never reach even the launch boundary.
        launch_calls = []

        def forbidden_launch(*args, **kwargs):
            launch_calls.append((args, kwargs))
            raise AssertionError("invalid prepared input reached worker launch")

        with kb.connect() as conn:
            dispatched = kb.dispatch_once(conn, spawn_fn=forbidden_launch)
        assert dispatched.spawned == []
        assert launch_calls == []
        # Reopen the database to prove the failure and absent worker survive
        # connection loss. This is not a claimed process-exit certificate.
        with kb.connect() as conn:
            failed = conn.execute(
                "SELECT worker_pid, claim_lock, consecutive_failures, last_failure_error "
                "FROM tasks WHERE id = ?", (tid,),
            ).fetchone()
            assert failed["worker_pid"] is None
            assert failed["claim_lock"] is None
            assert failed["consecutive_failures"] == 1
            assert "exact prepared worktree" in failed["last_failure_error"]
    assert subprocess.check_output(["git", "-C", str(repo), "worktree", "list", "--porcelain"], text=True) == before
    if case != "missing":
        assert (target / "saved-work.txt").read_text(encoding="utf-8") == "preserve this"
    else:
        assert not target.exists()
    if case in ("changed-commit", "dirty-input"):
        assert (target / "README.md").read_text(encoding="utf-8") == "changed after controller preparation\n"


def test_decompose_worktree_children_get_own_workspace(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="build the feature", triage=True)
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', "
            "workspace_path='/repo/.worktrees/root' WHERE id = ?",
            (root,),
        )
        conn.commit()

        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[
                {"title": "spec it", "assignee": "alice", "parents": []},
                {"title": "implement it", "assignee": "bob", "parents": [0]},
            ],
            author="decomposer",
        )
        assert child_ids is not None and len(child_ids) == 2

        for cid in child_ids:
            row = conn.execute(
                "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
                (cid,),
            ).fetchone()
            assert row["workspace_kind"] == "worktree"
            # Each child resolves its own <repo>/.worktrees/<child-id> at
            # dispatch; the root's literal path must never be shared.
            assert row["workspace_path"] is None




def test_resolve_worktree_falls_back_when_path_occupied(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)
    occupied = _add_worktree(repo, repo / ".worktrees" / "sibling", "wt/sibling")

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="second sibling",
            workspace_kind="worktree",
            workspace_path=str(occupied),  # inherited shared/stale path
        )
        task = kb.get_task(conn, tid)

    workspace, branch = kb._resolve_worktree_workspace(task)
    assert workspace == (repo / ".worktrees" / tid).resolve()
    assert branch == f"wt/{tid}"
    # The sibling's checkout is untouched, still on its own branch.
    assert (occupied / "README.md").exists()
    head = subprocess.run(
        ["git", "-C", str(occupied), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == "wt/sibling"
