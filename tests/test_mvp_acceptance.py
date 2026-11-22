"""Приёмка политик и конвейера через консольную точку входа."""

import json
import os
import shutil
from pathlib import Path

import pytest

from file_state import inspect_file
from pipeline_utils import WSL_MODIFICATION_DISABLED
from recovery_store import DIRECTORY_NAME
from tag_ntfs import guarded_file
from tag_repository import AudioTags, TagRepository
from tests.test_deep_mode import run_cli
from tests.test_tag_publication import synthetic_audio, windows
from tests.test_tag_repository import write_mp3
from user_settings import load_mode, save_mode, settings_path


def snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): None if path.is_dir() else path.read_bytes()
        for path in root.rglob('*')
    }


@pytest.mark.parametrize('deep', [False, True], ids=['safe', 'deep'])
@pytest.mark.parametrize('dry_run', [False, True], ids=['apply', 'plan'])
def test_cli_collection_matrix(
    deep: bool,
    dry_run: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    template = tmp_path / 'template'
    template.mkdir()
    winner = template / 'Artist - Song.mp3'
    synthetic_audio(winner, title='Song')
    copy = 'Artist - Song (1).mp3'
    shutil.copyfile(winner, template / copy)
    version = template / 'Artist - Song (2).mp3'
    synthetic_audio(version, title='Song', frequency=660)
    case = 'case - track.mp3'
    canonical = 'Case - Track.mp3'
    write_mp3(template / case, artist='Case', title='Track')
    conflict = 'Other – Clash.mp3'
    write_mp3(template / conflict, artist='Other', title='Clash')
    shutil.copyfile(template / conflict, template / 'Other - Clash.mp3')
    partial = 'Unknown.mp3'
    write_mp3(template / partial, title='Unknown')
    root = tmp_path / 'collection'
    shutil.copytree(template, root)
    before = snapshot(root)
    names = set(before)
    identities = {name: inspect_file(root / name) for name in names}
    repo = TagRepository()
    tags_before = {name: repo.read_tags_uncached(root / name).tags for name in names}
    assert tags_before[partial] == AudioTags('', 'Unknown')
    assert before[copy] == before[winner.name] != before[version.name]
    save_mode(settings_path(), 'safe')
    settings_before = snapshot(settings_path().parent)
    windows_run = os.name == 'nt'
    expected_rename = int(windows_run)
    expected_delete = int(deep and (windows_run or dry_run))
    expected_paths = {name: name for name in names}
    if windows_run and not dry_run:
        expected_paths[case] = canonical
        if deep:
            del expected_paths[copy]

    answers = ['deep', 'run'] if deep else ['safe']
    # Оставляем номера копий для этапа анализа. Принимаем остальные
    # предложения этапа переименования по тегам в порядке списка CLI.
    rename_candidates = [
        p.name for p in root.iterdir() if p.name in {copy, version.name, case, conflict}
    ]
    excluded = ' '.join(
        str(i)
        for i, name in enumerate(rename_candidates, 1)
        if name in {copy, version.name}
    )
    prompts, report = run_cli(
        tmp_path,
        monkeypatch,
        [*answers, str(root), 'y', excluded, 'n'],
        dry_run=dry_run,
    )
    output = capsys.readouterr().out
    assert prompts.call_count == len(answers) + 4
    assert 'Отчёт запуска:' in output
    assert 'Байты различаются' in output
    assert f'Удалено дубликатов: {expected_delete}' in output
    assert f'Переименовано по тегам: {expected_rename}' in output
    assert 'Записано тегов: 0' in output
    assert 'Ручной ввод: 0' in output
    assert partial in output
    if windows_run:
        assert 'конфликт целевого имени; оба файла сохранены' in report
    else:
        assert WSL_MODIFICATION_DISABLED in output
    assert ('прогноз удаления' in output) == (deep and dry_run)
    assert ('Точные дубли будут удалены безвозвратно' in output) == (
        deep and not dry_run
    )
    operations = [
        json.loads(line) for line in report.splitlines() if line.startswith('{')
    ]
    expected_deletions = []
    if deep:
        result = 'ПРЕДЛОЖЕНИЕ' if dry_run else 'успех' if windows_run else 'отказ'
        expected_deletions.append((copy, result))
    assert [
        (Path(op['path']).name, op['result'])
        for op in operations
        if op.get('kind') == 'duplicate_delete'
    ] == expected_deletions
    assert [
        (Path(op['path']).name, Path(op['target']).name, op['result'])
        for op in operations
        if op.get('kind') == 'rename' and op['result'] in ('ПРЕДЛОЖЕНИЕ', 'успех')
    ] == (
        [(case, canonical, 'ПРЕДЛОЖЕНИЕ' if dry_run else 'успех')]
        if windows_run
        else []
    )
    assert (
        f'не изменён: {7 if dry_run or not windows_run else 6 - expected_delete}'
        in report
    )
    assert {p.name for p in root.iterdir() if p.is_file()} == set(
        expected_paths.values()
    )
    for original, current in expected_paths.items():
        path = root / current
        assert path.read_bytes() == before[original]
        assert inspect_file(path) == identities[original]
        assert repo.read_tags_uncached(path).tags == tags_before[original]
    if dry_run:
        assert snapshot(root) == before
        assert snapshot(settings_path().parent) == settings_before
    else:
        assert load_mode(settings_path()) == ('deep' if deep else 'safe')
        assert not list((root / DIRECTORY_NAME).glob('*.json'))
    if windows_run and deep and not dry_run:
        after = snapshot(root)
        cancelled, _ = run_cli(tmp_path, monkeypatch, ['cancel'])
        assert cancelled.call_count == 1
        cancellation = capsys.readouterr().out
        assert 'Точные дубли будут удалены безвозвратно' in cancellation
        assert 'Выбран глубокий режим' not in cancellation
        assert snapshot(root) == after
        run_cli(tmp_path, monkeypatch, ['once', str(root), 'y', 'y', 'n'])
        assert snapshot(root) == after
        assert not (root / copy).exists()
        assert 'Удалено дубликатов: 0' in capsys.readouterr().out


def test_cli_rejects_hard_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'collection'
    root.mkdir()
    source = write_mp3(root / 'Artist - Song.mp3', artist='Artist', title='Song')
    alias = root / 'Artist - Song (1).mp3'
    alias.hardlink_to(source)
    before = snapshot(root)
    _, report = run_cli(tmp_path, monkeypatch, ['deep', 'run', str(root)])
    assert source.samefile(alias)
    assert source.stat().st_nlink == 2
    assert {path.name: path.read_bytes() for path in root.glob('*.mp3')} == before
    assert report.count('требуется ровно одна жёсткая ссылка') >= 2


@windows
def test_cli_reports_rename_success_then_sharing_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / 'collection'
    root.mkdir()
    first = write_mp3(root / 'First – Song.mp3', artist='First', title='Song')
    locked = write_mp3(root / 'second - song.mp3', artist='Second', title='Song')
    first_before = inspect_file(first), first.read_bytes()
    locked_before = inspect_file(locked), locked.read_bytes()

    with guarded_file(locked):
        _, report = run_cli(tmp_path, monkeypatch, ['safe', str(root), 'y', 'y'])

    target = root / 'First - Song.mp3'
    assert {p.name for p in root.glob('*.mp3')} == {target.name, locked.name}
    assert (inspect_file(target), target.read_bytes()) == first_before
    assert (inspect_file(locked), locked.read_bytes()) == locked_before
    operations = [
        json.loads(line) for line in report.splitlines() if line.startswith('{')
    ]
    renames = [op for op in operations if op.get('kind') == 'rename']
    assert [(Path(op['path']).name, op['result']) for op in renames] == [
        (first.name, 'успех'),
        (locked.name, 'отказ'),
    ]
    assert 'WinError 32' in renames[1]['reason']
    output = capsys.readouterr().out
    assert 'Нормализация тире и пробелов: 1' in output
    assert 'Переименовано по тегам: 0' in output


@windows
@pytest.mark.parametrize('missing', ['ffprobe', 'ffmpeg'])
def test_cli_missing_tool_before_processing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    missing: str,
) -> None:
    root = tmp_path / 'music'
    root.mkdir()
    source = root / 'Artist - Song.mp3'
    synthetic_audio(source, artist='', title='Song')
    original = source.read_bytes()
    other = root / 'Other – Tune.mp3'
    write_mp3(other, artist='Other', title='Tune')
    other_bytes = other.read_bytes()
    tools = tmp_path / 'bin'
    tools.mkdir()
    available = 'ffmpeg' if missing == 'ffprobe' else 'ffprobe'
    executable = shutil.which(available)
    assert executable is not None
    shutil.copy2(executable, tools / Path(executable).name)
    monkeypatch.setenv('PATH', str(tools))
    _, report = run_cli(tmp_path, monkeypatch, ['safe', str(root), 'y', 'y', 'n'])
    output = capsys.readouterr().out
    warning = missing + ' недоступен'
    assert output.count(warning) == 1
    assert output.index(warning) < output.index('Снимаю исходное состояние')
    assert root.joinpath('Other - Tune.mp3').read_bytes() == other_bytes
    operations = [
        json.loads(line) for line in report.splitlines() if line.startswith('{')
    ]
    [write] = [
        op
        for op in operations
        if op.get('kind') == 'tags' and op['path'] == str(source)
    ]
    tags = TagRepository().read_tags_uncached(source).tags
    if missing == 'ffprobe':
        assert 'изменения тегов artist/title' in output
        assert write['result'] == 'отказ' and write['reason'] == warning
        assert tags == AudioTags('', 'Song') and source.read_bytes() == original
    else:
        assert write['result'] == 'успех'
        assert tags == AudioTags('Artist', 'Song')


@windows
def test_safe_reports_tag_write_refused_for_named_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / 'music'
    root.mkdir()
    source = root / 'Artist - Song.mp3'
    synthetic_audio(source, artist='', title='Song')
    stream = Path(f'{source}:synthetic')
    stream.write_bytes(b'synthetic stream data')
    before = source.read_bytes(), stream.read_bytes()

    _, report = run_cli(tmp_path, monkeypatch, ['safe', str(root), 'y', 'n'])
    operations = [
        json.loads(line) for line in report.splitlines() if line.startswith('{')
    ]
    [write] = [op for op in operations if op.get('kind') == 'tags']
    assert write['path'] == str(source) and write['result'] == 'отказ'
    reason = write['reason']
    assert 'именованный поток NTFS' in reason and 'запись тегов отклонена' in reason
    assert 'только основной поток' in reason
    assert reason in capsys.readouterr().out
    assert (source.read_bytes(), stream.read_bytes()) == before
