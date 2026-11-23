---
name: chronix-cli
description: Reference for Chronix CLI commands, interactive mode, task operations, configuration, and companion server entrypoints. Use when invoking or interpreting Chronix.
---

# Chronix CLI Reference

## At a glance

- Covers the Chronix command-line program and its companion MCP/API server entrypoints.
- Running chronix with no arguments opens the interactive shell; chronix help prints the command catalog.
- Task commands can run interactively or as one-shot commands with explicit arguments.
- In one-shot mode, done, pause, and resume require --source; the interactive shell can resolve a task's source after sync.
- Configuration lives at ~/.config/chronix/config.toml.
- This is a tool reference. The central chronix-task-management skill retains task-planning and task-update workflows.

## Activation contract

Before acting under this skill, read this entire SKILL.md from the first line to the last. The summary above is orientation only; it does not replace the complete instructions below. Read the current repository documentation if a command's exact behavior is not covered here. If a required configuration, permission, or source is unavailable, stop and report the exact blocker.

## Entry points

| Executable | Purpose |
|---|---|
| chronix | Interactive task-management shell and one-shot command interface. |
| chronix-mcp | STDIO MCP server exposing Chronix tools. Install the package with its mcp extra when building this entrypoint. |
| chronix-api | HTTP API server for integrations. |

The CLI is installed from the Chronix Python package. The project declares Python >=3.9. The API server accepts --host and --port; defaults are 127.0.0.1 and 8766. CHRONIX_API_HOST, CHRONIX_API_PORT, and CHRONIX_API_LOG_LEVEL configure the API server defaults.

## Interactive and one-shot use

- chronix with no arguments starts the interactive shell.
- chronix help prints the available command catalog. The interactive shell accepts help and exit/quit.
- One-shot form is chronix COMMAND [ARGUMENTS].
- Interactive add, update, and meta open a full-screen form when required arguments are omitted. Tab/arrow keys move between fields, Ctrl+S submits, and Esc cancels.
- delete, done, undone, pause, and resume prompt for a task ID when one is omitted.
- In the interactive shell, --source can be omitted for task commands after a project has been synced; Chronix resolves the source from the task ID.
- In one-shot mode, done, pause, and resume require --source because there is no prior sync from which to resolve it.

## Command catalog

| Command | Syntax / purpose |
|---|---|
| Add | chronix add [<duration> <title>] - create a task; no arguments opens the interactive form. |
| Update | chronix update [<task_id>] [--source NAME] - edit task fields; no arguments or only an ID opens the form. |
| Delete | chronix delete [<task_id>] [--source NAME] - delete a task; no ID prompts and confirms. |
| Done / undone | chronix done [<task_id>] [--source NAME]; chronix undone [<task_id>] [--source NAME] - change completion state. |
| Pause / resume | chronix pause [<task_id>] [--source NAME]; chronix resume [<task_id>] [--source NAME] - close or open a work session. |
| Duration | chronix duration <task_id> <duration> [--source NAME] - set an estimate such as 2h or 30m. |
| Deadline | chronix deadline <task_id> <ISO|-> [--user] [--source NAME] - set an external deadline; --user sets the user deadline. |
| Deadline backfill | chronix deadlines <task_id|--source NAME|--all> [--dry-run] - exactly one scope is required. |
| Metadata | chronix meta [<task_id>] [--remove KEY] [--source NAME] - set or remove metadata. |
| Execution mode | chronix mode <task_id> <mode> [--source NAME] - mode is atomic, flex, or contiguous_preferred. |
| Track | chronix track <task_id> <auto|primary|secondary> [--source NAME] - choose the schedule track. |
| Rename | chronix rename <task_id> <title> [--source NAME] - rename a task. |
| Explain | chronix explain <task_id> - show task details and scheduling information. |
| Projects | chronix projects - list configured projects and their sources. |
| Project tasks | chronix project <name> [--page N] [--per-page N] [--status STATUS] - show paginated tasks. |
| Document tabs | chronix tabs <name> - list tabs in a project's Google Docs source. |
| Sync | chronix sync; chronix sync <name> [<name> ...] - sync all configured projects or selected projects. |
| Schedule | chronix schedule; chronix schedule <start>-<end>; chronix schedule from <day> - display a multi-day schedule, date range, or forecast. |
| Today | chronix today [HH:MM] [--split] - show today's schedule; --split separates primary and secondary tracks. |
| Calendar | chronix calendar [HH:MM] [--force] [--split] - sync the primary track to Google Calendar; --split also syncs the secondary track. |
| Conflicts | chronix conflicts - show tasks that disagree across sources and are excluded from scheduling until resolved. |
| Blocks | chronix blocks; chronix blocks pause <label|kind>; chronix blocks resume <label|kind|all> - inspect or pause/resume recurring time blocks for this session. |
| Config | chronix config <init|show|path|validate|reload> - manage configuration. |

## Configuration

- Default config: ~/.config/chronix/config.toml.
- chronix config init creates a default configuration.
- show prints the loaded configuration; path prints its path; validate checks it; reload reloads it into the current interactive session.
- Chronix loads config at REPL startup or on demand in one-shot mode. Edits made on disk do not change an already-running REPL until config reload is used.
- Project/source mappings and scheduling priorities are configured in config.toml; do not invent a project name or source when a task's identity is ambiguous.

## Companion server entrypoints

- chronix-mcp is a STDIO MCP server. It is intended to be launched by the MCP host's server configuration, not run as an interactive task shell.
- chronix-api is an HTTP API server. Defaults: 127.0.0.1:8766. Override with --host/--port or CHRONIX_API_HOST/CHRONIX_API_PORT; CHRONIX_API_LOG_LEVEL controls logging.
- Do not start duplicate server processes if the intended MCP/API service is already managed by the host.

## Related workflow skill

Use chronix-task-management in the central skills repository for task decomposition, planning, status updates, and deciding what belongs in Chronix. This skill documents the executable interface only.
