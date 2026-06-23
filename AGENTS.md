# AGENTS.md

Repository notes for future work:

- Use `./scripts/test.sh` as the default test entrypoint.
- The script creates and reuses `.test-venv`, installs `requirements.txt` plus `requirements-dev.txt`, and runs `pytest`.
- Do not rely on the system Python for tests.
- If you need a specific interpreter or venv, set `TEST_PYTHON=/path/to/python` or `TEST_VENV=/path/to/venv` before running the test script.
- The bridge web service runs in Docker Compose.
- `.venv` is for host-side helper processes such as `hermes-send-proxy.service` and the MCP server command; do not use it as the default test environment.
- `resend_mcp_server.py` may be launched by Hermes or by `.venv` depending on deployment; keep it separate from `.test-venv`.
- If you modify bridge or proxy code, restart Docker Compose and `hermes-send-proxy.service`; restart `hermes-gateway.service` only when MCP/Hermes config changes require it.
