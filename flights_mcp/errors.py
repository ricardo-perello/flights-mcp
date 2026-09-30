"""Structured tool failures.

The MCP SDK turns a ToolError into a result with is_error set. The message is
JSON so a client can read the code without scraping prose. Prices are never
part of an error.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError


class FlightToolError(ToolError):
    """A search or lookup failure the caller can correct or retry."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        payload: dict[str, Any] = {"code": code, "message": message}
        for key, value in details.items():
            if value is not None:
                payload[key] = value
        super().__init__(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        self.code = code
        self.payload = payload
