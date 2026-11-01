"""CLI-скрипт для очистки и переименования аудиофайлов."""

import sys
from collections import Counter
from pathlib import Path

from config import SUPPORTED_EXTENSIONS
from context import SessionContext
from interactor import ConsoleInteractor
from pipeline import AudioProcessingPipeline
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
        raw = input('Введите путь к папке: ').strip().strip('"')
        path_obj = Path(raw)
        if path_obj.is_dir():
            return path_obj
        print('Ошибка: путь не является папкой. Попробуйте снова.')


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
    """Запускает основной цикл обработки и переименования файлов."""
    # 1. Инициализация зависимостей
    ctx = SessionContext(
        dry_run='--dry-run' in sys.argv,
        debug='--debug' in sys.argv,
    )
    repo = TagRepository()
    interactor = ConsoleInteractor()

    print('=== Обработчик аудиофайлов ===')
    if ctx.dry_run:
        print('*** СУХОЙ РЕЖИМ: изменения не будут применены ***')

    target_dir = get_valid_path()
    files = get_audio_files(target_dir)

    total = len(files)
    stats = get_format_stats(files)
    stats_msg = format_stats_message(stats)

    print(f'\nНайдено {total} аудиофайлов ({stats_msg}).')
    if not files:
        return

    # 2. Сборка пайплайна
    pipeline = AudioProcessingPipeline([
        JunkCleaningStage(),
        NormalizationStage(),
        TagRenamingStage(),
        TagWritingStage(),
        DuplicateResolutionStage(),
        ArtistNormalizationStage(),
        ManualInputStage(),
    ])

    # 3. Запуск пайплайна
    files = pipeline.run(
        initial_files=files,
        ctx=ctx,
        repo=repo,
        interactor=interactor,
    )

    # 4. Финальная статистика
    print('\n' + '=' * 60)
    print('=== Статистика обработки ===')
    print('=' * 60)
    print(f'Очистка от мусора (имена): {ctx.get_stat("junk_removed")}')
    print(f'Очистка от мусора (теги): {ctx.get_stat("tags_cleaned")}')
    print(f'Нормализация тире и пробелов: {ctx.get_stat("dash_normalized")}')
    print(f'Удалено дубликатов: {ctx.get_stat("duplicates_removed")}')
    print(f'Переименовано по тегам: {ctx.get_stat("renamed_by_tags")}')
    print(f'Записано тегов: {ctx.get_stat("tags_written")}')
    print(
        f'Нормализовано исполнителей: '
        f'{ctx.get_stat("artists_normalized")} файлов '
        f'({ctx.get_stat("artists_normalized_unique")} уникальных)'
    )
    print(f'Ручной ввод: {ctx.get_stat("manual_input")}')

    still_missing: list[Path] = getattr(ctx, '_still_missing', [])
    if still_missing:
        print(f'\nВнимание! {len(still_missing)} файлов осталось без метаданных:')
        for f in still_missing:
            print(f'  - {f.name}')
        print('Рекомендация: AcoustID / MusicBrainz или вручную.')

    print('\nОбработка завершена.')


if __name__ == '__main__':
    main()
