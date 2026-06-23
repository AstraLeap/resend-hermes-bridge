# AGENTS.md

Repository notes for future work:

- Use `./scripts/test.sh` as the default test entrypoint.
- The script creates and reuses `.test-venv`, installs `requirements.txt` plus `requirements-dev.txt`, and runs `pytest`.
- Do not rely on the system Python for tests.
- If you need a specific interpreter or venv, set `TEST_PYTHON=/path/to/python` or `TEST_VENV=/path/to/venv` before running the test script.
- `resend-hermes-bridge.service` runs from `.venv`; do not use the bridge runtime venv as the default test environment.
- `resend_mcp_server.py` is launched by the Hermes agent's own venv and should not be confused with either `.venv` or `.test-venv`.
- If you modify related files, remember to restart both `resend-hermes-bridge.service` and `hermes-gateway.service`.
