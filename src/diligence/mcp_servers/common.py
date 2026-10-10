"""Shared setup for the MCP servers: identity comes from the launching process.

Each run starts its own server processes with the *requesting user's* identity in the
environment (standing in for a per-user OAuth token). The servers hold no broad
credentials and cannot act for anyone else.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Callable

from mcp.server.mcpserver.exceptions import ToolError

from ..config import load_config
from ..data_access import AccessContext, AccessDenied, DataAccess
from ..ledger import Ledger


def denials_as_tool_errors(fn: Callable) -> Callable:
    """Surface an access denial to the model as a plain tool error it can report."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except AccessDenied as e:
            raise ToolError(f"Access denied: {e}") from None

    return wrapper


def context_from_env() -> tuple[DataAccess, AccessContext]:
    cfg = load_config()
    ledger = Ledger(Path(os.environ["DILIGENCE_DB"]) if "DILIGENCE_DB" in os.environ else cfg.path("db"))
    ctx = AccessContext(
        user=os.environ["DILIGENCE_USER"],
        deal_id=os.environ["DILIGENCE_DEAL"],
        run_id=os.environ.get("DILIGENCE_RUN", "adhoc"),
        actor=os.environ.get("DILIGENCE_ACTOR", "agent"),
    )
    return DataAccess(cfg, ledger), ctx
