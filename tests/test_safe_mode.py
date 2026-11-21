"""Выбор режима через обычную консольную точку входа."""

import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from mutagen.id3 import ID3, TALB

import main as cli
from audio_verification import audio_signature, find_ffprobe
from recovery_store import DIRECTORY_NAME
from tag_repository import AudioTags, TagRepository
from tests.test_tag_publication import synthetic_audio, windows
from user_settings import load_mode, save_mode, settings_path


@pytest.fixture
def cli_prompts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Mock:
    monkeypatch.setattr(cli, '__file__', str(tmp_path / 'main.py'))
    monkeypatch.setattr(sys, 'argv', ['main.py'])
    prompt = Mock()
    monkeypatch.setattr('builtins.input', prompt)
    return prompt


def test_mode_lifecycle(
    cli_prompts: Mock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = settings_path()
    enter_collection = Mock(side_effect=EOFError)
    monkeypatch.setattr(cli, 'get_valid_path', enter_collection)
    for answers, starts, preferred in [
        ([''], True, 'safe'),
        ([''], True, 'safe'),
        (['deep', 'deep'], False, 'deep'),
        ([''], False, 'deep'),
        (['once'], True, 'deep'),
        (['safe'], True, 'safe'),
    ]:
        enter_collection.reset_mock()
        cli_prompts.side_effect = answers
        if starts:
            with pytest.raises(EOFError):
                cli.main()
        else:
            cli.main()
        assert enter_collection.call_count == int(starts)
        assert load_mode(path) == preferred
        output = capsys.readouterr().out
        if answers in (['deep', 'deep'], ['once']):
            assert 'безвозвратное удаление' in output
        if answers == ['once']:
            assert 'Сохранённое предпочтение остаётся прежним' in output


@pytest.mark.parametrize('answer', ['', 'cancel', EOFError(), KeyboardInterrupt()])
def test_deep_exit_never_reaches_recovery(
    answer: str | BaseException, cli_prompts: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = settings_path()
    save_mode(path, 'deep')
    original = path.read_bytes()
    cli_prompts.side_effect = [answer]
    get_path = Mock(side_effect=AssertionError('collection'))
    store = Mock(side_effect=AssertionError('recovery'))
    monkeypatch.setattr(cli, 'get_valid_path', get_path)
    monkeypatch.setattr(cli, 'RecoveryStore', store)
    cli.main()
    assert not get_path.called and not store.called
    assert path.read_bytes() == original


@pytest.mark.parametrize('answer', ['cancel', EOFError(), KeyboardInterrupt()])
def test_cancel_deep_selection_preserves_safe(
    answer: str | BaseException, cli_prompts: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = settings_path()
    save_mode(path, 'safe')
    cli_prompts.side_effect = ['DEEP', answer]
    monkeypatch.setattr(cli, 'get_valid_path', Mock(side_effect=AssertionError))
    cli.main()
    assert load_mode(path) == 'safe'


@pytest.mark.parametrize('payload', ['{', '{"preferred_mode": "other"}', None])
def test_bad_settings_offer_safe(
    payload: str | None,
    cli_prompts: Mock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = settings_path()
    if payload is None:
        monkeypatch.setattr(
            Path, 'read_text', Mock(side_effect=PermissionError('denied'))
        )
    else:
        path.parent.mkdir(parents=True)
        path.write_text(payload, encoding='utf-8')
    cli_prompts.side_effect = ['']
    monkeypatch.setattr(cli, 'get_valid_path', Mock(side_effect=EOFError))
    with pytest.raises(EOFError):
        cli.main()
    output = capsys.readouterr().out
    assert 'Не удалось прочитать настройки' in output
    assert 'Выбран безопасный режим' in output


def test_failed_replace_keeps_previous_preference(
    cli_prompts: Mock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = settings_path()
    save_mode(path, 'safe')
    original = path.read_bytes()
    cli_prompts.side_effect = ['deep', 'deep']
    monkeypatch.setattr(Path, 'replace', Mock(side_effect=PermissionError('denied')))
    cli.main()
    output = capsys.readouterr().out
    assert 'Не удалось сохранить предпочтение' in output
    assert 'Сохранено предпочтение' not in output
    assert path.read_bytes() == original
    assert list(path.parent.iterdir()) == [path]


def test_dry_run_does_not_create_settings(
    cli_prompts: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, 'argv', ['main.py', '--dry-run'])
    cli_prompts.side_effect = ['deep', 'deep']
    cli.main()
    assert not settings_path().parent.exists()


@windows
@pytest.mark.parametrize('refuse_write', [False, True])
def test_safe_manual_tags_on_disk(
    refuse_write: bool,
    tmp_path: Path,
    cli_prompts: Mock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / 'music'
    root.mkdir()
    source = root / 'Track.mp3'
    synthetic_audio(source, artist='', title='Original (Live)')
    album = ID3(source)
    album.add(TALB(encoding=3, text=['Album']))
    album.save(source)
    original = source.read_bytes()
    signature = audio_signature(find_ffprobe(), source)
    cli_prompts.side_effect = ['', str(root), 'y', 'McCartney']
    if refuse_write:
        monkeypatch.setattr(
            TagRepository, 'save_work_tags', Mock(side_effect=PermissionError('denied'))
        )

    cli.main()

    files = list(root.glob('*.mp3'))
    assert not list((root / DIRECTORY_NAME).glob('*.json'))
    if refuse_write:
        assert files == [source]
        assert source.read_bytes() == original
        assert 'denied' in capsys.readouterr().out
        return
    target = root / 'McCartney - Original (Live).mp3'
    assert files == [target]
    assert TagRepository().read_tags_uncached(target).tags == AudioTags(
        'McCartney', 'Original (Live)'
    )
    assert str(ID3(target)['TALB']) == 'Album'
    assert audio_signature(find_ffprobe(), target) == signature


def test_safe_dry_run_keeps_collection_and_settings(
    tmp_path: Path, cli_prompts: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'music'
    root.mkdir()
    source = root / 'Track.mp3'
    synthetic_audio(source, artist='', title='Original (Live)')
    original = source.read_bytes()
    path = settings_path()
    save_mode(path, 'deep')
    settings_before = path.read_bytes()
    monkeypatch.setattr(sys, 'argv', ['main.py', '--dry-run'])
    cli_prompts.side_effect = ['safe', str(root), 'y']
    if os.name == 'nt':
        cli_prompts.side_effect = ['safe', str(root), 'y', 'McCartney']
    cli.main()
    assert list(root.iterdir()) == [source]
    assert source.read_bytes() == original
    assert list(path.parent.iterdir()) == [path]
    assert path.read_bytes() == settings_before
