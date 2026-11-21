"""Изоляция пользовательских настроек, включая дочерние CLI-процессы."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_user_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_home = str(tmp_path / 'config')
    monkeypatch.setenv('LOCALAPPDATA', config_home)
    monkeypatch.setenv('XDG_CONFIG_HOME', config_home)
