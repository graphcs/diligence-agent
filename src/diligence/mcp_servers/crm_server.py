"""CRM MCP server: company directory plus deal records scoped to the requesting user."""

from __future__ import annotations

import json

from mcp.server.mcpserver import MCPServer

from .common import context_from_env, denials_as_tool_errors

access, ctx = context_from_env()
app = MCPServer("crm", log_level="ERROR", instructions="Firm CRM. Deal records are visible only to the deal team.")


@app.tool()
@denials_as_tool_errors
def lookup_company(query: str) -> str:
    """Find companies in the CRM by name, alias or domain. Returns candidates with stage."""
    return json.dumps(access.lookup_company(ctx, query))


@app.tool()
@denials_as_tool_errors
def get_deal(crm_id: str) -> str:
    """Return the deal record for a CRM company id, if the user is on the deal team."""
    return json.dumps(access.get_deal(ctx, crm_id))


if __name__ == "__main__":
    app.run()
