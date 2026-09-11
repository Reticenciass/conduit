"""User configuration stored outside individual lab databases."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Small, dependency-free configuration model."""

    theme: str = "dark"
    editor: str = "nvim"
    terminal: str = "tmux"
    auto_save: bool = True


def config_path() -> Path:
    """Return the platform-independent user config path."""

    return Path.home() / ".config" / "ctfws" / "config.toml"


def load_config(path: Path | None = None) -> AppConfig:
    """Load config or return safe defaults when it does not exist."""

    target = (path or config_path()).expanduser()
    if not target.is_file():
        return AppConfig()
    with target.open("rb") as handle:
        data = tomllib.load(handle)
    return AppConfig(
        theme=str(data.get("theme", "dark")),
        editor=str(data.get("editor", "nvim")),
        terminal=str(data.get("terminal", "tmux")),
        auto_save=bool(data.get("auto_save", True)),
    )


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    """Write a minimal TOML configuration."""

    target = (path or config_path()).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f'theme = "{config.theme}"\n'
        f'editor = "{config.editor}"\n'
        f'terminal = "{config.terminal}"\n'
        f"auto_save = {'true' if config.auto_save else 'false'}\n",
        encoding="utf-8",
    )
    return target


def update_config(key: str, value: str, path: Path | None = None) -> AppConfig:
    """Update one supported setting and persist it."""

    current = load_config(path)
    if key == "theme":
        if value not in {"dark", "light", "matrix", "minimal"}:
            raise ValueError("theme deve ser dark, light, matrix ou minimal.")
        updated = AppConfig(value, current.editor, current.terminal, current.auto_save)
    elif key == "editor":
        updated = AppConfig(current.theme, value, current.terminal, current.auto_save)
    elif key == "terminal":
        updated = AppConfig(current.theme, current.editor, value, current.auto_save)
    elif key == "auto_save":
        if value.lower() not in {"true", "false"}:
            raise ValueError("auto_save deve ser true ou false.")
        updated = AppConfig(
            current.theme, current.editor, current.terminal, value.lower() == "true"
        )
    else:
        raise ValueError(f"Configuração desconhecida: {key}")
    save_config(updated, path)
    return updated
