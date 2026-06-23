from __future__ import annotations

from functools import lru_cache

from bridge_settings import APP_DIR


@lru_cache(maxsize=8)
def load_prompt_template(name: str) -> str:
    path = APP_DIR / "prompts" / name
    return path.read_text(encoding="utf-8")
