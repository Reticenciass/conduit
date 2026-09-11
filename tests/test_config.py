from pathlib import Path

from ctfws.core.config import AppConfig, load_config, save_config, update_config


def test_config_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    save_config(AppConfig(theme="matrix", editor="vim", terminal="tmux", auto_save=False), path)

    loaded = load_config(path)
    updated = update_config("theme", "light", path)

    assert loaded.theme == "matrix"
    assert loaded.auto_save is False
    assert updated.theme == "light"
