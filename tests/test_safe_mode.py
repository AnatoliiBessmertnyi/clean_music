"""Выбор режима через обычную консольную точку входа."""

import json
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from mutagen.id3 import ID3, TALB

import main as cli
from audio_verification import audio_signature, find_ffprobe
from context import SessionContext
from interactor import UserInteractor
from pipeline import AudioProcessingPipeline
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
    enter_collection = Mock(return_value=path.parents[2])
    monkeypatch.setattr(cli, '_process', Mock())
    # Настоящая сессия восстановления работает только во временной папке.
    monkeypatch.setattr(cli, 'windows_local_ntfs', lambda _: True)
    monkeypatch.setattr(cli, 'get_valid_path', enter_collection)
    for answers, starts, preferred in [
        ([''], True, None),
        (['safe'], True, 'safe'),
        ([''], True, 'safe'),
        (['deep', 'deep'], False, 'deep'),
        (['run'], True, 'deep'),
        (['', ''], False, 'deep'),
        (['', 'run'], True, 'deep'),
        (['once'], True, 'deep'),
        (['safe'], True, 'safe'),
    ]:
        enter_collection.reset_mock()
        cli_prompts.side_effect = answers
        cli.main()
        assert enter_collection.call_count == int(starts)
        assert load_mode(path) == preferred
        output = capsys.readouterr().out
        if answers in (['deep', 'deep'], ['run'], ['once']):
            assert 'Точные дубли будут удалены безвозвратно' in output
        if answers == ['once']:
            assert 'Сохранённое предпочтение остаётся прежним' in output


@pytest.mark.parametrize('answer', ['cancel', EOFError(), KeyboardInterrupt()])
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


@windows
@pytest.mark.parametrize('deep', [False, True], ids=['safe', 'deep'])
def test_tag_model_matches_successful_cli_run(
    deep: bool,
    tmp_path: Path,
    cli_prompts: Mock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dry_root, real_root = tmp_path / 'plan', tmp_path / 'apply'
    dry_root.mkdir()
    synthetic_audio(dry_root / 'Artist – Song.mp3', artist='', title='')
    synthetic_audio(
        dry_root / 'Artist - Song.flac', codec='flac', artist='Artist', title='Song'
    )
    synthetic_audio(dry_root / 'Other – Clash.mp3', artist='Other', title='Clash')
    shutil.copyfile(dry_root / 'Other – Clash.mp3', dry_root / 'Other - Clash.mp3')
    shutil.copytree(dry_root, real_root)
    path = settings_path()
    save_mode(path, 'deep')

    def snapshot(root: Path) -> dict[Path, bytes | None]:
        return {
            p.relative_to(root): None if p.is_dir() else p.read_bytes()
            for p in root.rglob('*')
        }

    before = snapshot(dry_root)
    assert before == snapshot(real_root)
    ffprobe = find_ffprobe()
    original_audio = audio_signature(ffprobe, real_root / 'Artist – Song.mp3')
    settings_before = snapshot(path.parent)
    plan_tags: dict[str, AudioTags | None] = {}
    run_pipeline = AudioProcessingPipeline.run

    def inspect_plan(
        self: AudioProcessingPipeline,
        initial_files: list[Path],
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> list[Path]:
        files = run_pipeline(self, initial_files, ctx, repo, interactor)
        if ctx.dry_run:
            plan_tags.update((p.name, repo.get_tags(p).tags) for p in files)
            assert not any(ctx.stats.values())
            assert ctx.planned_stats['dash_normalized'] == 1
            assert ctx.planned_stats['tags_written'] == 1
        return files

    monkeypatch.setattr(AudioProcessingPipeline, 'run', inspect_plan)
    logs = tmp_path / 'logs'

    def run(root: Path, *, dry_run: bool) -> list[dict[str, str | bool]]:
        cli_prompts.reset_mock()
        cli_prompts.side_effect = ['run' if deep else 'safe', str(root), 'y', 'y', 'y']
        args = ['main.py', '--dry-run'] if dry_run else ['main.py']
        monkeypatch.setattr(sys, 'argv', args)
        existing = set(logs.glob('run-*.log'))
        cli.main()
        [report] = set(logs.glob('run-*.log')) - existing
        output = capsys.readouterr().out
        assert str(report) in output
        assert "('artist', 'song')" in output
        assert ('Будущие размер и байты неизвестны' in output) == dry_run
        assert 'Удалено дубликатов: 0' in output
        return [
            json.loads(line)
            for line in report.read_text(encoding='utf-8').splitlines()
            if line.startswith('{')
        ]

    planned = run(dry_root, dry_run=True)
    plan_prompts = cli_prompts.call_args_list.copy()
    assert snapshot(dry_root) == before
    assert snapshot(path.parent) == settings_before
    assert not (dry_root / DIRECTORY_NAME).exists()
    confirmed = run(real_root, dry_run=False)
    assert cli_prompts.call_args_list == plan_prompts
    assert not list((real_root / DIRECTORY_NAME).glob('*.json'))
    result = real_root / 'Artist - Song.mp3'
    assert result.read_bytes() != before[Path('Artist – Song.mp3')]
    assert audio_signature(ffprobe, result) == original_audio
    for name in ('Artist - Song.flac', 'Other – Clash.mp3', 'Other - Clash.mp3'):
        assert (real_root / name).read_bytes() == before[Path(name)]

    def operations(
        records: list[dict[str, str | bool]], *, dry_run: bool
    ) -> list[tuple[object, ...]]:
        entries: list[tuple[object, ...]] = []
        for op in records:
            if op['type'] != 'ОПЕРАЦИЯ' or op['kind'] not in (
                'rename',
                'tags',
                'block',
            ):
                continue
            assert op['proposed'] == dry_run
            result = op['result']
            changed = result in ('ПРЕДЛОЖЕНИЕ', 'успех')
            entries.append(
                (
                    op['stage'],
                    op['kind'],
                    Path(str(op['path'])).name,
                    Path(str(op['target'])).name if op['target'] else None,
                    'изменение' if changed else result,
                    None if changed else op['reason'],
                    op['artist'],
                    op['title'],
                )
            )
        return entries

    plan_operations = operations(planned, dry_run=True)
    assert plan_operations == operations(confirmed, dry_run=False)
    assert [op[1] for op in plan_operations if op[4] == 'изменение'] == [
        'rename',
        'tags',
    ]
    assert any(op[4] == 'конфликт' for op in plan_operations)
    assert {record['status'] for record in planned if record['type'] == 'ФАЙЛ'} == {
        'не изменён'
    }
    assert {record['status'] for record in confirmed if record['type'] == 'ФАЙЛ'} == {
        'не изменён',
        'переименован и изменено содержимое/теги',
    }
    repo = TagRepository()
    assert (
        plan_tags
        == {
            p.name: repo.read_tags_uncached(p).tags
            for p in real_root.iterdir()
            if p.is_file()
        }
        == {
            'Artist - Song.mp3': AudioTags('Artist', 'Song'),
            'Artist - Song.flac': AudioTags('Artist', 'Song'),
            'Other – Clash.mp3': AudioTags('Other', 'Clash'),
            'Other - Clash.mp3': AudioTags('Other', 'Clash'),
        }
    )


@pytest.mark.parametrize('interrupt', [EOFError(), KeyboardInterrupt()])
@pytest.mark.parametrize('answers', [['2'], ['3', 'run']])
def test_path_interrupt_precedes_collection_and_save(
    cli_prompts: Mock,
    monkeypatch: pytest.MonkeyPatch,
    interrupt: BaseException,
    answers: list[str],
) -> None:
    cli_prompts.side_effect = [*answers, '   ', interrupt]
    monkeypatch.setattr(cli, 'find_ffmpeg_tool', Mock(side_effect=AssertionError))
    monkeypatch.setattr(cli, 'RecoveryStore', Mock(side_effect=AssertionError))
    cli.main()
    assert not settings_path().exists()


def test_invalid_folder_preserves_preference(
    cli_prompts: Mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_mode(settings_path(), 'deep')
    cli_prompts.side_effect = ['2', str(tmp_path / 'missing')]
    monkeypatch.setattr(cli, 'RecoveryStore', Mock(side_effect=AssertionError))
    cli.main()
    assert load_mode(settings_path()) == 'deep'
