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
* Tasks state: Backlog, Todo, Planning, In progress, Review, Done, Archived
* Any task can be archived at any time
* Archived tasks are not listed by default. They can be permanently deleted.
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

After MVP, the development of yask will dogfood itself to add more features
