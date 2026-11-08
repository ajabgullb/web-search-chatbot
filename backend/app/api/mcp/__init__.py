"""The MCP endpoint: the chatbot's services for MCP clients (ADR-0019)."""

from app.api.mcp.endpoint import McpEndpoint, mount_mcp

__all__ = ["McpEndpoint", "mount_mcp"]
