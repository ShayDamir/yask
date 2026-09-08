# AGENTS.md

## Repo state

The **MVP is implemented**: `yask/` (Python package: `store.py` domain logic,
`api.py` REST + static hosting, `web/` vanilla-JS UI, `mcp_server.py`,
`cli.py`), `tests/` (pytest), flake packaging. `README.md` remains the
authoritative product spec; if a behavior is unspecified there, treat the
README as truth and flag gaps with the user rather than inventing rules.

## Commands

No system Python deps — always work inside the nix dev environment:

```
nix develop                                # shell with Python + deps + pytest
nix develop -c bash -c 'python3 -m pytest tests -q'   # run the test suite
nix build                                  # build the package
nix flake check                            # build + run tests hermetically
nix develop -c yask serve                  # web UI on http://127.0.0.1:4304
nix develop -c yask mcp                    # MCP server on stdio
```

Environment quirks:

- The dev environment has **no node**; syntax-check JS with
  `nix shell nixpkgs#nodejs -c node --check <file>`.
- New untracked files are **silently excluded from nix builds**
  (`src = lib.cleanSource ./.` reads the git tree). After creating files, run
  `git add -N <files>` so `nix build` / `nix flake check` include them.
- `.opencode/` is local tool state, not part of the project.

## Tech stack

- **Python 3** backend with a **SQLite** database.
- Two interfaces: a **web UI** (run locally) and an **MCP interface** for agents.
- Web UI default port is **4304 (0x10D0)** and is configurable.

## Domain rules (the subtle part — get these right)

These are not obvious from file names and are easy to implement incorrectly:

- **Projects** have completely separate state from each other.
- Every **task has a number**, starting at 1 and increasing.
- Regular task types: **Story, Task, Bug** (readily extensible).
- **Epic** is a compound type:
  - Epics nest inside other epics as a cycle-free **tree**; an Epic may also
    contain Stories, Tasks, and Bugs.
  - Epics are **not** estimated; an Epic's estimate is the **sum** of the
    estimates of all tasks it (transitively) contains.
- Task lifecycle states: `Backlog`, `Todo`, `Planning`, `In progress`,
  `Review`, `Done`, `Archived`.
- Any task can be **archived** at any time; archived tasks are hidden by default
  and can be permanently deleted.
- Tasks are **sorted**; sorting and order-of-execution (top-down) must be
  preserved across changes.
- **Prerequisites**: moving a task pulls its prerequisites along **unless a
  prerequisite is already past that stage**. Example: task in Backlog with
  prereqs in Planning, Backlog, and Review — moving it to Todo also moves the
  Backlog prereq; the Planning and Review prereqs stay put.
- Changing state for **multiple tasks in one action requires a confirmation**.
- **Every state change is recorded with a timestamp.**
- Tasks may have **attachments**: markdown or images.

## Task-solving workflow

Development dogfoods yask through the MCP interface (`yask_*` tools). Solve
one task at a time, moving it through its lifecycle and recording evidence in
yask as attachments:

1. A task starts in `Backlog`. Move it to `Todo` when it is scheduled and to
   `Planning` when you start on it. Before implementing, write a short plan
   and attach it to the task (`yask_add_attachment`).
2. Move the task to `In progress` and implement the smallest appropriate
   change for that task only — do not bundle unrelated fixes.
3. Verify per the Commands section above (`python3 -m pytest tests -q`, and
   `nix flake check` before calling it done; syntax-check edited JS with
   `nix shell nixpkgs#nodejs -c node --check <file>`), then attach a short
   session summary and move the task to `Review`.
4. Address review findings in a follow-up round (attach your response). Run a
   second review once the findings are resolved: if it finds **no significant
   findings**, commit the changes, then the task may be marked `Done` without
   waiting for the human. Otherwise leave the task in `Review` for the human
   to decide.

### When a new task arises during implementation

If work outside the current task's scope comes up mid-implementation:

- Create it immediately as a task (`yask_create_task`) and move it to `Todo`
  so it is scheduled and not lost.
- If the current task depends on it, record that: `yask_set_prerequisites`
  with the current task's number and the new task as a prerequisite.
- Do **not** implement the new work inside the current task. Work in
  dependency order — finish prerequisite tasks first (through `Done`), then
  come back to the task that depended on them.

## Scope note

The README says development dogfoods yask on itself after MVP. Do not infer
additional features beyond the spec above; ask before extending.
