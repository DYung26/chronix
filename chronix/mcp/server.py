"""stdio MCP server exposing chronix as tools.

Sync model
----------
This server keeps `chronix.cli.commands._context` warm for its entire
process lifetime, the same way the interactive REPL keeps it warm for a
session -- there is no per-call re-sync and no auto-sync on startup. `sync`
is an ordinary tool: call it once near the start of a session, or again
whenever Google Docs may have changed outside of this server (e.g. edited
directly, or by another client). Every write tool (add_task, update_task,
mark_done, etc.) refreshes its own document's state in-context immediately
after writing, so results stay consistent without an explicit `sync` after
every edit.

Interactive-only behavior from the CLI (full-screen forms, prompts for a
missing task_id) does not apply here: every write tool requires its
arguments explicitly, and returns a structured `missing_required_fields`
error (see chronix.mcp.errors) when something needed is absent, so the
calling model can gather it and retry.
"""

from mcp.server.mcpserver import MCPServer

from chronix.mcp import tools_read, tools_write

_INSTRUCTIONS = """\
chronix manages tasks synced from Google Docs and schedules them against
configured work hours, breaks, sleep, and meetings.

Sync is explicit and the server keeps synced state in memory for the whole
session: call `sync` once before using `today`, `schedule`, `explain`,
`document`, or `deadlines_preview`/`deadlines_apply` for the first time, and
again only if Google Docs may have changed outside this server. Write tools
(add_task, update_task, mark_done, pause_task, resume_task, mark_undone,
delete_task, rename_task, set_duration, set_deadline, set_mode, set_track,
set_metadata, deadlines_apply) refresh their own document automatically, so
no re-sync is needed immediately after using them.

All write tools require their fields explicitly; none of them prompt. A
missing required field comes back as a structured error describing what's
needed -- ask the user for it and retry rather than guessing.
"""

mcp = MCPServer("chronix", instructions=_INSTRUCTIONS)

mcp.tool()(tools_read.sync)
mcp.tool()(tools_read.today)
mcp.tool()(tools_read.schedule)
mcp.tool()(tools_read.explain)
mcp.tool()(tools_read.documents)
mcp.tool()(tools_read.document)
mcp.tool()(tools_read.tabs)
mcp.tool()(tools_read.blocks)
mcp.tool()(tools_read.deadlines_preview)

mcp.tool()(tools_write.add_task)
mcp.tool()(tools_write.update_task)
mcp.tool()(tools_write.rename_task)
mcp.tool()(tools_write.set_duration)
mcp.tool()(tools_write.set_deadline)
mcp.tool()(tools_write.set_mode)
mcp.tool()(tools_write.set_track)
mcp.tool()(tools_write.set_metadata)
mcp.tool()(tools_write.mark_done)
mcp.tool()(tools_write.pause_task)
mcp.tool()(tools_write.resume_task)
mcp.tool()(tools_write.mark_undone)
mcp.tool()(tools_write.delete_task)
mcp.tool()(tools_write.deadlines_apply)
mcp.tool()(tools_write.pause_block)
mcp.tool()(tools_write.resume_block)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
