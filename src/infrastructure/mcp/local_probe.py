"""仅供本机集成验收的独立 MCP 进程探针，不进入生产服务装配。"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx

from application.common.context import TraceContext
from infrastructure.mcp.client import MCPToolClient

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SERVER_START_TIMEOUT_S = 60.0


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def probe_trace(tag: str) -> TraceContext:
    run = uuid.uuid4().hex[:8]
    return TraceContext(
        schema_version="1.0",
        request_id=f"req-e2e-{tag}-{run}",
        correlation_id=f"corr-e2e-{tag}-{run}",
        trace_id=f"trace-e2e-{tag}-{run}",
        thread_id=None,
    )


class MCPServerProcess:
    def __init__(self, port: int, *, knowledge_enabled: bool = False) -> None:
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        log_file = tempfile.NamedTemporaryFile(
            prefix="mcp-e2e-", suffix=".log", delete=False, mode="w", encoding="utf-8"
        )
        self.log_path = log_file.name
        log_file.close()
        env = dict(os.environ)
        env.update(
            {
                "MCP_SERVER_HOST": "127.0.0.1",
                "MCP_SERVER_PORT": str(port),
                "PYTHONIOENCODING": "utf-8",
                "KNOWLEDGE_LOCAL_TOOLS_ENABLED": "true" if knowledge_enabled else "false",
            }
        )
        if not knowledge_enabled:
            env["NEO4J_QUERY_PASSWORD"] = ""
        with open(self.log_path, "w", encoding="utf-8") as output:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "bootstrap.mcp_main"],
                cwd=str(PROJECT_ROOT),
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
            )

    def wait_ready(self) -> None:
        deadline = time.monotonic() + SERVER_START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                tail = Path(self.log_path).read_text(encoding="utf-8", errors="replace")[-2000:]
                raise RuntimeError(f"MCP 进程提前退出 code={self.process.returncode}，日志: {tail}")
            try:
                response = httpx.get(f"{self.base}/health/live", timeout=1.0)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        tail = Path(self.log_path).read_text(encoding="utf-8", errors="replace")[-2000:]
        raise RuntimeError(f"MCP 进程 {SERVER_START_TIMEOUT_S}s 内未就绪，日志: {tail}")

    def login(self, account_id: str) -> str:
        response = httpx.post(
            f"{self.base}/auth/mock/session", json={"account_id": account_id}, timeout=5.0
        )
        assert response.status_code == 200, response.text
        return response.json()["data"]["session_token"]

    def call(self, tool_name: str, arguments: dict, *, token: str, trace: TraceContext) -> dict:
        client = MCPToolClient(f"{self.base}/mcp")
        return asyncio.run(client.call_tool(tool_name, arguments, session_token=token, trace=trace))

    def stop(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
