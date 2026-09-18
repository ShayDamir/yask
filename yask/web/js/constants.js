// GENERATED FILE — do not edit by hand.
//
// Single-sourced from the Python constants (yask/db.py, yask/spec.py);
// the generator is yask/codegen.py. Regenerate with:
//
//     nix develop -c python -m yask.codegen
//
// Every export below is a JSON literal so tests/test_codegen.py can parse
// this file back and assert value-equality against the Python source.

export const WORKFLOW_STATES = ["Backlog", "Todo", "Planning", "In progress", "Review", "Done"];
export const DEFAULT_STATE = "Backlog";
export const BLOCKED_STATE = "Blocked";
export const ARCHIVED_STATE = "Archived";
export const ALL_STATES = ["Backlog", "Todo", "Planning", "In progress", "Review", "Done", "Blocked", "Archived"];
export const IN_PROGRESS_STATES = ["Todo", "Planning", "In progress", "Review"];
export const COLOR_HEX_RE = "^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$";
export const MARKDOWN_REGEXES = {"code_span": "`([^`]+)`", "strong_star": "\\*\\*([^*]+)\\*\\*", "em_star": "(^|[^*])\\*([^*\\n]+)\\*", "strike": "~~([^~]+)~~", "link": "\\[([^\\]]+)\\]\\((https?:[^)\\s]+)\\)"};
export const FIELD_LIMITS = {"title": 255, "description": 65536, "projectName": 255, "taskTypeName": 255, "labelName": 64, "roleName": 64, "attachmentFilename": 255, "telegramPassword": 256};
