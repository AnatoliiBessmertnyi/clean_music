"""CLI-скрипт для очистки и переименования аудиофайлов."""

import sys
from collections import Counter
from pathlib import Path

from config import SUPPORTED_EXTENSIONS
from context import SessionContext
from interactor import ConsoleInteractor, logged_input
from pipeline import AudioProcessingPipeline
from run_report import RunReport
from stages import (
    ArtistNormalizationStage,
    DuplicateResolutionStage,
    JunkCleaningStage,
    ManualInputStage,
    NormalizationStage,
    TagRenamingStage,
    TagWritingStage,
)
from tag_repository import TagRepository


def get_valid_path() -> Path:
    """Запрашивает у пользователя путь и проверяет его корректность."""
    while True:
        raw = logged_input('Введите путь к папке: ').strip().strip('"')
        path_obj = Path(raw)
        if path_obj.is_dir():
            return path_obj
        ConsoleInteractor().display(
            'Ошибка: путь не является папкой. Попробуйте снова.'
        )


def get_audio_files(directory: Path) -> list[Path]:
    """Возвращает список всех аудиофайлов в заданной директории."""
    return [f for f in directory.iterdir() if f.suffix.lower() in SUPPORTED_EXTENSIONS]


def get_format_stats(files: list[Path]) -> dict[str, int]:
    """Подсчитывает количество файлов по форматам."""
    counter = Counter(f.suffix.lower() for f in files)
    return dict(counter)


def format_stats_message(stats: dict[str, int]) -> str:
    """Форматирует статистику по форматам для вывода."""
    parts = []
    for ext, count in sorted(stats.items(), key=lambda x: -x[1]):
        ext_upper = ext.upper().lstrip('.')
        parts.append(f'{count} {ext_upper}')
    return ', '.join(parts)


def main() -> None:
    """Создаёт один журнал до любых вопросов и файловых изменений."""
    try:
        report = RunReport(Path(__file__).parent / 'logs', '--dry-run' in sys.argv)
    except OSError as exc:
        sys.stderr.write(f'Не удалось создать журнал; обработка не начата: {exc}\n')
        raise SystemExit(1) from exc
    with report:
        ConsoleInteractor().display(f'Отчёт запуска: {report.path}')
        _run(report)


def _run(report: RunReport) -> None:
    """Выполняет прежний конвейер внутри журнала запуска."""
    # 1. Инициализация зависимостей
    ctx = SessionContext(
        dry_run='--dry-run' in sys.argv,
        debug='--debug' in sys.argv,
        report=report,
    )
    repo = TagRepository()
    interactor = ConsoleInteractor()

    interactor.display('=== Обработчик аудиофайлов ===')
    if ctx.dry_run:
        interactor.display(
            '*** СУХОЙ РЕЖИМ: действия конвейера — предложения; итог сверяется с диском ***'
        )

    target_dir = get_valid_path()
    ctx.target_dir = target_dir
    interactor.display('Снимаю исходное состояние файлов (SHA-256 и теги)...')
    report.start(target_dir)
    files = get_audio_files(target_dir)

    total = len(files)
    stats = get_format_stats(files)
    stats_msg = format_stats_message(stats)

    interactor.display(f'\nНайдено {total} аудиофайлов ({stats_msg}).')
    if not files:
        return

    # 2. Сборка пайплайна
    pipeline = AudioProcessingPipeline(
        [
            JunkCleaningStage(),
            NormalizationStage(),
            TagRenamingStage(),
            TagWritingStage(),
            DuplicateResolutionStage(),
            ArtistNormalizationStage(),
            ManualInputStage(),
        ]
    )

    # 3. Запуск пайплайна
    files = pipeline.run(
        initial_files=files,
        ctx=ctx,
        repo=repo,
        interactor=interactor,
    )

    # 4. Финальная статистика
    interactor.display('\n' + '=' * 60)
    interactor.display('=== Счётчики конвейера (не итоговая сверка диска) ===')
    interactor.display('=' * 60)
    interactor.display(f'Очистка от мусора (имена): {ctx.get_stat("junk_removed")}')
    interactor.display(f'Очистка от мусора (теги): {ctx.get_stat("tags_cleaned")}')
    interactor.display(
        f'Нормализация тире и пробелов: {ctx.get_stat("dash_normalized")}'
    )
    interactor.display(f'Удалено дубликатов: {ctx.get_stat("duplicates_removed")}')
    interactor.display(f'Переименовано по тегам: {ctx.get_stat("renamed_by_tags")}')
    interactor.display(f'Записано тегов: {ctx.get_stat("tags_written")}')
    interactor.display(
        f'Нормализовано исполнителей: '
        f'{ctx.get_stat("artists_normalized")} файлов '
        f'({ctx.get_stat("artists_normalized_unique")} уникальных)'
    )
    interactor.display(f'Ручной ввод: {ctx.get_stat("manual_input")}')

    still_missing: list[Path] = getattr(ctx, '_still_missing', [])
    if still_missing:
        interactor.display(
            f'\nВнимание! {len(still_missing)} файлов осталось без метаданных:'
        )
        for f in still_missing:
            interactor.display(f'  - {f.name}')
        interactor.display('Рекомендация: AcoustID / MusicBrainz или вручную.')

    interactor.display('\nОбработка завершена. Итоговая сверка будет записана в отчёт.')


if __name__ == '__main__':
    main()
