"""Data-room MCP server: permission-aware, truncation-aware document access.

Stands in for the custom Box MCP from the design. Reads are paged; every read reports
`coverage` so the agent can never silently answer from part of a file.
"""

from __future__ import annotations

import json

from mcp.server.mcpserver import MCPServer

from .common import context_from_env, denials_as_tool_errors

access, ctx = context_from_env()
app = MCPServer("dataroom", log_level="ERROR", instructions="Data room for the deal being diligenced. "
                "Only files the requesting user may open are visible.")


@app.tool()
@denials_as_tool_errors
def list_files() -> str:
    """List data-room files the requesting user may open for this deal, with page counts."""
    return json.dumps(access.list_files(ctx))


@app.tool()
@denials_as_tool_errors
def get_file_manifest(file_id: str) -> str:
    """Return a file's title, as-of date, total pages and token estimate. Call before reading
    a long file so you can plan which pages to read."""
    return json.dumps(access.get_file_manifest(ctx, file_id))


@app.tool()
@denials_as_tool_errors
def read_file(file_id: str, start_page: int = 1, end_page: int | None = None) -> str:
    """Read pages [start_page, end_page] of a file (max 20 pages per call). The response's
    `coverage` says which pages were read, the total, and the next unread page. If
    `truncated` is true you have NOT seen the whole file."""
    return json.dumps(access.read_file(ctx, file_id, start_page, end_page))


@app.tool()
@denials_as_tool_errors
def search_files(query: str) -> str:
    """Keyword search across accessible files. Returns file_id, page and a snippet per hit.
    Snippets are leads only: read the page before citing it."""
    return json.dumps(access.search_files(ctx, query))


if __name__ == "__main__":
    app.run()
