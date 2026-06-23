# Repository Guidelines

## Project Structure & Module Organization

This repository is a Python 3.11 FastAPI bridge for Resend inbound email and a local Hermes runtime. The main ASGI app and orchestration logic live in `app.py`; `manage.py` provides administrative CLI entry points. HTTP routes are split under `routers/`, domain services under `services/`, persistence helpers under `db/`, and shared email/notice utilities under `utils/`. Hermes task instructions are stored in `prompts/hermes_email_task.md`. Tests currently live in `tests/test_app.py`. Deployment and local helper scripts are in `scripts/`, including a systemd service template for host-native installation.

## Build, Test, and Development Commands

- `python3 -m venv .venv && . .venv/bin/activate`: create and enter a local development environment.
- `pip install -r requirements.txt -r requirements-dev.txt`: install runtime, test, and lint dependencies.
- `./scripts/test.sh`: run Ruff and pytest using `.test-venv`; this is the preferred pre-commit check.
- `python scripts/send_test_webhook.py`: exercise the local webhook path without a real inbound email.
- `uvicorn app:app --host 127.0.0.1 --port 8765`: run the bridge directly on the host.
- `systemctl --user enable --now resend-hermes-bridge.service`: run the bridge as a systemd user service.

## Coding Style & Naming Conventions

Use standard Python formatting with 4-space indentation, type hints where they clarify interfaces, and small functions around service boundaries. Ruff is configured in `pyproject.toml` with a 100-character line length and rules for pycodestyle, pyflakes, import sorting, pyupgrade, and bugbear. Prefer `snake_case` for modules, functions, variables, and tests; use `PascalCase` for classes. Keep long Hermes prompts in `prompts/`, not inline Python strings.

## Testing Guidelines

Pytest is the test framework. Add new tests in `tests/test_app.py` or split into `tests/test_*.py` as coverage grows. Name tests `test_<behavior>`, and use fixtures or monkeypatching for Resend, Hermes, filesystem, and environment side effects. Run `./scripts/test.sh` before submitting changes; it also validates linting.

## Commit & Pull Request Guidelines

Recent history uses concise imperative subjects, with conventional prefixes when useful, for example `feat: ...`, `refactor: ...`, `docs: ...`, and `chore: ...`. Keep commits focused and avoid mixing unrelated cleanup with behavior changes. Pull requests should include a short summary, configuration or migration notes, test results, and linked issues when applicable. Include screenshots only for user-visible rendered output.

## Security & Configuration Tips

Never commit `.env`, `state.db`, `attachments/`, `data/`, or MCP draft files. Generate secrets with `openssl rand -hex 32`. Keep the service bound to loopback and expose only the Resend webhook path through a reverse proxy.
