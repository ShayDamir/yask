# Yet Another Simple Kanban board (yask)

This is a simple Kanban board that can track multiple projects.

The functionality will be extended later.

The MVP definition:

* track multiple projects with completely separate state between them
* every task has a number, starting with 1 and increasing
* regular task types: Story, Task, Bug. Can be easily changed anytime.
* regular task estimate: story points
* compound task type: Epic
* Epics can contain other epics (tree-like structure) without cycles, and also Stories, Tasks and Bugs
* Epics cannot be estimated, they contain the sum of estimations of all contained tasks
* Tasks state: Backlog, Todo, Planning, In progress, Review, Done, Blocked, Archived
* Any task can be archived at any time
* Archived tasks are not listed by default. They can be permanently deleted.
* Blocked is a holding state for a task that cannot proceed until external input arrives (answers, a decision, missing info). It is skipped by the pipeline until the task is moved back to a normal state; moving to Blocked carries no prerequisite pull.
* Tasks are sorted, sorting must be preserved. Order of execution is top-down.
* Tasks can have other tasks as prerequisite
* If task has prerequisites and is moved in the workflow, prerequisites are moved with it unless they're already past the stage
* example: task is in Backlog, and has prereqs in Planning, Backlog and Review stages. If the task is moved from Backlog from Todo, the prerequisite that is also in Backlog is moved too. Others stay at their stages.
* ask confirmation before changing state for multiple tasks in one action
* Each state change is tracked with timestamp
* Tasks can have attachments - markdown or images

Interface:

* web interface (on local machine), configurable port, 4304 (0x10D0)
* MCP interface for agents

Tech stack:

* sqlite backend
* python3

After MVP, the development of yask will dogfood itself to add more features.

## Usage

### Development

Everything is provided by the flake (nixpkgs 26.05). Enter the dev environment
(Python with all dependencies and pytest):

```
nix develop
```

Run the test suite (131 tests covering the domain rules above):

```
python3 -m pytest tests -q
```

Full build + tests, hermetically:

```
nix build
nix flake check
```

### Web UI

```
yask serve                # http://127.0.0.1:4304  (0x10D0)
yask serve --port 9000    # or: YASK_PORT=9000 yask serve
yask serve --data DIR     # or: YASK_DATA=DIR yask serve
```

The UI is vanilla ES modules (no build step). State lives in a SQLite database
inside the data directory (default `~/.local/share/yask/yask.db`).

### MCP interface

```
yask mcp
```

Speaks MCP over stdio. Example client config:

```json
{ "mcpServers": { "yask": { "command": "nix", "args": ["run", "/path/to/yask", "--", "mcp"] } } }
```

Tools: list/create projects and tasks, set prerequisites, move/archive/
restore/delete/reorder tasks, task history, task types, attachments (list,
read — images come back as viewable image blocks, markdown as text — plus a
`last_attachment` convenience that returns the task's most recent one). Actions
that would touch several tasks return `requires_confirmation` plus the list of
affected tasks; re-invoke with `confirm: true` to apply.

### Telegram bot

```
TELEGRAM_BOT_TOKEN=123:ABC yask telegram
yask telegram --data DIR    # or: YASK_DATA=DIR yask telegram
```

Runs the bot as a separate process: long-polls the Bot API with the token
from `TELEGRAM_BOT_TOKEN` (get one from @BotFather) and answers `/start`,
`/help`, `/projects` (the list of projects with their per-state task counts),
`/tasks [project]` (the tasks in the active states — Todo, Planning,
In progress and Review — grouped by project and state), `/task
<project> <number|title>` (one task's details — state, estimate,
description, prerequisites, attachments and recent history — the task
found by number or by title) and `/attachment <project> <task> <id>`
(sends a task's attachment to the chat as a file). A chat can
`/subscribe [project]` to receive notifications about every task state
change in that project, and `/unsubscribe [project]` to stop them —
subscriptions are per chat and per project and persist across restarts; the
bot detects changes by polling `state_history`, so latency is at most one
poll interval (~30 s). More board commands are on the way. Reads the same
data directory as the other subcommands.

## Project layout

- `yask/store.py` — all domain logic (projects, numbering, epic trees,
  prerequisite cascade, archiving, ordering, history, attachments)
- `yask/api.py` — REST API; also serves the web UI
- `yask/web/` — web UI (vanilla JS modules, no build step)
- `yask/mcp_server.py` — MCP tool surface
- `yask/telegram_bot.py` — Telegram bot process (Bot API client, poll loop, command dispatch)
- `yask/cli.py` — `yask serve` / `yask mcp` / `yask telegram`
- `tests/` — pytest suite
- `flake.nix` / `package.nix` — packaging and dev environment
