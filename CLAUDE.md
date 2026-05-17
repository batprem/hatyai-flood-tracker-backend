# Backend Agent Guide

This directory is the Python FastAPI backend for the Hat Yai flood warning project. It is a Git submodule of the parent repository at `hatyai-flood-warning`.

## Worktree First

Before editing any code in this submodule, the working sub-agent must run in an isolated git worktree. The parent project's `coordinator` agent handles this by spawning sub-agents with `isolation: "worktree"`, which the Claude Code harness translates into a temporary worktree of this submodule.

If you are an agent invoked here:

1. **Confirm you are inside an isolated worktree** before making any changes. If you were not spawned with worktree isolation and your task will modify files, stop and ask the coordinator to re-spawn you with `isolation: "worktree"`.
2. **Never run two write-mode agents on `backend/` in parallel without separate worktrees** — they will fight over the same working tree.
3. **Read-only investigations do not require a worktree.** Skip isolation for triage, schema review, log inspection, or documentation reading.
4. After finishing, report the worktree path and branch name back so the user can review the diff and decide whether to merge.

## Conventions

Follow the rules in the parent repository:

- `../.claude/rules/python.md` — Python 3.13+ typing, FastAPI/async IO, MongoDB collections, risk logic, docstrings, Ruff.
- `../.claude/rules/git.md` — submodule-aware Git workflow. Commit backend changes from inside `backend/`, never from the root.
- `../AGENT.md` — project context, risk levels, public-safety expectations.

Local tooling lives here:

- `pyproject.toml`, `uv.lock` — use `uv` for dependency management.
- `pyrightconfig.json` — type checking.
- `tests/` — pytest suite.

Run commands from `backend/` unless explicitly told otherwise.
