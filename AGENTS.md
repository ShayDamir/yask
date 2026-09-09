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

The yask MCP server (`yask_*` tools) is how every agent in the workflow talks
to yask. Subagents that implement/verify code also use the commands above.

Environment quirks:

- The dev environment has **no node**; syntax-check JS with
  `nix shell nixpkgs#nodejs -c node --check <file>`.
- New untracked files are **silently excluded from nix builds**
  (`src = lib.cleanSource ./.` reads the git tree). After creating files, run
  `git add -N <files>` so `nix build` / `nix flake check` include them.
- `.opencode/` and `opencode.json` are local tool state, not part of the
  project: `.opencode/` is gitignored, and the agents live there.

## Tech stack

- **Python 3** backend with a **SQLite** database.
- Two interfaces: a **web UI** (run locally) and an **MCP interface** for agents.
- Web UI default port is **4304 (0x10D0)** and is configurable.

## Domain rules (the subtle part — get these right)

These are not obvious from file names and are easy to implement incorrectly:

- **Projects** have completely separate state from each other.
- Every **task has a number**, starting at 1 and increasing per project.
- Regular task types: **Story, Task, Bug** (readily extensible).
- **Epic** is a compound type:
  - Epics nest inside other epics as a cycle-free **tree**; an Epic may also
    contain Stories, Tasks, and Bugs.
  - Epics are **not** estimated; an Epic's estimate is the **sum** of the
    estimates of all tasks it (transitively) contains.
- Task lifecycle states: `Backlog`, `Todo`, `Planning`, `In progress`,
  `Review`, `Done`, `Archived` — plus the holding state `Blocked`.
- `Blocked` is a **holding state**: any agent may move a task there, but the
  move must be accompanied by an `unblock.md` attachment explaining what is
  needed to unblock (questions for the user, decisions, inputs) and the
  resume state. Blocked tasks wait for the user, never for agents.
- Any task can be **archived** at any time; archived tasks are hidden by default.
   **Only archived tasks may be permanently deleted** — a task must be archived
   first, then deleted.
- Tasks are **sorted**; sorting and order-of-execution (top-down) must be
  preserved across changes.
- **Prerequisites**: moving a task pulls its prerequisites along **unless a
  prerequisite is already past that stage**. Example: task in Backlog with
  prereqs in Planning, Backlog, and Review — moving it to Todo also moves the
  Backlog prereq; the Planning and Review prereqs stay put.
- Changing state for **multiple tasks in one action requires a confirmation**:
  the tool returns `requires_confirmation` with the affected list and changes
  nothing; re-issue with `confirm: true` to apply.
- **Every state change is recorded with a timestamp.**
- Tasks may have **attachments**: markdown or images.

## Multi-agent workflow

Development dogfoods yask through the MCP interface. The repo's sessions run
on an **Orchestrator** agent that drives a loop — pick the next task that
needs work, hand it off to the matching subagent by **project + task
number**, repeat until nothing is actionable, then stop and report. Everyone
else is a subagent it spawns. Role-specific instructions live in
`.opencode/agent/{orchestrator,planner,executor,reviewer,judge}.md`; this
file documents only what every agent must agree on.

### Dispatch (state → agent)

Subagents handle the states the Orchestrator dispatches:

| Task state                | Handled by            | Ends with                         |
| ------------------------- | --------------------- | --------------------------------- |
| `Todo` / `Planning`       | Planner               | `In progress` (plan attached)     |
| `In progress`             | Executor              | `Review` (session summary attached) |
| `Review` (no review yet)  | Reviewer              | stays `Review` (review attached)  |
| `Review` (review attached)| Judge                 | `Done` (commit) or `In progress` (verdict) |
| `Todo`/`Planning` blocked on unmet prereqs | (skip until prereqs advance) | — |
| `Blocked` / `Backlog`     | nobody — waiting on user / unscheduled | — |
| `Done` / `Archived`       | nobody — finished     | —                                   |

The Dispatch rule details are the Orchestrator's job (see its role file).

### Handoff contract

- The Orchestrator hands a task to a subagent by **project + task number** —
  `Task #<n> in project <name>` and nothing more. Including the project
  removes any ambiguity, since numbers start at 1 in every project.
- Each subagent resolves the task itself via the yask MCP server and reads
  the task's title, description, prerequisites, and **all** attachments.
- Subagents never touch anything outside their own task except when creating
  a new task for out-of-scope work (below).

### Attachment conventions

Filenames are the contract; content is markdown unless noted:

| filename             | writer       | content                                        |
| -------------------- | ------------ | ---------------------------------------------- |
| `plan.md`            | Planner      | implementation plan                            |
| `session-summary.md` | Executor     | what changed, verification results, deviations |
| `review.md`          | Reviewer     | plan→code verification, findings, verdict      |
| `verdict.md`         | Judge        | what must be fixed (re-work round)             |
| `unblock.md`         | any agent    | why a task is `Blocked` and what unblocks it   |

Attach markdown by writing a temp file under `/tmp` and calling
`yask_add_attachment` with `file_path`, `content_type: text/markdown`, and
the canonical `filename`.

### Confirmation rule

Any move/archive/restore/delete that would change the state of multiple tasks
returns `requires_confirmation` plus the affected list and applies nothing.
The cascade is the domain's intended behavior (prerequisite pull-along), so
inspect the affected list, then re-issue with `confirm: true`. It must
contain only the task plus its dragged prerequisites.

### Blocking

Any agent may move its task to `Blocked` when it cannot proceed without
external input (ambiguous requirements, answers needed from the user, missing
information, a human decision). Rules:

- Attach `unblock.md` **before** moving: why blocked, exactly what is needed
  (a concrete list of questions for the user or other inputs), and the state
  to resume in.
- Never dispatch or advance a `Blocked` task. When nothing else is actionable
  the Orchestrator reports blocked tasks so the user can answer.
- Once the user answers and moves the task out of `Blocked` (web UI or
  otherwise), it re-enters the pipeline at its resume state.

### Commits

Only the **Judge** commits, and only when moving a task to `Done`: stage and
commit exactly that task's files with a concise conventional message. No other
agent commits; the Executor's work sits uncommitted until then.

### When a new task arises during implementation

If an agent (Planner, Executor, Reviewer) spots work outside the current
task's scope:

- Create it immediately as a task (`yask_create_task`) and move it to `Todo`
  so it is scheduled and not lost.
- If the current task depends on it, record that: `yask_set_prerequisites`
  with the current task's number and the new task as a prerequisite.
- Do **not** implement the new work inside the current task. Work in
  dependency order — finish prerequisite tasks first (through `Done`), then
  come back to the task that depended on them.

## Scope note

The README says development dogfoods yask on itself after MVP. Do not infer
additional features beyond the spec above; ask before extending. The `Blocked`
state is assumed by this workflow and is tracked as its own task in yask (in
`Todo`), which implements it in the backend while keeping these docs in sync.