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


@windows
def test_safe_refuses_tag_write_with_id3v1_trailer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / 'music'
    root.mkdir()
    source = root / 'Artist - Song.mp3'
    synthetic_audio(source, artist='', title='Song')
    # ID3v1: signature, 30-byte title, empty artist/album/year/comment, unknown genre.
    with source.open('ab') as stream:
        stream.write(b'TAG' + b'Song'.ljust(30, b'\0') + bytes(94) + b'\xff')
    original = source.read_bytes()
    identity = inspect_file(source)
    assert original.startswith(b'ID3\x04') and original[-128:-125] == b'TAG'
    repo = TagRepository()
    tags = repo.read_tags_uncached(source).tags
    assert tags == AudioTags('', 'Song')

    _, report = run_cli(tmp_path, monkeypatch, ['safe', str(root), 'y', 'n'])
    operations = [
        json.loads(line) for line in report.splitlines() if line.startswith('{')
    ]
    [write] = [op for op in operations if op.get('kind') == 'tags']
    assert write['path'] == str(source) and write['artist'] == 'Artist'
    assert write['result'] == 'отказ' and not write['proposed']
    reason = 'сосуществующий ID3v1 не поддерживается при записи'
    assert write['reason'] == reason
    assert f'{source.name}: {reason}' in capsys.readouterr().out
    assert inspect_file(source) == identity and source.read_bytes() == original
    assert repo.read_tags_uncached(source).tags == tags
    assert {p.name for p in (root / DIRECTORY_NAME).iterdir()} == {'owner', 'lock'}


@windows
@pytest.mark.parametrize(
    'qualifier', ['(Album Version)', '(Original Mix)', '(Archive Take Z)']
)
def test_name_only_version_reaches_deep_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qualifier: str
) -> None:
    root = tmp_path / 'music'
    root.mkdir()
    source = root / f'Artist - Title {qualifier}.mp3'
    synthetic_audio(source, title='Title')
    original = source.read_bytes()
    for dry_run in (True, False):
        _, report = run_cli(tmp_path, monkeypatch, ['safe', str(root)], dry_run=dry_run)
        assert not any(
            op.get('kind') in ('tags', 'rename') for op in report_operations(report)
        )
        assert source.read_bytes() == original
    shutil.copyfile(source, root / 'Artist - Title (1).mp3')
    before = snapshot(root)
    # Keep the numbered candidate for duplicate analysis.
    _, report = run_cli(
        tmp_path, monkeypatch, ['deep', 'run', str(root), 'n'], dry_run=True
    )
    assert f'Уточнения (имя): {qualifier[1:-1]}' in report
    assert 'в группе нет единственного имени, соответствующего тегам' in report
    assert 'Уточнения в имени и теге расходятся' in report
    assert not any(
        op.get('kind') in ('rename', 'duplicate_delete', 'tags')
        for op in report_operations(report)
    )
    assert f'{source.name}  ->' not in report
    assert snapshot(root) == before
    assert TagRepository().read_tags_uncached(source).tags == AudioTags(
        'Artist', 'Title'
    )


def report_operations(report: str) -> list[dict]:
    return [json.loads(line) for line in report.splitlines() if line.startswith('{')]


@windows
@pytest.mark.parametrize('occupied', [False, True])
def test_safe_sanitizes_name_and_keeps_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, occupied: bool
) -> None:
    root = tmp_path / 'music'
    root.mkdir()
    source = root / 'Old - Title (Album Version) [Archive Take Z] (vksaver).mp3'
    synthetic_audio(source, artist='Art/ist', title='Title (Album Version)')
    original = source.read_bytes()
    target = root / 'Artist - Title (Album Version) [Archive Take Z].mp3'
    if occupied:
        synthetic_audio(
            target, artist='Art/ist', title='Title (Album Version)', frequency=660
        )
    before = snapshot(root)
    for dry_run in (True, False):
        _, report = run_cli(
            tmp_path, monkeypatch, ['safe', str(root), 'y', '1', 'y'], dry_run=dry_run
        )
        renames = [op for op in report_operations(report) if op.get('kind') == 'rename']
        assert len(renames) == 2
        assert Path(renames[0]['target']).name == source.name.replace(' (vksaver)', '')
        assert Path(renames[1]['target']) == target
        assert renames[1]['result'] == (
            'конфликт' if occupied else 'ПРЕДЛОЖЕНИЕ' if dry_run else 'успех'
        )
        if dry_run:
            assert snapshot(root) == before
    current = (
        source.with_name(source.name.replace(' (vksaver)', '')) if occupied else target
    )
    assert current.read_bytes() == original
    assert TagRepository().read_tags_uncached(current).tags == AudioTags(
        'Art/ist', 'Title (Album Version)'
    )
    if occupied:
        assert target.read_bytes() == before[target.name]
    else:
        _, repeated = run_cli(tmp_path, monkeypatch, ['safe', str(root)])
        assert not any(
            op.get('kind') in ('tags', 'rename') for op in report_operations(repeated)
        )
        assert target.read_bytes() == original


@windows
@pytest.mark.parametrize('source_title', ['Title', 'Title (w_o Guest)'])
def test_safe_qualifier_slash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_title: str
) -> None:
    root = tmp_path / 'music'
    root.mkdir()
    source = root / f'Artist - {source_title}.mp3'
    title = 'Title (w/o Guest)'
    target = root / 'Artist - Title (w_o Guest).mp3'
    synthetic_audio(source, title=title)
    original = source.read_bytes()
    for dry_run in (True, False):
        answers = ['safe', str(root), '1'] + (['y'] if target != source else [])
        _, report = run_cli(tmp_path, monkeypatch, answers, dry_run=dry_run)
        assert 'способ исправления имени не выбран' not in report
        renames = [op for op in report_operations(report) if op.get('kind') == 'rename']
        assert [op['target'] for op in renames] == (
            [str(target)] if source != target else []
        )
        if dry_run:
            assert source.read_bytes() == original
    assert target.read_bytes() == original
    assert TagRepository().read_tags_uncached(target).tags == AudioTags('Artist', title)
    _, repeated = run_cli(tmp_path, monkeypatch, ['safe', str(root), '1'])
    assert 'способ исправления имени не выбран' not in repeated
    assert not any(op.get('kind') == 'rename' for op in report_operations(repeated))


@windows
@pytest.mark.parametrize('occupied', [True, False])
@pytest.mark.parametrize(
    ('source_group', 'tag_group', 'target_group', 'strategy'),
    [
        (
            '(Original_ 2022 Remaster)',
            '(Original; 2022 Remaster)',
            '(Original; 2022 Remaster)',
            [],
        ),
        ('[Live] (from _Work_)', '[Live] (from "Work")', '[Live] (from Work)', ['1']),
        ("(from 'Work')", '(from "Work")', '(from Work)', ['1']),
        ('[Live] (from _Work-)', '[Live] (from "Work")', '[Live] (from Work)', ['1']),
        ("(from 'Work_)", '(from "Work")', '(from Work)', ['1']),
        ('[Part_One-Two]', '[Part:One:Two]', '[Part-One-Two]', ['2']),
        ('[Part Work]', '[Part:Work]', '[Part-Work]', ['2']),
    ],
)
def test_safe_ambiguous_qualifier_choice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    occupied: bool,
    source_group: str,
    tag_group: str,
    target_group: str,
    strategy: list[str],
) -> None:
    root = tmp_path / 'music'
    root.mkdir()
    source = root / f'Artist - Title {source_group}.mp3'
    title = f'Title {tag_group}'
    target = root / f'Artist - Title {target_group}.mp3'
    synthetic_audio(source, title=title)
    original = source.read_bytes()
    if occupied:
        synthetic_audio(target, title=title, frequency=660)
    target_before = target.read_bytes() if occupied else None
    _, skipped = run_cli(tmp_path, monkeypatch, ['safe', str(root), *strategy, ''])
    assert not any(
        op.get('kind') in ('rename', 'tags') for op in report_operations(skipped)
    )
    assert source.read_bytes() == original
    for dry_run in (True, False):
        _, report = run_cli(
            tmp_path, monkeypatch, ['safe', str(root), *strategy, 't'], dry_run=dry_run
        )
        assert f'{source.name} → {target.name}' in report
        [rename] = [
            op for op in report_operations(report) if op.get('kind') == 'rename'
        ]
        assert Path(rename['target']) == target
        assert rename['result'] == (
            'конфликт' if occupied else 'ПРЕДЛОЖЕНИЕ' if dry_run else 'успех'
        )
        if dry_run or occupied:
            assert source.read_bytes() == original
    if occupied:
        assert target.read_bytes() == target_before
    else:
        assert target.read_bytes() == original
        _, repeated = run_cli(tmp_path, monkeypatch, ['safe', str(root), *strategy])
        assert not any(op.get('kind') == 'rename' for op in report_operations(repeated))


@windows
@pytest.mark.parametrize('choice', ['', 't'])
def test_deep_qualifier_choice_before_duplicate_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, choice: str
) -> None:
    root = tmp_path / 'music'
    root.mkdir()
    title = 'Title (Original; 2022 Remaster)'
    source = root / 'Artist - Title (Original_ 2022 Remaster).mp3'
    numbered = root / f'Artist - {title} (1).mp3'
    synthetic_audio(source, title=title)
    shutil.copyfile(source, numbered)
    before = snapshot(root)
    _, report = run_cli(
        tmp_path, monkeypatch, ['deep', 'run', str(root), choice, 'n'], dry_run=True
    )
    ops = report_operations(report)
    renamed = [op['target'] for op in ops if op.get('kind') == 'rename']
    deletes = [op['path'] for op in ops if op.get('kind') == 'duplicate_delete']
    assert renamed == ([str(root / f'Artist - {title}.mp3')] if choice else [])
    assert deletes == ([str(numbered)] if choice else [])
    assert 'Уточнения (имя): Original; 2022 Remaster' in report
    if not choice:
        assert 'Уточнения (имя): Original_ 2022 Remaster' in report
        assert 'Выбор копии пропущен' in report
    assert snapshot(root) == before


@windows
@pytest.mark.parametrize('choice', ['', 't'])
@pytest.mark.parametrize('source_group', ['from _Work_', 'from _Work-'])
def test_deep_quote_projection_is_not_duplicate_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, choice: str, source_group: str
) -> None:
    root = tmp_path / 'music'
    root.mkdir()
    title = 'Title (from "Work")'
    source = root / f'Artist - Title ({source_group}).mp3'
    target = root / 'Artist - Title (from Work).mp3'
    numbered = root / 'Artist - Title (from Work) (1).mp3'
    synthetic_audio(source, title=title)
    shutil.copyfile(source, numbered)
    before = snapshot(root)
    _, report = run_cli(
        tmp_path,
        monkeypatch,
        ['deep', 'run', str(root), '1', choice, 'n'],
        dry_run=True,
    )
    ops = report_operations(report)
    assert [op['target'] for op in ops if op.get('kind') == 'rename'] == (
        [str(target)] if choice else []
    )
    assert not any(op.get('kind') == 'duplicate_delete' for op in ops)
    assert 'Уточнения (имя): from Work' in report
    if not choice:
        assert f'Уточнения (имя): {source_group}' in report
    assert snapshot(root) == before


@windows
def test_already_sanitized_name_is_independent_of_group_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'music'
    root.mkdir()
    fixed = root / 'First - Title (from Work).mp3'
    other = root / 'Second - Title.mp3'
    title = 'Title (from "Work")'
    synthetic_audio(fixed, artist='First', title=title)
    synthetic_audio(other, artist='Second', title=title)
    before = snapshot(root)
    _, report = run_cli(
        tmp_path, monkeypatch, ['safe', str(root), '2', 'y'], dry_run=True
    )
    assert 'способ исправления имени не выбран' not in report
    [rename] = [op for op in report_operations(report) if op.get('kind') == 'rename']
    assert rename['path'] == str(other)
    assert Path(rename['target']).name == 'Second - Title (from -Work-).mp3'
    assert snapshot(root) == before
