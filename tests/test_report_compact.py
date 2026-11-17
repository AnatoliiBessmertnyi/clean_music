"""Контракт v2: удобное начало без потери аварийного сырого журнала."""

import subprocess
from collections import Counter
from pathlib import Path
from unittest.mock import Mock

import pytest

import run_report
from context import SessionContext
from interactor import ConsoleInteractor
from pipeline_utils import safe_rename
from report_events import log_operation
from run_report import RunReport
from tag_repository import TagRepository
from tests.test_run_report import file_records, make_audio

APPENDIX = '=== ПРИЛОЖЕНИЕ: ПОЛНЫЙ СЫРОЙ ЖУРНАЛ И СНИМКИ ДО/ПОСЛЕ ===\n'


def test_compact_layout_deltas_hashes_and_lossless_appendix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / 'music'
    folder.mkdir()
    unchanged = make_audio(folder / 'unchanged.mp3')
    modified = make_audio(folder / 'before.mp3')
    original_hash = run_report.snapshot_file(unchanged, TagRepository()).sha256
    raw_bytes: list[bytes] = []
    compact = RunReport._compact

    def capture_raw(self: RunReport) -> None:
        raw_bytes.append(self.path.read_bytes())
        compact(self)

    monkeypatch.setattr(RunReport, '_compact', capture_raw)
    with RunReport(tmp_path / 'logs', False) as report:
        report.start(folder)
        ConsoleInteractor().display('Диалог должен сохраниться целиком')
        renamed = folder / 'after.mp3'
        modified.rename(renamed)
        report.record_rename(modified, renamed)
        assert TagRepository().update_tags(renamed, 'Другой', 'Новый трек')
    data = report.path.read_bytes()
    header, appendix = data.split(APPENDIX.encode(), 1)
    assert appendix == raw_bytes[0]
    text = header.decode('utf-8')
    assert text.startswith('=== ОТЧЁТ ЗАПУСКА v2 ===')
    for field in (
        'Статус запуска:',
        'полная сверка: True',
        'Режим:',
        'Commit:',
        'Python/платформа:',
        'Обработка',
        'Сверка:',
    ):
        assert field in text
    assert (
        text.index('КРАТКИЙ ИТОГ')
        < text.index('ПРОБЛЕМЫ')
        < text.index('ИЗМЕНЁННЫЕ ФАЙЛЫ')
        < text.index('НЕИЗМЕНЁННЫЕ')
    )
    assert 'artist: "Исполнитель" → "Другой"' in text
    assert 'title: "Название" → "Новый трек"' in text
    assert 'rename: успех; причина:' in text
    assert 'запись тегов: успех; причина:' in text
    unchanged_section = text.split('=== НЕИЗМЕНЁННЫЕ: ИМЯ И SHA-256 ===', 1)[1].split(
        '=== ПРЕДЛОЖЕНИЯ', 1
    )[0]
    assert unchanged_section.count(str(original_hash)) == 1
    assert 'unchanged.mp3' in unchanged_section
    assert 'artist' not in unchanged_section and 'title' not in unchanged_section
    assert 'Диалог должен сохраниться целиком' in appendix.decode('utf-8')
    assert appendix.count(' INFO ДО '.encode()) == 2
    assert appendix.count(' INFO ПОСЛЕ '.encode()) == 2
    assert report.counts == Counter(
        item['status'] for item in file_records(report.path)
    )
    for status, count in report.counts.items():
        assert f'{status}: {count}' in text
    assert 'появился: 0' in text and 'ошибка проверки: 0' in text
    assert len(list((tmp_path / 'logs').iterdir())) == 1


@pytest.mark.parametrize('failure', [KeyboardInterrupt, EOFError, RuntimeError])
def test_interrupted_run_stays_raw(
    tmp_path: Path, failure: type[BaseException]
) -> None:
    folder = tmp_path / 'music'
    folder.mkdir()
    make_audio(folder / 'track.mp3')
    with pytest.raises(failure), RunReport(tmp_path / 'logs', False) as report:
        report.start(folder)
        raise failure('interrupted')
    text = report.path.read_text(encoding='utf-8')
    assert 'СЫРОЙ ФОРМАТ' in text and 'статус запуска: прерван' in text
    assert APPENDIX not in text
    assert text.startswith('20')
    assert len(list((tmp_path / 'logs').iterdir())) == 1


def test_incomplete_verification_stays_raw(tmp_path: Path) -> None:
    folder = tmp_path / 'music'
    folder.mkdir()
    (folder / 'unreadable.mp3').write_bytes(b'invalid header')
    with RunReport(tmp_path / 'logs', False) as report:
        report.start(folder)
    text = report.path.read_text(encoding='utf-8')
    assert 'полная сверка: False' in text
    assert 'СЫРОЙ ФОРМАТ' in text and APPENDIX not in text


@pytest.mark.parametrize(
    'failed_step',
    [
        'render_compact',
        'tempfile.NamedTemporaryFile',
        'shutil.copyfileobj',
        'os.fsync',
        'os.replace',
    ],
)
def test_compaction_failure_keeps_raw_and_cleans_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_step: str
) -> None:
    folder = tmp_path / 'music'
    folder.mkdir()
    make_audio(folder / 'track.mp3')
    raw: list[bytes] = []
    compact = RunReport._compact

    def capture_raw(self: RunReport) -> None:
        raw.append(self.path.read_bytes())
        compact(self)

    monkeypatch.setattr(RunReport, '_compact', capture_raw)
    monkeypatch.setattr(
        'run_report.' + failed_step, Mock(side_effect=OSError('test failure'))
    )
    with RunReport(tmp_path / 'logs', False) as report:
        report.start(folder)
    assert report.path.read_bytes().startswith(raw[0])
    text = report.path.read_text(encoding='utf-8')
    assert 'WARNING Компактизация не выполнена; сохранён сырой журнал' in text
    assert APPENDIX not in text
    assert len(list((tmp_path / 'logs').iterdir())) == 1


def test_interrupted_compaction_keeps_raw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / 'music'
    folder.mkdir()
    monkeypatch.setattr('run_report.os.replace', Mock(side_effect=KeyboardInterrupt))
    with RunReport(tmp_path / 'logs', False) as report:
        report.start(folder)
    text = report.path.read_text(encoding='utf-8')
    assert 'сохранён сырой журнал' in text and APPENDIX not in text
    assert len(list((tmp_path / 'logs').iterdir())) == 1


def test_written_tags_and_conflicting_rename_in_one_problem_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / 'music'
    folder.mkdir()
    source = make_audio(folder / 'source.mp3')
    target = make_audio(folder / 'occupied.mp3')
    target_bytes = target.read_bytes()
    # Только проверка конфликта на синтетике; rename не выполняется на POSIX.
    monkeypatch.setattr('pipeline_utils.modification_allowed', lambda *args: True)
    with RunReport(tmp_path / 'logs', False) as report:
        report.start(folder)
        report.stage = 'Обновление и переименование'
        assert TagRepository().update_tags(source, 'Новый', 'Название')
        status, _ = safe_rename(
            source,
            target,
            SessionContext(False, False, report=report),
            TagRepository(),
            ConsoleInteractor(),
        )
        assert status == 'duplicate_exists'
    header = report.path.read_text(encoding='utf-8').split(APPENDIX)[0]
    problems = header.split('=== ПРОБЛЕМЫ ===', 1)[1].split('=== ИЗМЕНЁННЫЕ ФАЙЛЫ', 1)[
        0
    ]
    assert problems.count('ФАЙЛ:') == 1
    assert 'теги записаны, rename отклонён' in problems
    assert 'запись тегов: успех' in problems
    assert 'rename: конфликт' in problems
    assert 'блокировка: блокировка' in problems
    assert 'конфликт целевого имени' in problems
    assert 'Обновление и переименование' in problems
    assert target.read_bytes() == target_bytes


def test_rename_chain_correlates_tag_events_to_original(tmp_path: Path) -> None:
    folder = tmp_path / 'music'
    folder.mkdir()
    source = make_audio(folder / 'first.mp3')
    middle, final = folder / 'second.mp3', folder / 'third.mp3'
    with RunReport(tmp_path / 'logs', False) as report:
        report.start(folder)
        source.rename(middle)
        report.record_rename(source, middle)
        assert TagRepository().update_tags(middle, 'Новый', 'Тег')
        middle.rename(final)
        report.record_rename(middle, final)
    header = report.path.read_text(encoding='utf-8').split(APPENDIX)[0]
    assert header.count('rename: успех') == 2
    assert header.count('запись тегов: успех') == 1
    assert all(op.originals == (str(source),) for op in report.operations)


def test_identity_correlates_event_after_unregistered_external_rename(
    tmp_path: Path,
) -> None:
    folder = tmp_path / 'music'
    folder.mkdir()
    source = make_audio(folder / 'first.mp3')
    target = folder / 'external.mp3'
    with RunReport(tmp_path / 'logs', False) as report:
        report.start(folder)
        source.rename(target)
        assert TagRepository().update_tags(target, 'Новый', 'Тег')
    assert report.operations[-1].originals == (str(source),)
    assert 'отсутствует после запуска' in report.path.read_text(encoding='utf-8')


def test_dry_run_proposals_are_separate_including_blocks(tmp_path: Path) -> None:
    folder = tmp_path / 'music'
    folder.mkdir()
    source = make_audio(folder / 'source.mp3')
    proposed = folder / 'proposed.mp3'
    with RunReport(tmp_path / 'logs', True) as report:
        report.start(folder)
        log_operation(
            'rename', source, 'ПРЕДЛОЖЕНИЕ', 'тестовый прогноз', target=proposed
        )
        TagRepository().simulate_update_tags(proposed, 'Прогноз', 'Тег')
        SessionContext(True, False, report=report).block_path(
            source, 'конфликт в модели'
        )
    header = report.path.read_text(encoding='utf-8').split(APPENDIX)[0]
    facts, proposals = header.split('=== ПРЕДЛОЖЕНИЯ: НЕ ФАКТ ДИСКА ===', 1)
    assert 'Прогноз' not in facts and 'конфликт в модели' not in facts
    assert 'Прогноз' in proposals and 'ПРЕДЛОЖЕНИЕ (блокировка)' in proposals
    assert all(op.proposed for op in report.operations)
    assert report.operations[1].originals == (str(source),)
    assert report.counts == {'не изменён': 1}


@pytest.mark.parametrize(
    'failure',
    [FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError],
)
def test_git_metadata_failure_does_not_break_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    error: Exception
    if failure is subprocess.TimeoutExpired:
        error = subprocess.TimeoutExpired('git', 2)
    elif failure is subprocess.CalledProcessError:
        error = subprocess.CalledProcessError(1, 'git')
    else:
        error = FileNotFoundError('git missing')
    monkeypatch.setattr('run_report.subprocess.run', Mock(side_effect=error))
    folder = tmp_path / 'music'
    folder.mkdir()
    with RunReport(tmp_path / 'logs', False) as report:
        report.start(folder)
    assert 'Commit: unknown' in report.path.read_text(encoding='utf-8')
    assert report.verification_complete


def test_tag_write_failure_has_structured_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / 'music'
    folder.mkdir()
    path = make_audio(folder / 'track.mp3')
    repo = TagRepository()
    with RunReport(tmp_path / 'logs', False) as report:
        report.start(folder)
        monkeypatch.setattr(repo, '_write_to_disk', Mock(return_value=False))
        assert not repo.update_tags(path, 'Не записано', 'Тег')
    header = report.path.read_text(encoding='utf-8').split(APPENDIX)[0]
    assert 'запись тегов: отказ' in header
    assert 'запись тегов не подтверждена' in header
    assert report.counts == {'не изменён': 1}
