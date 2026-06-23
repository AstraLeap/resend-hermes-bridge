#!/usr/bin/env python3
"""Host-side proxy that exposes host Hermes commands over HTTP for the container.

Listen on 127.0.0.1:18765 by default. The container hermes wrapper forwards
`hermes send --to TARGET MESSAGE` here, and this process executes the real
host Hermes binary. The bridge also posts task prompts to /task, which starts a
fresh non-interactive Hermes Agent session and returns the final response.

Set HERMES_PROXY_SECRET to the same value used by the container. By default the
container wrapper reuses RESEND_BRIDGE_SEND_SECRET.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HOST = os.getenv("HERMES_PROXY_HOST", "127.0.0.1")
PORT = int(os.getenv("HERMES_PROXY_PORT", "18765"))
SECRET = os.getenv("HERMES_PROXY_SECRET") or os.getenv("RESEND_BRIDGE_SEND_SECRET", "")
HERMES_BIN = os.getenv("HERMES_SEND_BIN", "")


def find_hermes() -> str:
    if HERMES_BIN:
        return HERMES_BIN
    for candidate in ("hermes",):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    candidates = [
        Path.home() / ".local" / "bin" / "hermes",
        Path("/usr/local/bin/hermes"),
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    raise RuntimeError("hermes binary not found; set HERMES_SEND_BIN")


async def run_hermes_send(target: str, message: str, subject: str, quiet: bool, json_output: bool) -> dict:
    args = [find_hermes(), "send", "--to", target]
    if subject:
        args += ["--subject", subject]
    if quiet:
        args.append("--quiet")
    if json_output:
        args.append("--json")
    if message:
        args.append(message)

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=90)
    return {
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "returncode": proc.returncode,
    }


async def run_hermes_task(prompt: str, timeout: float) -> dict:
    args = [
        find_hermes(),
        "chat",
        "--query",
        prompt,
        "--quiet",
        "--source",
        "tool",
        "--yolo",
    ]

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return {
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "returncode": proc.returncode,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        pass

    def _unauthorized(self) -> None:
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": "unauthorized"}).encode("utf-8"))

    def _error(self, code: int, message: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": message}).encode("utf-8"))

    def _ok(self, payload: dict) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _check_auth(self) -> bool:
        if not SECRET:
            return True
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {SECRET}"

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            raise ValueError("empty body")
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        return body

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in {"/send", "/task"}:
            self._error(404, "not found")
            return
        if not self._check_auth():
            self._unauthorized()
            return

        try:
            body = self._read_json_body()
        except Exception as exc:
            self._error(400, f"invalid json: {exc}")
            return

        if parsed.path == "/task":
            self._handle_task(body)
            return

        self._handle_send(body)

    def _handle_send(self, body: dict) -> None:
        target = str(body.get("target") or "").strip()
        message = str(body.get("message") or "").strip()
        subject = str(body.get("subject") or "").strip()
        quiet = bool(body.get("quiet"))
        json_output = bool(body.get("json"))

        if not target:
            self._error(400, "missing target")
            return

        try:
            result = asyncio.run(run_hermes_send(target, message, subject, quiet, json_output))
        except Exception as exc:
            self._error(502, f"hermes send failed: {exc}")
            return

        self._ok(result)

    def _handle_task(self, body: dict) -> None:
        prompt = str(body.get("prompt") or "")
        if not prompt.strip():
            self._error(400, "missing prompt")
            return
        try:
            timeout = float(
                body.get("timeout") or os.getenv("HERMES_PROXY_TASK_TIMEOUT", "180")
            )
        except (TypeError, ValueError):
            timeout = 180.0

        try:
            result = asyncio.run(run_hermes_task(prompt, timeout))
        except Exception as exc:
            self._error(502, f"hermes task failed: {exc}")
            return

        self._ok(result)


def main() -> int:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"hermes-send-proxy listening on http://{HOST}:{PORT}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
