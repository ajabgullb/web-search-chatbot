"""Smoke-test the project's MCP servers: `make mcp-check` (docs/mcp.md).

Each server is started exactly as `.mcp.json` says, spoken to over stdio the
way an MCP client would, and asked to do one real thing. The Postgres check
also proves the boundary the whole setup relies on: the role it logs in as can
count runs but is refused message content.

The first run downloads the servers (Toolbox's binary alone takes about a
minute), which is longer than Claude Code waits for a server to start. Running
this once warms those caches.

Standard library only, so it runs before any project environment exists.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_VERSION = "2025-06-18"
# Generous because the first launch downloads the server.
START_TIMEOUT_SECONDS = 240.0
CALL_TIMEOUT_SECONDS = 60.0


class McpError(Exception):
    pass


def env_value(key: str, default: str) -> str:
    """Shell environment, then .env, then the default, as scripts/mcp/_env.sh reads it."""
    if os.environ.get(key):
        return os.environ[key]
    value = ""
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if re.match(rf"^\s*{re.escape(key)}=", line):
                value = line.split("=", 1)[1]
    quoted = re.fullmatch(r"\"(.*)\"|'(.*)'", value)
    if quoted:
        value = quoted.group(1) if quoted.group(1) is not None else quoted.group(2)
    else:
        value = re.split(r"\s#", value, maxsplit=1)[0].strip()
    return value or default


class StdioClient:
    """The smallest MCP client that works: newline-delimited JSON-RPC over pipes."""

    def __init__(self, command: list[str], *, env: dict[str, str]) -> None:
        # Outlives this method, so not a `with` block: close() closes it.
        self.stderr = tempfile.TemporaryFile()  # noqa: SIM115
        # Started from backend/ on purpose: .mcp.json must work from any
        # directory in the repository, not only its root.
        # The command comes from the committed .mcp.json, never from input.
        self._process = subprocess.Popen(  # noqa: S603
            command,
            cwd=ROOT / "backend",
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            text=True,
        )
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._next_id = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _send(self, message: dict[str, Any]) -> None:
        assert self._process.stdin is not None
        self._process.stdin.write(json.dumps(message) + "\n")
        self._process.stdin.flush()

    def request(self, method: str, params: dict[str, Any], *, timeout: float) -> Any:
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            try:
                line = self._lines.get(timeout=timeout)
            except queue.Empty:
                raise McpError(f"no answer to {method} within {timeout:.0f}s") from None
            if line is None:
                raise McpError(f"server exited during {method}: {self.stderr_tail()}")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # Anything but JSON-RPC on stdout breaks a real client too.
                raise McpError(f"server wrote non-JSON to stdout: {line[:120]!r}") from None
            if message.get("id") != request_id:
                continue  # a notification, such as tools/list_changed
            if "error" in message:
                raise McpError(f"{method} failed: {message['error']}")
            return message["result"]

    def initialize(self) -> None:
        self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mcp-check", "version": "1"},
            },
            timeout=START_TIMEOUT_SECONDS,
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def tool_names(self) -> list[str]:
        result = self.request("tools/list", {}, timeout=CALL_TIMEOUT_SECONDS)
        return [tool["name"] for tool in result["tools"]]

    def call(self, tool: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        """Return (is_error, text) for one tool call."""
        result = self.request(
            "tools/call", {"name": tool, "arguments": arguments}, timeout=CALL_TIMEOUT_SECONDS
        )
        text = "\n".join(
            part.get("text", "") for part in result.get("content", []) if part.get("type") == "text"
        )
        return bool(result.get("isError")), text

    def stderr_tail(self) -> str:
        self.stderr.seek(0)
        return self.stderr.read().decode(errors="replace").strip()[-600:]

    def close(self) -> None:
        if self._process.stdin is not None:
            self._process.stdin.close()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()
        self.stderr.close()


def check_postgres(client: StdioClient) -> str:
    is_error, text = client.call(
        "execute_sql", {"sql": "SELECT current_user AS role, count(*) AS runs FROM runs"}
    )
    if is_error or '"role":"grafana_reader"' not in text.replace(" ", ""):
        raise McpError(f"expected to run as grafana_reader, got: {text[:300]}")
    # Toolbox returns one row as an object and several as a list.
    row = json.loads(text)
    runs = (row[0] if isinstance(row, list) else row).get("runs", "?")

    # The point of logging in as this role: content stays out of reach.
    is_error, text = client.call("execute_sql", {"sql": "SELECT content FROM messages LIMIT 1"})
    if not is_error or "permission denied" not in text:
        raise McpError(f"message content was NOT refused: {text[:300]}")
    return f"runs as grafana_reader ({runs} runs visible); message content refused"


def check_prometheus(client: StdioClient) -> str:
    is_error, text = client.call("execute_query", {"query": 'up{job="api"}'})
    if is_error:
        raise McpError(f"query failed: {text[:300]}")
    result = json.loads(text).get("result", [])
    if not result:
        raise McpError("Prometheus has no `api` target; is `make dev-observability` running?")
    state = "up" if result[0]["value"][1] == "1" else "DOWN"
    return f"api scrape target is {state}"


def check_playwright(client: StdioClient) -> str:
    url = f"http://localhost:{env_value('WEB_HOST_PORT', '5173')}/"
    is_error, text = client.call("browser_navigate", {"url": url})
    try:
        if is_error:
            raise McpError(f"could not open {url}: {text[:300]}")
        title = re.search(r"Page Title: (.*)", text)
        return f"opened {url} ({title.group(1) if title else 'no title'})"
    finally:
        client.call("browser_close", {})


CHECKS: dict[str, Callable[[StdioClient], str]] = {
    "postgres": check_postgres,
    "prometheus": check_prometheus,
    "playwright": check_playwright,
}


# What Claude Code needs set for the usage dashboard (docs/mcp.md#usage-dashboard).
TELEMETRY_SETTINGS = {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOG_TOOL_DETAILS": "1",
}


def check_usage_telemetry() -> str:
    """The collector behind the MCP usage dashboard accepts OTLP from this machine.

    Sends an empty batch, so nothing is counted. Whether Claude Code sends
    anything is set where Claude Code runs, so for this shell it is only reported.
    """
    port = env_value("OTLP_HTTP_HOST_PORT", "4318")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/logs",
        data=b'{"resourceLogs": []}',
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 (fixed local URL)
            status = response.status
    except (urllib.error.URLError, OSError) as exc:
        raise McpError(
            f"the collector isn't accepting OTLP on 127.0.0.1:{port} ({exc}); "
            "start it with `make dev-observability`"
        ) from None
    if status != 200:
        raise McpError(f"the collector answered {status}")
    missing = [key for key, value in TELEMETRY_SETTINGS.items() if os.environ.get(key) != value]
    shell = (
        "this shell has Claude Code telemetry on"
        if not missing
        else f"this shell does not set {', '.join(missing)}"
        " (fine if Claude Code gets them elsewhere)"
    )
    return f"collector accepts OTLP on 127.0.0.1:{port}; {shell}"


def main() -> int:
    servers = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]
    # Keep the smoke test from opening browser windows.
    env = {**os.environ, "PLAYWRIGHT_MCP_HEADLESS": "1"}
    failed = False
    for name, spec in servers.items():
        check = CHECKS.get(name)
        if check is None:
            print(f"?    {name}: no check defined in scripts/mcp/check.py")
            failed = True
            continue
        print(f"...  {name}: starting", flush=True)
        client = StdioClient([spec["command"], *spec.get("args", [])], env=env)
        try:
            client.initialize()
            tools = client.tool_names()
            detail = check(client)
            print(f"ok   {name}: {len(tools)} tools; {detail}")
        except (McpError, KeyError, ValueError) as exc:
            failed = True
            print(f"FAIL {name}: {exc}")
            tail = client.stderr_tail()
            if tail:
                print("     stderr: " + tail.replace("\n", "\n             "))
        finally:
            client.close()

    try:
        print(f"ok   usage dashboard: {check_usage_telemetry()}")
    except McpError as exc:
        failed = True
        print(f"FAIL usage dashboard: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
