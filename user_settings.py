"""Предпочтительный режим пользователя."""

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

type Mode = Literal['safe', 'deep']


def settings_path() -> Path:
    if os.name == 'nt':
        root = os.environ.get('LOCALAPPDATA')
        if not root:
            raise OSError('переменная LOCALAPPDATA не задана')
        config_home = Path(root)
    else:
        root = os.environ.get('XDG_CONFIG_HOME')
        config_home = Path(root) if root else Path.home() / '.config'
    return config_home / 'clean_music' / 'settings.json'


def load_mode(path: Path) -> Mode | None:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    mode = payload.get('preferred_mode') if isinstance(payload, dict) else None
    if mode == 'safe':
        return 'safe'
    if mode == 'deep':
        return 'deep'
    raise ValueError('неизвестное значение preferred_mode')


def save_mode(path: Path, mode: Mode) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(
            mode='w', encoding='utf-8', dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump({'preferred_mode': mode}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
