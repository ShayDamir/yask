# AGENTS.md

## Project

The project name is **`yask`**. All `yask_*` tools and agent dispatches use this name.

## Repo state

The **MVP is implemented**: `yask/` (Python package: `store.py` domain logic,
`api.py` REST + static hosting, `web/` vanilla-JS UI, `mcp_server.py`,
`cli.py`), `tests/` (pytest), flake packaging. `README.md` is the
authoritative product spec; treat it as truth and flag gaps with the user
rather than inventing rules.

## Commands

Always work inside the nix dev environment (no system Python deps):

```
nix develop                                # shell with Python + deps + pytest
nix develop -c bash -c 'python3 -m pytest tests -q'   # run the test suite
nix build                                  # build the package
nix flake check                            # build + run tests hermetically
nix develop -c yask serve                  # web UI on http://127.0.0.1:4304
nix develop -c yask mcp                    # MCP server on stdio
nix develop -c yask telegram               # Telegram bot (needs $TELEGRAM_BOT_TOKEN)
```

The yask MCP server (`yask_*` tools) is how every agent talks to yask.

Environment quirks:

- No **node**; syntax-check JS with
  `nix shell nixpkgs#nodejs -c node --check <file>`.
- New untracked files are **silently excluded from nix builds** (`src =
  lib.cleanSource ./.` reads the git tree). After creating files, run
  `git add -N <files>` so `nix build` / `nix flake check` include them.
- `.opencode/` and `opencode.json` are local tool state, not part of the
  project.

## Domain rules (the subtle part — get these right)

- **Projects** have completely separate state from each other.
- Every **task has a number**, starting at 1 and increasing per project.
- Regular task types: **Story, Task, Bug** (readily extensible).
- **Epic** is a compound type:
  - Epics nest as a cycle-free **tree**; an Epic may also contain Stories,
    Tasks, and Bugs.
  - Epics are **not** estimated; an Epic's estimate is the **sum** of all
    tasks it (transitively) contains.
- Task lifecycle states: `Backlog`, `Todo`, `Planning`, `In progress`,
  `Review`, `Done`, `Archived` — plus the holding state `Blocked`.
- `Blocked` is a **holding state**: moving a task there must be accompanied
  by an `unblock.md` attachment (what is needed to unblock — questions for
  the user, decisions, inputs — and the resume state). Blocked tasks wait for
  the user, never for agents.
- Any task can be **archived** at any time; archived tasks are hidden by
  default. **Only archived tasks may be permanently deleted.**
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

Development dogfoods yask through the MCP interface. An **Orchestrator** runs
a loop: pick the next task with `yask_get_next_task(project)`, hand it to the
matching subagent by **project + task number** (`Task #<n> in project
<name>`, nothing more), repeat until nothing is actionable. Role-specific
instructions live in `.opencode/agent/{orchestrator,planner,epic-planner,
investigator,executor,reviewer,judge}.md`; this file documents only what every
agent must agree on.

### Dispatch (state → agent)

| Task state                          | Handled by          | Ends with                         |
| ----------------------------------- | ------------------- | --------------------------------- |
| `Todo` / `Planning` (Epic)          | Epic Planner        | stays `Todo` (prereqs set on subtasks) |
| `Todo` / `Planning` (Investigation) | Investigator        | `Review` (epics created, `investigation.md` attached) |
| `Todo` / `Planning`                 | Planner             | `In progress` (plan attached)     |
| `In progress` (Investigation)       | Investigator (re-work) | `Review` (session summary attached) |
| `In progress`                       | Executor            | `Review` (session summary attached) |
| `Review` (no review yet)            | Reviewer            | stays `Review` (review attached)  |
| `Review` (review attached)          | Judge               | `Done` (commit) or `In progress` (verdict) |
| `Todo`/`Planning` blocked on unmet prereqs | (skip until prereqs advance) | — |
| `Blocked` / `Backlog`               | nobody — waiting on user / unscheduled | — |
| `Done` / `Archived`                 | nobody — finished    | —                                   |

The dispatch mechanics are the Orchestrator's job (see its role file).

### Epic workflow

Epics are containers, not work items; planning is about organizing subtasks,
not producing an implementation plan. The flow:

1. An Epic in `Todo` is dispatched to the **Epic Planner**.
2. It reviews the tree (`yask_get_project`), creates missing tasks, sets
   prerequisites between subtasks, and defines execution order.
3. It sets **all direct child tasks as prerequisites of the Epic itself**
   (`yask_set_prerequisites`). That is the gate: `get_next_task` skips the
   Epic until all subtasks are done.
4. Subtasks flow through the normal pipeline.
5. When all subtasks are `Done`, `get_next_task` returns the Epic again and
   the Orchestrator dispatches the Judge to move it to `Done`.

An Epic **never enters `In progress`** — it stays in `Todo` until all its
subtasks complete, then jumps to `Done`. If subtasks already exist and are
properly ordered, the planner may skip creating new tasks and just attach an
`epic-plan.md` summary; the key output is the prerequisite links.

### Investigation workflow

Investigation tasks produce **no code** — they produce **Epics**:

1. An Investigation task in `Todo` (or re-dispatched from `In progress`
   after a verdict) goes to the **Investigator**.
2. It researches the topic (repo docs/code plus internet sources), then
   creates **one or several Epics** in `Backlog` with self-contained
   descriptions. The epics get **no subtasks and no prerequisites** — that is
   the Epic Planner's job.
3. It attaches `investigation.md` to every created epic, attaches a
   `session-summary.md` to the task, and moves the task to `Review`.
4. The task flows through the normal review pipeline; the Judge moves it to
   `Done` **without a commit**.
5. The epics stay in `Backlog` until the **user** moves them to `Todo`, where
   the Epic Planner splits them using the epic's `investigation.md` as scope.

### Handoff contract

- The Orchestrator hands a subagent a task by **project + task number** —
  `Task #<n> in project <name>` and nothing more.
- Each subagent resolves the task itself with `yask_get_task(project, number)`
  and reads only the attachments relevant to its role (`yask_last_attachment`,
  pulling older ones only when needed).
- Subagents never touch anything outside their own task except when creating
  a new task for out-of-scope work (below).

### Attachment conventions

Filenames are the contract; content is markdown unless noted:

| filename           | writer       | content                                      |
| ------------------ | ------------ | -------------------------------------------- |
| `plan.md`          | Planner      | implementation plan                          |
| `epic-plan.md`     | Epic Planner | task breakdown, prerequisites, execution order |
| `investigation.md` | Investigator | result of investigation, attached to created epics |
| `session-summary.md` | Executor   | what changed, verification results, deviations |
| `review.md`        | Reviewer     | plan→code verification, findings, verdict    |
| `verdict.md`       | Judge        | what must be fixed (re-work round)           |
| `unblock.md`       | any agent    | why a task is `Blocked` and what unblocks it |

Attach markdown by writing a temp file under `/tmp` and calling
`yask_add_attachment` with `file_path`, `content_type: text/markdown`, and
the canonical `filename`.

### Confirmation rule

Any move/archive/restore/delete that would change the state of multiple tasks
returns `requires_confirmation` plus the affected list and applies nothing.
The cascade is the domain's intended behavior (prerequisite pull-along), so
inspect the affected list (it must contain only the task plus its dragged
prerequisites), then re-issue with `confirm: true`.

### Blocking

Any agent may move its task to `Blocked` when it cannot proceed without
external input (ambiguous requirements, missing information, a human
decision). Attach `unblock.md` **before** moving: why blocked, a concrete
list of what is needed (questions for the user, decisions, inputs), and the
resume state. Never dispatch or advance a `Blocked` task; when nothing else
is actionable the Orchestrator reports blocked tasks so the user can answer.
Once the user moves the task out of `Blocked`, it re-enters the pipeline at
its resume state.

### Commits

Only the **Judge** commits, and only when moving a task to `Done`: stage and
commit exactly that task's files with a concise conventional message.
**Exception:** Investigation tasks reach `Done` **without a commit** — they
produce no code, only epics and documents inside yask.

### When a new task arises during implementation

If an agent (Planner, Executor, Reviewer) spots work outside the current
task's scope: create it immediately with `yask_create_task`, move it to
`Todo`, and — if the current task depends on it — record that with
`yask_set_prerequisites`. Do **not** implement it inside the current task.
Work in dependency order: finish prerequisite tasks first (through `Done`),
then return to the task that depended on them.

## Scope note

Do not infer additional features beyond the spec above; ask before extending.
The `Blocked` state is assumed by this workflow and is tracked as its own
task in yask (in `Todo`), which implements it in the backend while keeping
these docs in sync.