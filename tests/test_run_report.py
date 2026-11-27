"""Сверка настоящих синтетических MP3 и жизненный цикл одного журнала."""

import json
import logging
import os
from pathlib import Path
from unittest.mock import Mock

import pytest
from mutagen.asf import ASFUnicodeAttribute
from mutagen.id3 import ID3

import main as cli
from context import SessionContext
from file_state import OperationStatus, inspect_file
from interactor import ConsoleInteractor
from pipeline_utils import safe_rename
from run_report import FileSnapshot, RunReport, snapshot_file
from tag_repository import AudioTags, TagRepository


def make_audio(path: Path) -> Path:
    # MPEG1 Layer III, 128kbps / 44100Hz: только синтетические кадры.
    path.write_bytes((b'\xff\xfb\x90\x64' + bytes(413)) * 5)
    assert write_fixture_tags(path, 'Исполнитель', 'Название')
    return path


def write_fixture_tags(path: Path, artist: str, title: str) -> bool:
    repo = TagRepository()

    def save(a: str | None, t: str | None) -> tuple[OperationStatus, str]:
        with path.open('r+b') as stream:
            repo.save_work_tags(path, stream, a, t)
        return OperationStatus.SUCCESS, 'изменена синтетическая фикстура'

    result = repo.apply_tags(path, artist, title, simulated=False, publish=save)
    return result.status in (OperationStatus.SUCCESS, OperationStatus.NOOP)


def file_records(path: Path) -> list[dict[str, object]]:
    records = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if line.startswith('{'):
            item = json.loads(line)
            if item.get('type') == 'ФАЙЛ':
                item.pop('type')
                records.append(item)
        elif ' ФАЙЛ ' in line:
            records.append(json.loads(line.split(' ФАЙЛ ', 1)[1]))
    return records


def test_actual_states_and_rename_chain(tmp_path: Path) -> None:
    folder = tmp_path / 'music'
    folder.mkdir()
    unchanged = make_audio(folder / 'unchanged.mp3')
    renamed = make_audio(folder / 'original.mp3')
    modified = make_audio(folder / 'tags.mp3')
    combined = make_audio(folder / 'combined.mp3')
    missing = make_audio(folder / 'missing.mp3')
    with RunReport(tmp_path / 'logs', False) as report:
        report.start(folder)
        middle, final = folder / 'middle.mp3', folder / 'Final.mp3'
        renamed.rename(middle)
        report.record_rename(renamed, middle)
        middle.rename(final)
        report.record_rename(middle, final)
        # Смена только регистра должна оставаться видимой, включая Windows Path.
        lower = folder / 'final.mp3'
        final.rename(lower)
        report.record_rename(final, lower)
        assert write_fixture_tags(modified, 'Новый', 'Тег')
        new_combined = folder / 'new-combined.mp3'
        combined.rename(new_combined)
        report.record_rename(combined, new_combined)
        assert write_fixture_tags(new_combined, 'Другой', 'Трек')
        missing.unlink()
        make_audio(folder / 'added.mp3')
    records = file_records(report.path)
    assert report.verification_complete
    assert len(records) == 6
    assert report.counts == {
        'не изменён': 1,
        'переименован': 1,
        'изменено содержимое/теги': 1,
        'переименован и изменено содержимое/теги': 1,
        'отсутствует после запуска (удаление не подтверждено)': 1,
        'появился': 1,
    }
    assert unchanged.exists()
    text = report.path.read_text(encoding='utf-8')
    assert 'original.mp3' in text and 'final.mp3' in text
    assert 'Новый' in text and 'Исполнитель' in text


def test_hash_detects_content_change_without_tag_change(tmp_path: Path) -> None:
    folder = tmp_path / 'music'
    folder.mkdir()
    path = make_audio(folder / 'track.mp3')
    with RunReport(tmp_path / 'logs', False) as report:
        report.start(folder)
        with path.open('ab') as stream:
            stream.write(b'changed bytes')
    assert report.counts == {'изменено содержимое/теги': 1}


@pytest.mark.parametrize('dry_run', [False, True])
def test_same_path_replacement_is_not_unchanged(tmp_path: Path, dry_run: bool) -> None:
    folder = tmp_path / 'music'
    folder.mkdir()
    path = make_audio(folder / 'track.mp3')
    replacement = make_audio(tmp_path / 'replacement.mp3')
    with RunReport(tmp_path / 'logs', dry_run) as report:
        report.start(folder)
        path.unlink()
        replacement.rename(path)
    assert report.counts == {'ошибка проверки: файловый объект заменён': 1}
    assert not report.verification_complete


def test_read_errors_are_not_reported_as_unchanged(tmp_path: Path) -> None:
    folder = tmp_path / 'music'
    folder.mkdir()
    (folder / 'broken.mp3').write_bytes(b'not an MP3')
    with RunReport(tmp_path / 'logs', False) as report:
        report.start(folder)
    assert report.counts == {'ошибка проверки': 1}
    assert not report.verification_complete
    assert 'чтение тегов' in report.path.read_text(encoding='utf-8')


def test_hash_read_error_is_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_audio(tmp_path / 'track.mp3')
    monkeypatch.setattr(Path, 'open', Mock(side_effect=PermissionError('locked')))
    result = snapshot_file(path, TagRepository())
    assert result.sha256 is None
    assert result.errors == ['чтение файла: locked']


def test_uncached_tags_bypass_pipeline_simulation(tmp_path: Path) -> None:
    path = make_audio(tmp_path / 'track.mp3')
    repo = TagRepository()
    repo.simulate_update_tags(path, 'Предложение', 'Прогноз')
    assert snapshot_file(path, repo).tags == AudioTags('Исполнитель', 'Название')


def test_partial_tags_are_visible_in_pipeline_and_report(
    tmp_path: Path,
) -> None:
    path = make_audio(tmp_path / 'track.mp3')
    tags = ID3(path)
    tags.delall('TIT2')
    tags.save(path)
    repo = TagRepository()
    assert repo.get_tags(path).tags == AudioTags('Исполнитель', '')
    assert repo.read_tags_uncached(path).tags == AudioTags('Исполнитель', '')


def test_report_serializes_asf_attribute_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / 'music'
    folder.mkdir()
    (folder / 'track.wma').write_bytes(b'synthetic container')
    monkeypatch.setattr(
        'tag_repository.ASF',
        Mock(
            return_value=Mock(
                tags={
                    'Author': [ASFUnicodeAttribute('Исполнитель')],
                    'Title': [ASFUnicodeAttribute('Название')],
                },
                info=None,
            )
        ),
    )
    with RunReport(tmp_path / 'logs', False) as report:
        report.start(folder)
    assert report.counts == {'не изменён': 1}
    assert report.verification_complete
    assert 'Исполнитель' in report.path.read_text(encoding='utf-8')


def test_symlink_is_not_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = make_audio(tmp_path / 'target.mp3')
    link = tmp_path / 'link.mp3'
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f'Создание symlink недоступно: {exc}')
    reader = Mock()
    monkeypatch.setattr(Path, 'open', reader)
    result = snapshot_file(link, TagRepository())
    reader.assert_not_called()
    assert result.sha256 is None
    assert 'ссылка или reparse point' in result.errors[0]


def test_dry_run_predictions_do_not_change_actual_report(tmp_path: Path) -> None:
    folder = tmp_path / 'music'
    folder.mkdir()
    path = make_audio(folder / 'track.mp3')
    with RunReport(tmp_path / 'logs', True) as report:
        report.start(folder)
        repo = TagRepository()
        repo.simulate_update_tags(path, 'Прогноз', 'Только предложение')
        ConsoleInteractor().display('[DRY RUN] Переименовал бы файл')
    assert report.counts == {'не изменён': 1}
    text = report.path.read_text(encoding='utf-8')
    assert 'dry-run (предложения)' in text
    assert 'ПРЕДЛОЖЕНИЕ' in text
    assert '[DRY RUN]' in text


@pytest.mark.parametrize('dry_run', [False, True])
@pytest.mark.skipif(os.name != 'nt', reason='Реальный Windows rename')
def test_safe_rename_records_only_real_success(tmp_path: Path, dry_run: bool) -> None:
    folder = tmp_path / 'music'
    folder.mkdir()
    old = make_audio(folder / 'old.mp3')
    new = folder / 'new.mp3'
    with RunReport(tmp_path / 'logs', dry_run) as report:
        report.start(folder)
        ctx = SessionContext(dry_run, False, report=report)
        ctx.registry.register(old, identity=inspect_file(old))
        status, _ = safe_rename(old, new, ctx, TagRepository(), ConsoleInteractor())
    assert status == ('planned' if dry_run else 'renamed')
    text = report.path.read_text(encoding='utf-8')
    events = [json.loads(line) for line in text.splitlines() if line.startswith('{')]
    renames = [
        item
        for item in events
        if item.get('type') == 'ОПЕРАЦИЯ' and item['kind'] == 'rename'
    ]
    assert len(renames) == 1
    assert renames[0]['proposed'] == dry_run
    assert renames[0]['result'] == ('ПРЕДЛОЖЕНИЕ' if dry_run else 'успех')
    assert renames[0]['path'] == str(old)
    assert renames[0]['target'] == str(new)
    assert report.counts == {('не изменён' if dry_run else 'переименован'): 1}


def test_dialog_answers_and_errors_in_one_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr('builtins.input', Mock(side_effect=['', 'ТЕКСТ']))
    with RunReport(tmp_path / 'logs', False) as report:
        ui = ConsoleInteractor()
        ui.display('Список вариантов')
        assert ui.prompt('Выбор [y]: ', 'y') == 'y'
        assert ui.prompt('Имя: ') == 'ТЕКСТ'
        ui.display_error('Не удалось выполнить действие')
    text = report.path.read_text(encoding='utf-8')
    for expected in (
        'Список вариантов',
        'ВОПРОС: Выбор',
        "ОТВЕТ: ''",
        'вариант y',
        "ОТВЕТ: 'ТЕКСТ'",
        'Не удалось выполнить действие',
    ):
        assert expected in text
    assert len(list((tmp_path / 'logs').iterdir())) == 1


@pytest.mark.parametrize('failure', [KeyboardInterrupt, EOFError, RuntimeError])
def test_cli_failure_still_compares_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[BaseException]
) -> None:
    folder = tmp_path / 'music'
    folder.mkdir()
    path = make_audio(folder / 'track.mp3')
    monkeypatch.setattr(cli, '__file__', str(tmp_path / 'main.py'))
    monkeypatch.setattr('builtins.input', Mock(side_effect=['', str(folder)]))

    def interrupted_run(
        self: cli.AudioProcessingPipeline, **kwargs: object
    ) -> list[Path]:
        assert write_fixture_tags(path, 'Уже записано', 'До прерывания')
        raise failure('test interruption')

    monkeypatch.setattr(cli.AudioProcessingPipeline, 'run', interrupted_run)
    with pytest.raises(failure):
        cli.main()
    logs = list((tmp_path / 'logs').glob('*.log'))
    assert len(logs) == 1
    text = logs[0].read_text(encoding='utf-8')
    assert 'Уже записано' in text
    assert 'статус запуска: прерван' in text
    assert failure.__name__ in text
    assert file_records(logs[0])[0]['status'] == 'изменено содержимое/теги'


def test_cli_empty_folder_and_unique_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / 'empty'
    folder.mkdir()
    monkeypatch.setattr(cli, '__file__', str(tmp_path / 'main.py'))
    monkeypatch.setattr('builtins.input', Mock(side_effect=['', str(folder)] * 2))
    cli.main()
    cli.main()
    logs = list((tmp_path / 'logs').glob('*.log'))
    assert len(logs) == 2
    assert all(
        'Аудиозаписей до: 0; после: 0' in path.read_text(encoding='utf-8')
        for path in logs
    )


def test_cli_eof_before_path_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, '__file__', str(tmp_path / 'main.py'))
    monkeypatch.setattr('builtins.input', Mock(side_effect=['', EOFError]))
    with pytest.raises(EOFError):
        cli.main()
    log = next((tmp_path / 'logs').glob('*.log'))
    text = log.read_text(encoding='utf-8')
    assert 'ВОПРОС: Введите путь к папке:' in text
    assert 'Папка не выбрана' in text


def test_no_processing_if_log_cannot_be_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, '__file__', str(tmp_path / 'main.py'))
    (tmp_path / 'logs').write_text('occupied', encoding='utf-8')
    prompt = Mock()
    monkeypatch.setattr('builtins.input', prompt)
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    prompt.assert_not_called()


def test_final_scan_failure_preserves_original_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / 'music'
    folder.mkdir()
    with (
        pytest.raises(RuntimeError, match='pipeline failure'),
        RunReport(tmp_path / 'logs', False) as report,
    ):
        report.start(folder)
        monkeypatch.setattr(
            report, '_audio_entries', Mock(side_effect=PermissionError('scan locked'))
        )
        raise RuntimeError('pipeline failure')
    text = report.path.read_text(encoding='utf-8')
    assert 'pipeline failure' in text and 'scan locked' in text
    assert 'полная сверка: False' in text


def test_report_restores_logger_configuration(tmp_path: Path) -> None:
    logger = logging.getLogger('clean_music')
    original = (logger.level, logger.propagate, list(logger.handlers))
    with RunReport(tmp_path / 'logs', False):
        pass
    assert (logger.level, logger.propagate, logger.handlers) == original


def test_log_write_failure_stops_following_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / 'music'
    folder.mkdir()
    path = make_audio(folder / 'track.mp3')
    original = path.read_bytes()
    with (
        pytest.raises(OSError, match='Не удалось записать журнал'),
        RunReport(tmp_path / 'logs', False) as report,
    ):
        report.start(folder)
        with monkeypatch.context() as patch:
            patch.setattr(
                report._handler,
                'stream',
                Mock(write=Mock(side_effect=OSError('disk full'))),
            )
            ConsoleInteractor().display('Сообщение перед действием')
            path.write_bytes(b'must not be written')
    assert path.read_bytes() == original
    assert 'статус запуска: прерван' in report.path.read_text(encoding='utf-8')


def test_interrupted_final_scan_keeps_available_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / 'music'
    folder.mkdir()
    first = make_audio(folder / 'first.mp3')
    make_audio(folder / 'second.mp3')
    original_snapshot = snapshot_file

    def interrupt_second(path: Path, repo: TagRepository) -> FileSnapshot:
        if path.name == 'second.mp3':
            raise KeyboardInterrupt
        return original_snapshot(path, repo)

    with RunReport(tmp_path / 'logs', False) as report:
        report.start(folder)
        monkeypatch.setattr('run_report.snapshot_file', interrupt_second)
    text = report.path.read_text(encoding='utf-8')
    after_lines = [line for line in text.splitlines() if ' ПОСЛЕ ' in line]
    assert len(after_lines) == 1 and first.name in after_lines[0]
    assert 'Итоговая сверка неполна' in text
    assert 'полная сверка: False' in text
