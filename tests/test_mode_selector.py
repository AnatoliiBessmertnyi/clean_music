"""Проверки терминального выбора и построчного протокола CLI."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import interactor as ui
from user_settings import save_mode, settings_path


@pytest.mark.parametrize('preferred', [None, 'safe', 'deep', 'broken'])
@pytest.mark.parametrize('dry_run', [True, False])
def test_default_preserves_settings(
    preferred: str | None, dry_run: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = settings_path()
    if preferred in ('safe', 'deep'):
        save_mode(path, 'safe' if preferred == 'safe' else 'deep')
    elif preferred:
        path.parent.mkdir(parents=True)
        path.write_bytes(b'broken')
    before = path.read_bytes() if path.exists() else None
    monkeypatch.setattr('builtins.input', lambda _: '')
    choice = ui.choose_mode(ui.ConsoleInteractor(), dry_run=dry_run)
    assert choice and choice.mode == 'safe' and not choice.remember
    assert (path.read_bytes() if path.exists() else None) == before


@pytest.mark.parametrize(
    'answers,action',
    [
        ('3\n3\n\x1b[B\n\n', 'cancel'),
        ('5\n', 'cancel'),
        ('4\n', 'remember_deep'),
        ('', 'cancel'),
        ('wrong\nCANCEL\n', 'cancel'),
        ('deep\ndeep\n', 'remember_deep'),
    ],
)
def test_piped_cli_records_decision(tmp_path: Path, answers: str, action: str) -> None:
    code = 'import main,sys; main.__file__=sys.argv.pop(1); main.main()'
    result = subprocess.run(
        [sys.executable, '-c', code, str(tmp_path / 'main.py'), '--dry-run'],
        input=answers,
        capture_output=True,
        encoding='utf8',
        timeout=10,
        env={**os.environ, 'PYTHONIOENCODING': 'utf8'},
    )
    assert result.returncode == 0, result.stderr
    [log] = (tmp_path / 'logs').glob('*.log')
    for text in (result.stdout, log.read_text(encoding='utf8')):
        assert '[1] Safe один раз' in text
        assert f'Выбор режима: {action}. Запуск: отмена.' in text
        assert '2/2 Укажите папку' not in text
    assert not settings_path().exists()


def test_pipe_preserves_next_answer(tmp_path: Path) -> None:
    folder = tmp_path / 'Case Sensitive'
    folder.mkdir()
    code = 'import main,sys; main.__file__=sys.argv.pop(1); main.main(); print(input())'
    result = subprocess.run(
        [sys.executable, '-c', code, str(tmp_path / 'main.py'), '--dry-run'],
        input=f'3\nrun\n"{folder}"\nNEXT ANSWER\n',
        capture_output=True,
        encoding='utf8',
        timeout=10,
        env={**os.environ, 'PYTHONIOENCODING': 'utf8'},
    )
    assert result.returncode == 0, result.stderr
    assert 'Найдено 0 аудиофайлов' in result.stdout
    assert result.stdout.rstrip().endswith('NEXT ANSWER')
    assert not settings_path().exists()
