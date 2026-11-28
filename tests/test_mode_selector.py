"""Проверки терминального выбора и построчного протокола CLI."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

import interactor as ui
from user_settings import save_mode, settings_path


@pytest.mark.parametrize(
    'keys, expected',
    [
        (['\r'], 'once'),
        (['\x1b', '[', 'B', '\n'], 'safe'),
        (['\x1b', 'O', 'B', '\x1b', '[', 'A', '\n'], 'once'),
    ],
)
def test_arrow_choice(keys: list[str], expected: str) -> None:
    assert ui._navigate(Mock(side_effect=keys), windows=False) == expected


@pytest.mark.parametrize('tail', ['X', '', OSError('read'), ValueError('closed')])
def test_escape_failure_cancels_without_line_input(
    monkeypatch: pytest.MonkeyPatch, tail: object
) -> None:
    def interrupted() -> str:
        return ui._navigate(Mock(side_effect=['\x1b', '[', tail]), windows=False)

    monkeypatch.setattr(ui, '_arrow_action', interrupted)
    monkeypatch.setattr('builtins.input', Mock(side_effect=AssertionError))
    assert ui.choose_mode(ui.ConsoleInteractor(), dry_run=False) is None
    assert not settings_path().exists()


def test_terminal_failure_restarts_at_safe_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ui, '_arrow_action', Mock(side_effect=OSError('terminal')))
    monkeypatch.setattr('builtins.input', Mock(return_value=''))
    assert ui.choose_mode(ui.ConsoleInteractor(), dry_run=False) == 'safe'
    assert not settings_path().exists()


@pytest.mark.parametrize('stream', ['stdin', 'stdout'])
def test_redirected_stream_selects_line_menu(
    monkeypatch: pytest.MonkeyPatch, stream: str
) -> None:
    monkeypatch.setattr(sys, stream, Mock(isatty=lambda: False))
    assert ui._arrow_action() is None


def test_invalid_console_handle_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform != 'win32':
        pytest.skip('требуется Windows Console API')
    import msvcrt

    stream = Mock(isatty=lambda: True, fileno=lambda: 0)
    with monkeypatch.context() as m:
        m.setattr(sys, 'stdin', stream)
        m.setattr(sys, 'stdout', stream)
        m.setattr(msvcrt, 'get_osfhandle', lambda fd: 0)
        assert ui._arrow_action() is None


@pytest.mark.parametrize('result', ['once', EOFError(), KeyboardInterrupt()])
def test_terminal_restored(
    monkeypatch: pytest.MonkeyPatch, result: str | BaseException
) -> None:
    if sys.platform == 'win32':
        pytest.skip('требуются POSIX PTY и termios')
    import pty
    import termios

    master, slave = pty.openpty()
    try:
        before = termios.tcgetattr(slave)

        def navigate(*args: object, **kwargs: object) -> str:
            assert not termios.tcgetattr(slave)[3] & termios.ICANON
            if isinstance(result, BaseException):
                raise result
            return result

        with monkeypatch.context() as m:
            m.setattr(sys, 'stdin', Mock(isatty=lambda: True, fileno=lambda: slave))
            m.setattr(sys, 'stdout', Mock(isatty=lambda: True))
            m.setattr(ui, '_navigate', navigate)
            if isinstance(result, BaseException):
                with pytest.raises(type(result)):
                    ui._arrow_action()
            else:
                assert ui._arrow_action() == result
        assert termios.tcgetattr(slave) == before
    finally:
        os.close(slave)
        os.close(master)


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
    monkeypatch.setattr(ui, '_arrow_action', lambda: None)
    monkeypatch.setattr('builtins.input', lambda _: '')
    assert ui.choose_mode(ui.ConsoleInteractor(), dry_run=dry_run) == 'safe'
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
        assert '1. Safe один раз' in text
        assert f'Выбор режима: {action}. Запуск: отмена.' in text
        assert 'Введите путь к папке' not in text
    assert not settings_path().exists()
