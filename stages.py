"""Конкретные этапы (Stages) конвейера обработки аудиофайлов."""

import re
from collections.abc import Callable
from pathlib import Path

from config import (
    FEAT_VARIANTS_PATTERN,
    INVALID_FILENAME_CHARS,
)
from context import SessionContext
from file_operations import FileExecutor
from file_state import OperationStatus
from interactor import UserInteractor, prompt_command
from naming import (
    build_safe_filename,
    normalize_artist_separators,
    normalize_dash,
    normalize_feat_pair,
    normalize_filename_stem,
    normalize_title_spacing,
    preserve_name_qualifiers,
    qualifier_name_candidate,
    remove_feat_guests_from_artist,
    remove_junk,
    title_separator,
)
from parsing import (
    extract_title_from_filename,
    parse_artist_title,
    tags_from_filename,
)
from pipeline_utils import (
    categorize_mismatch,
    clean_filename_safely,
    find_artist_variations,
    modification_allowed,
    process_duplicates,
    select_indices,
)
from strategies import StrategyFactory
from tag_repository import MetadataRead, ReadStatus, TagRepository

_file_executor = FileExecutor()


def process_rename_batch(
    files: list[Path],
    candidates: list[Path],
    transform: Callable[[str], str],
    description: str,
    ctx: SessionContext,
    repo: TagRepository,
    interactor: UserInteractor,
    stat_key: str,
) -> list[Path]:
    """Универсальная обработка пакета переименований."""
    if not candidates:
        return files

    labels = [f'{f.name}  ->  {transform(f.stem)}{f.suffix}' for f in candidates]
    idx = select_indices(labels, f'Найдено {len(candidates)} {description}', interactor)
    chosen = {candidates[i] for i in idx}

    new_files = []
    for f in files:
        if f not in chosen:
            new_files.append(f)
            continue
        new_path = f.with_name(transform(f.stem) + f.suffix)
        result = _file_executor.rename(f, new_path, ctx, repo, interactor)
        if result.is_confirmed_change or result.is_model_change:
            ctx.increment_stat(stat_key)
        if (
            result.is_confirmed_change or result.is_model_change
        ) and result.path is not None:
            new_files.append(result.path)
        else:
            new_files.append(f)

    count = (ctx.planned_stats if ctx.dry_run else ctx.stats).get(stat_key, 0)
    if count:
        label = 'Запланировано переименований' if ctx.dry_run else 'Обработано файлов'
        interactor.display(f'  {label}: {count}')

    return new_files


def _update_tags(
    path: Path,
    artist: str,
    title: str,
    ctx: SessionContext,
    repo: TagRepository,
    interactor: UserInteractor,
) -> bool:
    result = _file_executor.write_tags(path, artist, title, ctx, repo, interactor)
    return result.is_confirmed_change or result.is_model_change


class JunkCleaningStage:
    """Этап 1: Очистка имён файлов и тегов от мусорных вставок (например, vksaver)."""

    @property
    def name(self) -> str:
        return 'Очистка от мусора'

    def execute(
        self,
        files: list[Path],
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> list[Path]:
        files = ctx.present_paths(ctx.file_ids(files))
        # Подэтап 1.1: Очистка имён файлов
        junk_files = [f for f in files if remove_junk(f.stem) != f.stem.strip()]
        current_files = process_rename_batch(
            files=files,
            candidates=junk_files,
            transform=remove_junk,
            description='файлов с мусором (vksaver)',
            ctx=ctx,
            repo=repo,
            interactor=interactor,
            stat_key='junk_removed',
        )

        # Подэтап 1.2: Очистка тегов
        self._clean_tag_junk(current_files, ctx, repo, interactor)

        return current_files

    def _clean_tag_junk(
        self,
        files: list[Path],
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> None:
        """Очищает мусор из тегов исполнителя и названия."""
        candidates = []
        for f in files:
            tags = repo.get_tags(f).complete_tags
            if not tags:
                continue

            artist, title = str(tags.artist), str(tags.title)
            new_artist = remove_junk(artist)
            new_title = remove_junk(title)

            if new_artist != artist or new_title != title:
                candidates.append((f, artist, title, new_artist, new_title))

        if not candidates:
            interactor.display('  Файлов с мусором в тегах не найдено.')
            return

        interactor.display(f'\nНайдено {len(candidates)} файлов с мусором в тегах:')
        for i, (f, artist, title, new_artist, new_title) in enumerate(candidates, 1):
            interactor.display(f'  {i}. {f.name}')
            if new_artist != artist:
                interactor.display(f'      Исполнитель: {artist!r} → {new_artist!r}')
            if new_title != title:
                interactor.display(f'      Название:    {title!r} → {new_title!r}')

        idx = select_indices(
            [f.name for f, *_ in candidates],
            'Какие файлы очистить?',
            interactor,
        )

        cleaned_count = 0
        for i in idx:
            f, _, _, new_artist, new_title = candidates[i]
            if _update_tags(f, new_artist, new_title, ctx, repo, interactor):
                label = 'Запланирована очистка тегов' if ctx.dry_run else 'Теги очищены'
                interactor.display(f'  {label}: {f.name}')
                cleaned_count += 1

        if cleaned_count:
            ctx.increment_stat('tags_cleaned', cleaned_count)


class NormalizationStage:
    """Этап 2: Нормализация тире и пробелов в именах файлов."""

    @property
    def name(self) -> str:
        return 'Нормализация тире и пробелов'

    def execute(
        self,
        files: list[Path],
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> list[Path]:
        files = ctx.present_paths(ctx.file_ids(files))

        def normalize_text(text: str) -> str:
            text = normalize_dash(text)
            return normalize_filename_stem(text)

        candidates = [f for f in files if normalize_text(f.stem) != f.stem]

        return process_rename_batch(
            files=files,
            candidates=candidates,
            transform=normalize_text,
            description='файлов с нестандартными тире или лишними пробелами',
            ctx=ctx,
            repo=repo,
            interactor=interactor,
            stat_key='dash_normalized',
        )


class TagRenamingStage:
    """Этап 3: Переименование файлов по тегам с нормализацией метаданных."""

    @property
    def name(self) -> str:
        return 'Переименование по тегам'

    def execute(
        self,
        files: list[Path],
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> list[Path]:
        files = ctx.present_paths(ctx.file_ids(files))
        interactor.display(
            '\n  [Подэтап 3.1] Нормализация участников (feat./ft./featuring)'
        )
        interactor.display('  Приводим все вариации к единому формату: (feat. Guest)')
        files = self._normalize_feat(files, ctx, repo, interactor)

        interactor.display('\n  [Подэтап 3.2] Нормализация разделителей исполнителей')
        interactor.display(
            "  Нормализуем запятые и точки с запятой: 'Artist1, Artist2'"
        )
        files = self._normalize_separators(files, ctx, repo, interactor)

        interactor.display('\n  [Подэтап 3.3] Формирование новых имён файлов по тегам')
        files = self._rename_by_tags(files, ctx, repo, interactor)

        interactor.display('\n  [Подэтап 3.4] Обработка расхождений имён и тегов')
        files = self._handle_mismatches(files, ctx, repo, interactor)

        interactor.display('\n' + '=' * 60)
        interactor.display('  Этап нормализации тегов и имён завершён.')
        interactor.display('  Переход к поиску дубликатов.')
        interactor.display('=' * 60)

        return files

    # === Подэтап 3.1: feat ===

    def _normalize_feat(
        self,
        files: list[Path],
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> list[Path]:
        feat_candidates: list[tuple[Path, str, str, str, str, str]] = []
        for f in files:
            tags = repo.get_tags(f).complete_tags
            if not tags:
                continue

            artist, title = str(tags.artist), str(tags.title)
            if not (
                FEAT_VARIANTS_PATTERN.search(artist)
                or FEAT_VARIANTS_PATTERN.search(title)
            ):
                continue

            new_artist, new_title = normalize_feat_pair(artist, title)
            new_artist = remove_feat_guests_from_artist(new_artist, new_title)

            if new_artist != artist or new_title != title:
                new_filename = build_safe_filename(
                    new_artist, new_title, f.suffix, source_stem=f.stem
                )
                feat_candidates.append(
                    (f, artist, title, new_artist, new_title, new_filename)
                )

        if not feat_candidates:
            interactor.display('  Файлов для нормализации (feat.) не найдено.')
            return files

        interactor.display(
            f'\nНайдено {len(feat_candidates)} файлов для нормализации (feat.):'
        )
        for i, (f, artist, title, new_artist, new_title, new_filename) in enumerate(
            feat_candidates, 1
        ):
            interactor.display(f'\n  {i}. {f.name}')
            interactor.display('      БЫЛО:')
            interactor.display(f'        Исполнитель: {artist!r}')
            interactor.display(f'        Название:    {title!r}')
            interactor.display('      СТАНЕТ:')
            interactor.display(f'        Исполнитель: {new_artist!r}')
            interactor.display(f'        Название:    {new_title!r}')
            if new_filename != f.name:
                interactor.display(f'        Файл:        {new_filename}')

        selected_indices = select_indices(
            [f.name for f, *_ in feat_candidates],
            '\nКакие файлы нормализовать?',
            interactor,
        )

        normalized_count = 0
        for i in selected_indices:
            f, _, _, new_artist, new_title, _ = feat_candidates[i]
            if _update_tags(f, new_artist, new_title, ctx, repo, interactor):
                ctx.log_debug(f'Нормализован (feat.): {f.name}')
                normalized_count += 1

        if normalized_count:
            interactor.display(f'  Нормализовано (feat.): {normalized_count}')

        return files

    # === Подэтап 3.2: разделители ===

    def _normalize_separators(
        self,
        files: list[Path],
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> list[Path]:
        sep_candidates: list[tuple[Path, str, str, str]] = []
        for f in files:
            tags = repo.get_tags(f).complete_tags
            if not tags:
                continue
            artist, title = str(tags.artist), str(tags.title)
            new_artist = normalize_artist_separators(artist)
            if new_artist != artist:
                sep_candidates.append((f, artist, title, new_artist))

        if not sep_candidates:
            interactor.display('  Файлов для нормализации разделителей не найдено.')
            return files

        interactor.display(
            f'\nНайдено {len(sep_candidates)} файлов для нормализации разделителей:'
        )
        for i, (f, artist, _title, new_artist) in enumerate(sep_candidates, 1):
            interactor.display(f'\n  {i}. {f.name}')
            interactor.display('      БЫЛО:')
            interactor.display(f'        Исполнитель: {artist!r}')
            interactor.display('      СТАНЕТ:')
            interactor.display(f'        Исполнитель: {new_artist!r}')

        selected_indices = select_indices(
            [f.name for f, *_ in sep_candidates],
            '\nКакие файлы нормализовать?',
            interactor,
        )

        normalized_count = 0
        for i in selected_indices:
            f, _, _, new_artist = sep_candidates[i]
            title = sep_candidates[i][2]
            if _update_tags(f, new_artist, title, ctx, repo, interactor):
                ctx.log_debug(f'Нормализован (разделители): {f.name}')
                normalized_count += 1

        if normalized_count:
            interactor.display(f'  Нормализовано разделителей: {normalized_count}')

        return files

    # === Подэтап 3.3: формирование имён по тегам ===

    def _rename_by_tags(
        self,
        files: list[Path],
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> list[Path]:
        ctx.log_debug('Подэтап 3.3: формирование имён из тегов')

        interactor.display('  Поиск файлов с недопустимыми символами...')
        invalid_files = self._collect_invalid_filename_files(files, repo)

        strategies: dict[str, tuple[str, dict]] = {}
        if invalid_files:
            interactor.display(
                f'\n  Найдено {sum(len(v) for v in invalid_files.values())} файлов '
                f'с недопустимыми символами в {len(invalid_files)} группах'
            )

            priority_order = ['bilingual_title', 'artist_name']
            other_keys = [k for k in invalid_files if k not in priority_order]
            ordered_keys = priority_order + sorted(other_keys)

            for problem_key in ordered_keys:
                if problem_key not in invalid_files:
                    continue
                examples = invalid_files[problem_key]
                strategy, params = self._choose_strategy_for_group(
                    problem_key, examples, interactor
                )
                strategies[problem_key] = (strategy, params)

        confirmed: list[tuple[int, Path, str]] = []
        safe_candidates: list[tuple[int, Path, str]] = []
        mismatch_candidates: list[tuple[Path, str, str]] = []
        missing: list[Path] = []

        for position, f in enumerate(files):
            file_title = extract_title_from_filename(f.stem)
            metadata = repo.get_tags(f)
            tags = metadata.complete_tags
            ctx.log_debug(
                f'Анализ: {f.name}, теги: {tags}, извлечённое название: {file_title!r}'
            )

            if tags:
                artist = remove_junk(str(tags.artist))
                title = remove_junk(str(tags.title))
                raw_name = f'{artist} - {title}'
                raw_name = normalize_title_spacing(raw_name)
                user_already_chose = False

                invalid_chars = set(INVALID_FILENAME_CHARS.findall(raw_name))
                if invalid_chars:
                    problem_key = self._invalid_name_group(raw_name, invalid_chars)

                    if self._already_sanitized(raw_name, f.stem, problem_key):
                        continue
                    if problem_key in strategies:
                        strategy, params = strategies[problem_key]

                        if strategy != 'skip':
                            strategy_obj = StrategyFactory.create(
                                strategy, params, interactor, f.name
                            )
                            base_name = strategy_obj.apply(
                                raw_name, set(INVALID_FILENAME_CHARS.findall(raw_name))
                            )
                            if base_name:
                                user_already_chose = True
                                if (
                                    problem_key == 'artist_name'
                                    and strategy == 'remove'
                                ):
                                    # Если исполнитель уже совпадает, остаётся
                                    # отдельное решение о расхождении названий.
                                    current_parts = parse_artist_title(f.stem)
                                    proposed_parts = parse_artist_title(base_name)
                                    if current_parts and proposed_parts:
                                        user_already_chose = (
                                            current_parts[0] != proposed_parts[0]
                                        )
                            else:
                                interactor.display(
                                    f'{f.name}: новое имя не выбрано, файл пропущен.'
                                )
                                continue
                        else:
                            interactor.display(
                                f'{f.name}: выбрано пропустить переименование.'
                            )
                            continue
                    else:
                        interactor.display_error(
                            f'{f.name}: способ исправления имени не выбран.'
                        )
                        continue
                else:
                    base_name = raw_name

                proposed, ambiguous = qualifier_name_candidate(base_name, f.stem)
                new_name = f'{proposed}{f.suffix}'
                if new_name == f.name:
                    continue
                if ambiguous:
                    interactor.display(
                        f'  Разная пунктуация в уточнениях: {f.name} → {new_name}'
                    )
                    choice = prompt_command(
                        interactor,
                        '  t — выбрать написание из тегов, Enter — оставить имя: ',
                        default='n',
                    )
                    if choice == 't':
                        confirmed.append((position, f, new_name))
                    continue

                if user_already_chose or self._titles_match(file_title, title):
                    safe_candidates.append((position, f, new_name))
                else:
                    mismatch_candidates.append((f, file_title, new_name))
            elif metadata.status is ReadStatus.EMPTY:
                missing.append(f)

        resulting_files = files.copy()
        if safe_candidates:
            labels = [f'{f.name}  ->  {new_name}' for _, f, new_name in safe_candidates]
            selected_indices = select_indices(
                labels,
                f'Найдено {len(safe_candidates)} файлов для переименования по тегам (названия совпадают)',
                interactor,
            )

            confirmed.extend(safe_candidates[i] for i in selected_indices)

        if confirmed:
            renamed_count = 0
            duplicates_found: list[tuple[Path, Path]] = []

            for position, old_path, new_name in confirmed:
                new_path = old_path.with_name(new_name)
                result = _file_executor.rename(
                    old_path, new_path, ctx, repo, interactor
                )

                if (
                    result.is_confirmed_change or result.is_model_change
                ) and result.path is not None:
                    resulting_files[position] = result.path
                if result.is_confirmed_change or result.is_model_change:
                    ctx.increment_stat('renamed_by_tags')
                    renamed_count += 1
                elif result.status is OperationStatus.CONFLICT:
                    duplicates_found.append((old_path, new_path))

            if renamed_count:
                label = (
                    'Запланировано переименований'
                    if ctx.dry_run
                    else 'Переименовано файлов'
                )
                interactor.display(f'  {label}: {renamed_count}')

            if duplicates_found:
                self._handle_rename_duplicates(duplicates_found, ctx, repo, interactor)

        # Сохраняем missing для этапа 7
        ctx.missing_files = missing
        # Сохраняем mismatch_candidates для подэтапа 3.5
        self._pending_mismatches = mismatch_candidates

        return resulting_files

    # === Подэтап 3.4: обработка расхождений ===

    def _handle_mismatches(
        self,
        files: list[Path],
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> list[Path]:
        mismatch_candidates: list[tuple[Path, str, str]] = getattr(
            self, '_pending_mismatches', []
        )
        if not mismatch_candidates:
            return files

        # Фильтруем несуществующие и уже корректные
        active_mismatch: list[tuple[Path, str, str]] = []
        for f, file_title, _old_new_name in mismatch_candidates:
            current = ctx.present_path(ctx.registry.track(f).id)
            if current is None:
                continue
            f = current
            tags = repo.get_tags(f).complete_tags
            if not tags:
                continue

            artist = remove_junk(str(tags.artist))
            title = remove_junk(str(tags.title))
            current_new_name = build_safe_filename(
                artist, title, f.suffix, source_stem=f.stem
            )

            if current_new_name == f.name:
                ctx.log_debug(f'Файл {f.name} уже имеет корректное имя, пропускаю')
                continue

            file_title = extract_title_from_filename(f.stem)
            if self._titles_match(file_title, title):
                ctx.log_debug(f'Файл {f.name} теперь совпадает с тегами, пропускаю')
                continue

            active_mismatch.append((f, file_title, current_new_name))

        if not active_mismatch:
            return files

        renamed_paths: dict[Path, Path] = {}

        def rename_by_tags(path: Path, new_name: str) -> None:
            result = _file_executor.rename(
                path, path.with_name(new_name), ctx, repo, interactor
            )
            if result.is_confirmed_change or result.is_model_change:
                ctx.increment_stat('renamed_by_tags')
            if (
                result.is_confirmed_change or result.is_model_change
            ) and result.path is not None:
                renamed_paths[path] = result.path

        interactor.display(
            f'\n  Найдено {len(active_mismatch)} файлов с расхождением названий (файл vs теги):'
        )

        categorized: dict[str, list[tuple[Path, str, str, str, str]]] = {
            'A': [],
            'B': [],
            'C': [],
            'D': [],
            'E': [],
        }

        for f, file_title, new_name in active_mismatch:
            tags = repo.get_tags(f).complete_tags
            if not tags:
                continue
            tag_artist, tag_title = tags.artist, tags.title
            category = categorize_mismatch(f, file_title, tag_title, tag_artist)
            categorized[category].append(
                (f, file_title, new_name, tag_artist, tag_title)
            )

        category_names: dict[str, str] = {
            'A': 'Файлы с номерами треков',
            'B': 'Файлы без тире',
            'C': 'Файлы с feat в имени',
            'D': 'Проблемы с кодировкой',
            'E': 'Спорные случаи',
        }
        category_hints: dict[str, str] = {
            'A': '💡 В имени нет исполнителя',
            'B': '💡 Имя не в формате Artist - Title',
            'C': '💡 В имени есть "feat" вне скобок',
            'D': '💡 Теги содержат кракозябры',
            'E': '💡 Имя и теги различаются',
        }
        category_recommendations: dict[str, str] = {
            'A': 'Переименовать по тегам',
            'B': 'Переименовать по тегам',
            'C': 'Переименовать по тегам',
            'D': 'Обновить теги из имён',
            'E': 'Ручное решение',
        }
        category_defaults: dict[str, str | None] = {
            'A': 't',
            'B': 't',
            'C': 't',
            'D': 'f',
            'E': None,
        }

        for cat_key in ['A', 'B', 'C', 'D', 'E']:
            if not categorized[cat_key]:
                continue

            interactor.display(f'\n{"=" * 60}')
            interactor.display(
                f'Группа {cat_key}: {category_names[cat_key]} - {len(categorized[cat_key])} файлов'
            )
            interactor.display(f'{"=" * 60}')
            interactor.display(category_hints[cat_key])
            interactor.display(f'→ Рекомендация: {category_recommendations[cat_key]}')
            interactor.display('')

            for i, (f, file_title, new_name, tag_artist, tag_title) in enumerate(
                categorized[cat_key], 1
            ):
                tag_str = f'{tag_artist} - {tag_title}'
                interactor.display(f'  {i}. {f.name}')

                if cat_key == 'D':
                    proposed = tags_from_filename(f.stem, tag_artist, tag_title)
                    if proposed:
                        interactor.display('      Файл останется без изменений')
                        interactor.display(f'      Теги: {tag_str}')
                        interactor.display(f'         → {proposed[0]} - {proposed[1]}')
                elif cat_key == 'E':
                    interactor.display(f'      Файл: {file_title or "(нет)"}')
                    interactor.display(f'      Теги: {tag_str}')
                    interactor.display(f'      → {new_name}')
                else:
                    interactor.display(f'      → {new_name}')

            if cat_key != 'E':
                default = category_defaults[cat_key]
                action_name = (
                    'переименовать по тегам'
                    if default == 't'
                    else 'обновить теги из имён'
                )

                choice = prompt_command(
                    interactor,
                    f"\nПрименить '{action_name}' ко всей группе? (y/n) [y]: ",
                    default='y',
                )

                if choice in ('', 'y', 'yes', 'д', 'да'):
                    for (
                        f,
                        _file_title,
                        new_name,
                        tag_artist,
                        tag_title,
                    ) in categorized[cat_key]:
                        if default == 't':
                            rename_by_tags(f, new_name)
                        else:
                            proposed = tags_from_filename(f.stem, tag_artist, tag_title)
                            if (
                                proposed
                                and proposed != (tag_artist, tag_title)
                                and _update_tags(f, *proposed, ctx, repo, interactor)
                            ):
                                ctx.log_debug(f'Теги обновлены из имени: {f.name}')
                                ctx.increment_stat('tags_written')
                else:
                    interactor.display(f'  Группа {cat_key} пропущена.')

            else:
                interactor.display('\nДля каждого файла выберите действие:')
                interactor.display(
                    '  t - использовать данные из тега (переименовать файл)'
                )
                interactor.display('  f - использовать данные из файла (обновить тег)')
                interactor.display('  n - пропустить')

                last_action = 'n'

                for (
                    f,
                    file_title,
                    current_new_name,
                    tag_artist,
                    tag_title,
                ) in categorized[cat_key]:
                    interactor.display(f'\n  {f.name}')
                    interactor.display(f'      Файл: {file_title or "(нет)"}')
                    interactor.display(f'      Теги: {tag_artist} - {tag_title}')

                    parsed = tags_from_filename(
                        f.stem, tag_artist, tag_title, repair_brackets=False
                    )

                    if last_action == 't':
                        interactor.display(f'      → Новое имя (t): {current_new_name}')
                    elif last_action == 'f':
                        if parsed:
                            interactor.display(
                                f'      → Новые теги (f): {parsed[0]} - {parsed[1]}'
                            )
                        else:
                            interactor.display(
                                f'      → Новые теги (f): {file_title or f.stem}'
                            )
                    else:
                        interactor.display(f'      → Если t: {current_new_name}')
                        if parsed:
                            interactor.display(
                                f'      → Если f: {parsed[0]} - {parsed[1]}'
                            )

                    choice = prompt_command(
                        interactor,
                        f'      Действие [t/f/n] ({last_action}): ',
                        default=last_action,
                    )

                    if choice == last_action and not choice:
                        interactor.display(
                            f'      [Использовано предыдущее действие: {choice}]'
                        )
                    else:
                        last_action = choice

                    if choice == 't':
                        rename_by_tags(f, current_new_name)
                    elif choice == 'f':
                        if (
                            parsed
                            and parsed != (tag_artist, tag_title)
                            and _update_tags(
                                f, parsed[0], parsed[1], ctx, repo, interactor
                            )
                        ):
                            ctx.log_debug(f'Теги обновлены из имени: {f.name}')
                            ctx.increment_stat('tags_written')
                    else:
                        interactor.display(f'      Пропущен: {f.name}')

        return [renamed_paths.get(path, path) for path in files]

    # === Обработка дубликатов при переименовании ===

    def _handle_rename_duplicates(
        self,
        duplicates_found: list[tuple[Path, Path]],
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> None:
        interactor.display(f'\n  Конфликтов целевого имени: {len(duplicates_found)}')
        for old_path, new_path in duplicates_found:
            interactor.display(f'    {old_path.name} → {new_path.name}')
        interactor.display('  Объединение отключено; оба файла сохранены.')

    # === Приватные вспомогательные методы (чистая логика) ===

    def _collect_invalid_filename_files(
        self, files: list[Path], repo: TagRepository
    ) -> dict[str, list[tuple[Path, str, str]]]:
        """Собирает файлы с недопустимыми символами и группирует их."""
        problems: dict[str, list[tuple[Path, str, str]]] = {}

        for f in files:
            tags = repo.get_tags(f).complete_tags
            if not tags:
                continue

            artist = remove_junk(str(tags.artist))
            title = remove_junk(str(tags.title))
            raw_name = f'{artist} - {title}'
            raw_name = normalize_title_spacing(raw_name)

            invalid_chars = set(INVALID_FILENAME_CHARS.findall(raw_name))
            if not invalid_chars:
                continue

            problem_key = self._invalid_name_group(raw_name, invalid_chars)
            if self._already_sanitized(raw_name, f.stem, problem_key):
                continue

            if problem_key not in problems:
                problems[problem_key] = []

            problems[problem_key].append((f, f.name, raw_name))

        return problems

    def _choose_strategy_for_group(
        self,
        problem_key: str,
        examples: list[tuple[Path, str, str]],
        interactor: UserInteractor,
    ) -> tuple[str, dict]:
        """Запрашивает у пользователя стратегию обработки для группы файлов."""
        if problem_key == 'qualifier_slash':
            interactor.display("  Слэш внутри уточнения: заменить '/' на '_' в имени?")
            for path, _, raw_name in examples:
                proposed, _ = qualifier_name_candidate(
                    raw_name.replace('/', '_'), path.stem
                )
                interactor.display(f'  {path.name} → {proposed}{path.suffix}')
            choice = prompt_command(
                interactor, '  1 — заменить, n — пропустить [1]: ', default='1'
            )
            return ('replace', {'char': '_'}) if choice == '1' else ('skip', {})

        if problem_key == 'bilingual_title':
            default_strategy = 'first_title'
        elif problem_key == 'artist_name':
            default_strategy = 'remove'
        else:
            default_strategy = 'remove'

        chars_str = problem_key[6:] if problem_key.startswith('other:') else '/'

        interactor.display(
            '\n  Будет применено по умолчанию: ',
        )

        if problem_key == 'bilingual_title':
            interactor.display('оставить первое название')
        elif problem_key == 'artist_name':
            interactor.display("удалить '/'")
        else:
            interactor.display(f"удалить '{chars_str}'")

        interactor.display('')
        for i, (f, current_name, proposed_name) in enumerate(examples, 1):
            strategy_obj = StrategyFactory.create(
                default_strategy, {}, interactor, f.name
            )
            preview_result = strategy_obj.apply(
                proposed_name, set(INVALID_FILENAME_CHARS.findall(proposed_name))
            )
            interactor.display(f'  {i}. {current_name}')
            interactor.display(f'     {proposed_name}')
            if preview_result:
                interactor.display(
                    f'     → {preserve_name_qualifiers(preview_result, f.stem)}'
                )
            else:
                interactor.display(f'     → {proposed_name}')

        interactor.display(f'\n  Как обработать все {len(examples)} файлов?')

        if problem_key == 'bilingual_title':
            interactor.display("  1. Оставить первое название (до '/') [по умолчанию]")
            interactor.display("  2. Оставить второе название (после '/')")
            interactor.display('  3. Ввести имя вручную для каждого файла')
            interactor.display('  4. Пропустить все файлы')

            choice = prompt_command(interactor, '  Выберите вариант [1]: ', default='1')

            try:
                idx = int(choice or '1')
            except ValueError:
                interactor.display_error('Некорректный вариант. Группа пропущена.')
                return ('skip', {})

            if idx == 1:
                return ('first_title', {})
            if idx == 2:
                return ('second_title', {})
            if idx == 3:
                return ('manual', {})
            return ('skip', {})

        if problem_key == 'artist_name':
            interactor.display("  1. Удалить '/' из имени [по умолчанию]")
            interactor.display('  2. Ввести имя вручную для каждого файла')
            interactor.display('  3. Пропустить все файлы')

            choice = prompt_command(interactor, '  Выберите вариант [1]: ', default='1')

            try:
                idx = int(choice or '1')
            except ValueError:
                interactor.display_error('Некорректный вариант. Группа пропущена.')
                return ('skip', {})

            if idx == 1:
                return ('remove', {})
            if idx == 2:
                return ('manual', {})
            return ('skip', {})

        interactor.display(f"  1. Удалить символы '{chars_str}' [по умолчанию]")
        interactor.display(f"  2. Заменить '{chars_str}' на '-'")
        interactor.display('  3. Ввести имя вручную для каждого файла')
        interactor.display('  4. Пропустить все файлы')

        choice = prompt_command(interactor, '  Выберите вариант [1]: ', default='1')

        try:
            idx = int(choice or '1')
        except ValueError:
            interactor.display_error('Некорректный вариант. Группа пропущена.')
            return ('skip', {})

        if idx == 1:
            return ('remove', {})
        if idx == 2:
            return ('replace', {'char': '-'})
        if idx == 3:
            return ('manual', {})
        return ('skip', {})

    @staticmethod
    def _already_sanitized(raw_name: str, source_stem: str, problem_key: str) -> bool:
        cleaned = (
            raw_name.replace('/', '_')
            if problem_key == 'qualifier_slash'
            else INVALID_FILENAME_CHARS.sub('', raw_name)
        )
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        return qualifier_name_candidate(cleaned, source_stem)[0] == source_stem

    @staticmethod
    def _invalid_name_group(raw_name: str, invalid_chars: set[str]) -> str:
        if '/' in invalid_chars:
            artist, _, title = raw_name.partition(' - ')
            if '/' in artist:
                return 'artist_name'
            if title_separator(title) is not None:
                return 'bilingual_title'
            if invalid_chars == {'/'}:
                return 'qualifier_slash'
        return 'other:' + ''.join(sorted(invalid_chars))

    def _titles_match(self, file_title: str, tag_title: str) -> bool:
        """Проверяет, совпадают ли названия (с допуском на различия)."""
        f = file_title.lower().strip()
        t = preserve_name_qualifiers(tag_title, file_title).lower().strip()
        if f == t:
            return True
        return f in t or t in f


class TagWritingStage:
    """Этап 4: Запись тегов из имени файла для файлов без тегов."""

    @property
    def name(self) -> str:
        return 'Запись тегов из имени файла'

    def execute(
        self,
        files: list[Path],
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> list[Path]:
        file_ids = ctx.file_ids(files)
        files = ctx.present_paths(file_ids)
        tags_to_write: list[tuple[Path, MetadataRead, str, str]] = []

        for f in files:
            parsed = parse_artist_title(f.stem)
            if not parsed:
                continue
            metadata = repo.get_tags(f)
            tags = metadata.tags
            if tags is None or metadata.status is ReadStatus.COMPLETE:
                continue
            artist, title = parsed
            artist = remove_junk(artist)
            title = remove_junk(title)
            artist, title = normalize_feat_pair(artist, title)
            artist = remove_feat_guests_from_artist(artist, tags.title or title)
            tags_to_write.append(
                (f, metadata, tags.artist or artist, tags.title or title)
            )

        if not tags_to_write:
            return files

        labels: list[str] = []
        for f, _, artist, title in tags_to_write:
            new_filename = build_safe_filename(
                artist, title, f.suffix, source_stem=f.stem
            )
            if new_filename != f.name:
                labels.append(
                    f'{f.name} → {new_filename} (artist: {artist!r}, title: {title!r})'
                )
            else:
                labels.append(f'{f.name} (artist: {artist!r}, title: {title!r})')

        selected_indices = select_indices(
            labels,
            f'Найдено {len(tags_to_write)} файлов с корректным именем и незаполненными тегами. Записать теги?',
            interactor,
        )

        written_count = 0
        renamed_count = 0

        for i in selected_indices:
            f, metadata, artist, title = tags_to_write[i]
            current_metadata = repo.get_tags(f)
            if current_metadata != metadata:
                reason = current_metadata.error or 'метаданные изменились после выбора'
                interactor.display_error(f'{f.name}: запись тегов пропущена: {reason}')
                continue

            result = _file_executor.write_tags(f, artist, title, ctx, repo, interactor)
            if not (result.is_confirmed_change or result.is_model_change):
                continue
            written_count += 1
            ctx.increment_stat('tags_written')
            new_filename = build_safe_filename(
                artist, title, f.suffix, source_stem=f.stem
            )
            if new_filename != f.name:
                renamed = _file_executor.rename(
                    f, f.with_name(new_filename), ctx, repo, interactor
                )
                if renamed.is_confirmed_change or renamed.is_model_change:
                    ctx.increment_stat('renamed_by_tags')
                    renamed_count += 1

        if written_count:
            label = 'Запланировано записей тегов' if ctx.dry_run else 'Записано тегов'
            interactor.display(f'  {label}: {written_count}')
        if renamed_count:
            label = (
                'Запланировано переименований'
                if ctx.dry_run
                else 'Переименовано файлов'
            )
            interactor.display(f'  {label}: {renamed_count}')

        return ctx.present_paths(file_ids)


class DuplicateResolutionStage:
    """Этап 5: Анализ кандидатов и применение подтверждённых решений в deep."""

    @property
    def name(self) -> str:
        return 'Поиск кандидатов в дубликаты'

    def execute(
        self,
        files: list[Path],
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> list[Path]:
        return process_duplicates(
            ctx.present_paths(ctx.file_ids(files)), ctx, repo, interactor
        )


class ArtistNormalizationStage:
    """Этап 6: Нормализация имён исполнителей."""

    @property
    def name(self) -> str:
        return 'Нормализация имён исполнителей'

    def execute(
        self,
        files: list[Path],
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> list[Path]:
        file_ids = ctx.file_ids(files)
        files = ctx.present_paths(file_ids)

        artist_variations = find_artist_variations(files, repo)
        ids_by_path = {path: ctx.registry.track(path).id for path in files}
        unique_normalized: set[str] = set()

        if not artist_variations:
            return files

        interactor.display(
            f'\nНайдено {len(artist_variations)} исполнителей с разными вариантами написания.'
        )

        for key, entries in artist_variations.items():
            artist_counts: dict[str, int] = {}
            for _, _, individual_artist in entries:
                artist_counts[individual_artist] = (
                    artist_counts.get(individual_artist, 0) + 1
                )

            sorted_artists = sorted(
                artist_counts.items(), key=lambda x: x[1], reverse=True
            )

            interactor.display(f'\nИсполнитель: {key}')
            interactor.display('Варианты написания:')
            for i, (artist, count) in enumerate(sorted_artists, 1):
                interactor.display(f'  {i}. {artist} ({count} файлов)')

            choice = prompt_command(
                interactor, 'Выберите номер варианта (n - пропустить) [1]: '
            )
            if choice in ('n', 'no', 'н', 'нет'):
                continue

            if choice in ('', '1'):
                selected_idx = 0
            else:
                try:
                    selected_idx = int(choice) - 1
                    if not (0 <= selected_idx < len(sorted_artists)):
                        interactor.display('Некорректный номер, пропускаю.')
                        continue
                except ValueError:
                    interactor.display_error('Некорректный ввод. Группа пропущена.')
                    continue

            canonical = sorted_artists[selected_idx][0]
            ctx.log_debug(
                f'Обрабатываю исполнителя {key!r}, канонический вариант: {canonical!r}'
            )

            processed_files: set[int] = set()
            for original, _, individual_artist in entries:
                file_id = ids_by_path[original]
                if file_id in processed_files or individual_artist == canonical:
                    continue
                f = ctx.present_path(file_id)
                if f is None or (tags := repo.get_tags(f).complete_tags) is None:
                    continue

                new_full_artist = re.sub(
                    r'\b' + re.escape(individual_artist) + r'\b',
                    canonical,
                    tags.artist,
                    flags=re.IGNORECASE,
                )
                result = _file_executor.write_tags(
                    f, new_full_artist, tags.title, ctx, repo, interactor
                )
                if not (result.is_confirmed_change or result.is_model_change):
                    continue
                processed_files.add(file_id)
                ctx.increment_stat('artists_normalized')
                unique_normalized.add(key)

                parsed = parse_artist_title(f.stem)
                if parsed and parsed[0] != new_full_artist:
                    raw_name = f'{new_full_artist} - {parsed[1]}'

                    if INVALID_FILENAME_CHARS.search(raw_name):
                        base_name = clean_filename_safely(
                            raw_name, f.name, ctx, interactor
                        )
                        if not base_name or base_name == 'SKIP':
                            ctx.log_debug(
                                f'Пропущен из-за недопустимых символов: {f.name}'
                            )
                            continue
                    else:
                        base_name = raw_name

                    new_name = (
                        f'{preserve_name_qualifiers(base_name, f.stem)}{f.suffix}'
                    )
                    new_path = f.with_name(new_name)
                    renamed = _file_executor.rename(f, new_path, ctx, repo, interactor)
                    if renamed.is_confirmed_change or renamed.is_model_change:
                        ctx.increment_stat('renamed_by_tags')

        ctx.increment_stat('artists_normalized_unique', len(unique_normalized))
        return ctx.present_paths(file_ids)


class ManualInputStage:
    """Этап 7: Заполнение отсутствующих полей тегов."""

    @property
    def name(self) -> str:
        return 'Ручное заполнение отсутствующих тегов'

    def execute(
        self,
        files: list[Path],
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> list[Path]:
        file_ids = ctx.file_ids(files)
        files = ctx.present_paths(file_ids)

        candidates: list[tuple[Path, MetadataRead]] = []
        for f in files:
            metadata = repo.get_tags(f)
            if metadata.tags is None:
                interactor.display_error(
                    f'{f.name}: обработка тегов пропущена: '
                    f'{metadata.error or "чтение метаданных не подтверждено"}'
                )
            elif metadata.status is not ReadStatus.COMPLETE:
                candidates.append((f, metadata))
        ctx.missing_files = [f for f, _ in candidates]

        if not candidates:
            ctx._still_missing = []  # type: ignore[attr-defined]
            return files

        selected_indices = select_indices(
            [f.name for f, _ in candidates],
            f'Найдено {len(candidates)} файлов с незаполненными тегами. Заполнить вручную?',
            interactor,
        )

        for i in selected_indices:
            f, metadata = candidates[i]
            tags = metadata.tags
            assert tags is not None
            if not modification_allowed(f, ctx, interactor):
                continue
            title = tags.title or extract_title_from_filename(f.stem)
            interactor.display(f'\n  Файл: {f.name}')
            artist = tags.artist
            if not artist:
                title_label = 'Название из тега' if tags.title else 'Название из имени'
                interactor.display(f'  {title_label}: {title}')
                artist = interactor.prompt(
                    '  Введите исполнителя (Enter - пропустить): '
                ).strip()
            if not artist:
                continue
            if not tags.title and (tags.artist or not title):
                title = interactor.prompt(
                    '  Введите название (Enter - пропустить): '
                ).strip()
            if not title:
                continue

            current_metadata = repo.get_tags(f)
            if current_metadata != metadata:
                reason = current_metadata.error or 'метаданные изменились после выбора'
                interactor.display_error(f'{f.name}: запись тегов пропущена: {reason}')
                continue
            result = _file_executor.write_tags(f, artist, title, ctx, repo, interactor)
            if not (result.is_confirmed_change or result.is_model_change):
                continue
            ctx.increment_stat('manual_input')

            raw_name = f'{artist} - {title}'

            if INVALID_FILENAME_CHARS.search(raw_name):
                base_name = clean_filename_safely(raw_name, f.name, ctx, interactor)
                if not base_name or base_name == 'SKIP':
                    interactor.display(f'  Переименование пропущено: {f.name}')
                    continue
            else:
                base_name = raw_name

            new_name = f'{preserve_name_qualifiers(base_name, f.stem)}{f.suffix}'
            new_path = f.with_name(new_name)
            if new_path.name != f.name:
                _file_executor.rename(f, new_path, ctx, repo, interactor)

        ctx._still_missing = ctx.missing_files.copy()  # type: ignore[attr-defined]
        return ctx.present_paths(file_ids)
