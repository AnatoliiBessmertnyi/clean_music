import re
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from mutagen.asf import ASF
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis

from config import (ARTIST_NAME_EXCEPTIONS, FEAT_VARIANTS_PATTERN,
                    INVALID_FILENAME_CHARS, JUNK_PATTERN,
                    LEADING_TRACK_PATTERN, SUPPORTED_EXTENSIONS, TOTAL_STAGES)
from context import SessionContext
from interactor import ConsoleInteractor
from naming import (build_safe_filename, normalize_dash,
                    normalize_filename_stem, normalize_title_spacing,
                    normalize_unicode, remove_annotation_from_text,
                    remove_junk)
from parsing import (clean_extracted_tags, extract_title_from_filename,
                     parse_artist_title)
from pipeline import AudioProcessingPipeline
from pipeline_utils import (
    clean_filename_safely,
    files_are_same,
    handle_rename_result,
    rename_case_sensitive,
    safe_rename,
    select_indices,
)
from stages import JunkCleaningStage, NormalizationStage, TagRenamingStage
from strategies import StrategyFactory
from tag_repository import AudioTags, TagRepository


def stage(number: int, name: str, ctx: SessionContext) -> None:
    """Печатает заголовок этапа обработки."""
    print(f'\n{"=" * 60}')
    print(f'[Этап {number}/{TOTAL_STAGES}] {name}')
    print(f'{"=" * 60}')
    ctx.log_debug(f'Начинаю этап {number}/{TOTAL_STAGES}: {name}')


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


def simple_rename(
    old_path: Path, new_path: Path, ctx: SessionContext, repo: TagRepository
) -> tuple[str, Path | None]:
    """Простое переименование без обработки дубликатов."""
    ctx.log_debug(f'simple_rename: {old_path.name!r} -> {new_path.name!r}')

    if not old_path.exists():
        print(f'  Файл не найден: {old_path.name}')
        return 'skipped', None

    if files_are_same(old_path, new_path):
        ctx.log_debug('Это один и тот же файл (samefile), меняем только регистр')
        if ctx.dry_run:
            ctx.log_debug('[DRY RUN] Пропускаю смену регистра')
            return 'renamed', new_path
        if rename_case_sensitive(old_path, new_path, ctx):
            ctx.log_debug(f'Регистр успешно изменён: {new_path.name}')
            repo.simulate_rename(old_path, new_path)
            return 'renamed', new_path
        ctx.log_debug('Не удалось изменить регистр, пропускаю')
        return 'skipped', None

    if not new_path.exists():
        if ctx.dry_run:
            ctx.log_debug(f'[DRY RUN] Переименовал бы: {old_path.name} -> {new_path.name}')
            repo.simulate_rename(old_path, new_path)
            return 'renamed', new_path
        old_path.rename(new_path)
        repo.simulate_rename(old_path, new_path)
        return 'renamed', new_path

    return 'duplicate_exists', new_path


def main() -> None:
    """Запускает основной цикл обработки и переименования файлов."""
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

    # --- Запуск Этапов 1 и 2 через Pipeline ---
    pipeline = AudioProcessingPipeline([
        JunkCleaningStage(),
        NormalizationStage(),
    ])
    
    files = pipeline.run(
        initial_files=files,
        ctx=ctx,
        repo=repo,
        interactor=interactor,
    )

    # --- Этап 3: Переименование по тегам ---
    tag_stage = TagRenamingStage()
    files = tag_stage.execute(files, ctx, repo, interactor)

    # --- Шаг 4: Запись тегов из имени файла ---
    stage(4, 'Запись тегов из имени файла', ctx)
    tags_to_write = []

    for f in files:
        if not f.exists():
            continue
        parsed = parse_artist_title(f.stem)
        if parsed:
            tags = repo.get_tags(f)
            if not tags:
                artist, title = parsed
                artist = remove_junk(artist)
                title = remove_junk(title)
                artist, title = normalize_feat_pair(artist, title)
                artist = remove_feat_guests_from_artist(artist, title)
                title = normalize_title_spacing(title)
                tags_to_write.append((f, artist, title))

    if tags_to_write:
        bracket_pattern = re.compile(r'[\(\[]([^\)\]]+)[\)\]]')
        files_with_annotations = []

        for i, (f, artist, title) in enumerate(tags_to_write):
            matches = bracket_pattern.findall(title)
            real_matches = [m for m in matches if not FEAT_VARIANTS_PATTERN.match(m)]
            if real_matches:
                files_with_annotations.append((i, f, artist, title, real_matches))

        if files_with_annotations:
            print(
                f'\n  Внимание: {len(files_with_annotations)} файлов содержат уточнения в скобках:'
            )
            for i, f, artist, title, matches in files_with_annotations:
                print(f'    {f.name}')
                print(f'      Уточнения: {", ".join(f"({m})" for m in matches)}')

            choice = input('\n  Удалить уточнения из тегов и имён? (y/n) [y]: ').strip().lower()

            if choice in ('', 'y', 'yes', 'д', 'да'):
                for i, f, artist, title, matches in files_with_annotations:
                    new_title = title
                    for ann in matches:
                        new_title = remove_annotation_from_text(new_title, ann)
                    tags_to_write[i] = (f, artist, new_title)
                print('  Уточнения будут удалены перед записью.')
            else:
                print('  Уточнения будут сохранены.')

        labels = []
        for c in tags_to_write:
            f, artist, title = c
            new_filename = build_safe_filename(artist, title, f.suffix)

            if new_filename != f.name:
                labels.append(f'{f.name} → {new_filename} (artist: {artist!r}, title: {title!r})')
            else:
                labels.append(f'{f.name} (artist: {artist!r}, title: {title!r})')

        selected_indices = select_indices(
            labels,
            f'Найдено {len(tags_to_write)} файлов с корректным именем, но без тегов. Записать теги?',
            interactor,
        )

        written_count = 0
        renamed_count = 0

        for i in selected_indices:
            f, artist, title = tags_to_write[i]

            if ctx.dry_run:
                ctx.log_debug(f'[DRY RUN] Записал бы теги: {f.name}')
                repo.simulate_update_tags(f, artist, title)
                written_count += 1
            else:
                if repo.update_tags(f, artist, title):
                    ctx.log_debug(f'Записаны теги: {f.name}')
                    ctx.increment_stat('tags_written')
                    written_count += 1
                    new_filename = build_safe_filename(artist, title, f.suffix)

                    if new_filename != f.name:
                        new_path = f.with_name(new_filename)
                        status, result_path = safe_rename(f, new_path, ctx, repo)
                        if status in ('renamed', 'kept_existing'):
                            renamed_count += 1
                            ctx.increment_stat('renamed_by_tags')

        if written_count:
            print(f'  Записано тегов: {written_count}')
        if renamed_count:
            print(f'  Переименовано файлов: {renamed_count}')

    # --- Шаг 5: Поиск дубликатов ---
    stage(5, 'Поиск и объединение дубликатов', ctx)
    files = get_audio_files(target_dir)
    files = process_duplicates(files, ctx, repo)

    # --- Шаг 6: Нормализация имён исполнителей ---
    stage(6, 'Нормализация имён исполнителей', ctx)
    files = get_audio_files(target_dir)
    ctx.log_debug(f'Пересканировано файлов перед этапом 6: {len(files)}')
    artist_variations = find_artist_variations(files, repo)

    unique_normalized = set()

    if artist_variations:
        print(f'\nНайдено {len(artist_variations)} исполнителей с разными вариантами написания.')
        for key, entries in artist_variations.items():
            artist_counts: dict[str, int] = {}
            for _, _, individual_artist in entries:
                artist_counts[individual_artist] = artist_counts.get(individual_artist, 0) + 1

            sorted_artists = sorted(artist_counts.items(), key=lambda x: x[1], reverse=True)

            print(f'\nИсполнитель: {key}')
            print('Варианты написания:')
            for i, (artist, count) in enumerate(sorted_artists, 1):
                print(f'  {i}. {artist} ({count} файлов)')

            choice = interactor.prompt('Выберите номер варианта (n - пропустить) [1]: ')
            if choice in ('n', 'no', 'н', 'нет'):
                continue

            if choice in ('', '1'):
                selected_idx = 0
            else:
                try:
                    selected_idx = int(choice) - 1
                    if not (0 <= selected_idx < len(sorted_artists)):
                        print('Некорректный номер, пропускаю.')
                        continue
                except ValueError:
                    print('Некорректный ввод, выбираю вариант 1.')
                    selected_idx = 0

            canonical = sorted_artists[selected_idx][0]
            ctx.log_debug(f'Обрабатываю исполнителя {key!r}, канонический вариант: {canonical!r}')

            processed_files: set[Path] = set()
            for f, full_artist, individual_artist in entries:
                if not f.exists():
                    ctx.log_debug(f'Файл {f.name} уже не существует, пропускаю')
                    continue
                if f in processed_files:
                    continue

                if individual_artist != canonical:
                    new_full_artist = re.sub(
                        r'\b' + re.escape(individual_artist) + r'\b',
                        canonical,
                        full_artist,
                        flags=re.IGNORECASE,
                    )

                    tags = repo.get_tags(f)
                    if tags:
                        if ctx.dry_run:
                            print(
                                f'  [DRY RUN] Обновил бы тег: {f.name} ({full_artist} -> {new_full_artist})'
                            )
                            repo.simulate_update_tags(f, new_full_artist, tags.title)
                            ctx.increment_stat('artists_normalized')
                            unique_normalized.add(key)
                        elif repo.update_tags(f, new_full_artist, tags.title):
                            ctx.log_debug(
                                f'Обновлено: {f.name} ({full_artist} -> {new_full_artist})'
                            )
                            ctx.increment_stat('artists_normalized')
                            unique_normalized.add(key)

                    parsed = parse_artist_title(f.stem)
                    if parsed and parsed[0] != new_full_artist:
                        raw_name = f'{new_full_artist} - {parsed[1]}'

                        if INVALID_FILENAME_CHARS.search(raw_name):
                            base_name = clean_filename_safely(raw_name, f.name, ctx, interactor)
                            if base_name == 'SKIP':
                                ctx.log_debug(f'Пропущен из-за недопустимых символов: {f.name}')
                                continue
                        else:
                            base_name = raw_name

                        new_name = f'{base_name}{f.suffix}'
                        new_path = f.with_name(new_name)
                        status, result_path = safe_rename(f, new_path, ctx, repo)
                        handle_rename_result(status, f, result_path, ctx, 'renamed_by_tags')

                    processed_files.add(f)

        ctx.stats['artists_normalized_unique'] = len(unique_normalized)

    # --- Шаг 7: Ручной ввод для файлов без метаданных ---
    stage(7, 'Ручной ввод для файлов без метаданных', ctx)

    files = get_audio_files(target_dir)
    still_missing = []

    actually_missing = [f for f in ctx.missing_files if f.exists() and repo.get_tags(f) is None]
    ctx.log_debug(
        f'Файлов без тегов после Этапа 6: {len(actually_missing)} '
        f'(было {len(ctx.missing_files)})'
    )

    if actually_missing:
        labels = [f.name for f in actually_missing]
        selected_indices = select_indices(
            labels,
            f'Найдено {len(actually_missing)} файлов без тегов и формата. Ввести исполнителя вручную?',
            interactor,
        )
        for i in selected_indices:
            f = actually_missing[i]
            if not f.exists():
                continue
            title = extract_title_from_filename(f.stem)
            print(f'\n  Файл: {f.name}')
            print(f'  Предполагаемое название: {title}')
            artist = input('  Введите исполнителя (Enter - пропустить): ').strip()
            if not artist:
                still_missing.append(f)
                continue

            raw_name = f'{artist} - {title}'

            if INVALID_FILENAME_CHARS.search(raw_name):
                base_name = clean_filename_safely(raw_name, f.name, ctx, interactor)
                if base_name == 'SKIP':
                    print(f'  Оставлен без изменений: {f.name}')
                    continue
            else:
                base_name = raw_name

            new_name = f'{base_name}{f.suffix}'
            new_path = f.with_name(new_name)
            status, result_path = safe_rename(f, new_path, ctx, repo)
            handle_rename_result(status, f, result_path, ctx, 'manual_input')

    # --- Финальная статистика ---
    print('\n' + '=' * 60)
    print('=== Статистика обработки ===')
    print('=' * 60)
    print(f'Очистка от мусора (имена): {ctx.stats["junk_removed"]}')
    print(f'Очистка от мусора (теги): {ctx.stats["tags_cleaned"]}')
    print(f'Нормализация тире и пробелов: {ctx.stats["dash_normalized"]}')
    print(f'Удалено дубликатов: {ctx.stats["duplicates_removed"]}')
    print(f'Переименовано по тегам: {ctx.stats["renamed_by_tags"]}')
    print(f'Записано тегов: {ctx.stats["tags_written"]}')
    print(
        f'Нормализовано исполнителей: '
        f'{ctx.stats["artists_normalized"]} файлов '
        f'({ctx.stats.get("artists_normalized_unique", 0)} уникальных)'
    )
    print(f'Ручной ввод: {ctx.stats["manual_input"]}')

    if still_missing:
        print(f'\nВнимание! {len(still_missing)} файлов осталось без метаданных:')
        for f in still_missing:
            print(f'  - {f.name}')
        print('Рекомендация: AcoustID / MusicBrainz или вручную.')

    print('\nОбработка завершена.')


if __name__ == '__main__':
    main()
