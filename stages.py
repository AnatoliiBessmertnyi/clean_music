"""Конкретные этапы (Stages) конвейера обработки аудиофайлов."""

import re
from pathlib import Path
from typing import Any

from config import (
    FEAT_VARIANTS_PATTERN,
    INVALID_FILENAME_CHARS,
    JUNK_PATTERN,
    SUPPORTED_EXTENSIONS,
)
from context import SessionContext
from interactor import UserInteractor
from naming import (
    build_safe_filename,
    normalize_artist_separators,
    normalize_dash,
    normalize_feat_pair,
    normalize_filename_stem,
    normalize_title_spacing,
    remove_annotation_from_text,
    remove_feat_guests_from_artist,
    remove_junk,
)
from parsing import clean_extracted_tags, extract_title_from_filename, parse_artist_title
from pipeline_utils import (
    categorize_mismatch,
    clean_filename_safely,
    file_info_line,
    find_artist_variations,
    handle_rename_result,
    name_quality_score,
    process_duplicates,
    process_rename_batch,
    safe_rename,
    select_indices,
)
from strategies import StrategyFactory
from tag_repository import TagRepository


class JunkCleaningStage:
    """Этап 1: Очистка имён файлов и тегов от мусорных вставок (например, vksaver)."""

    @property
    def name(self) -> str:
        return "Очистка от мусора"

    def execute(
        self,
        files: list[Path],
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> list[Path]:
        # Подэтап 1.1: Очистка имён файлов
        junk_files = [f for f in files if JUNK_PATTERN.search(f.stem)]
        current_files = process_rename_batch(
            files=files,
            candidates=junk_files,
            transform=remove_junk,
            description="файлов с мусором (vksaver)",
            ctx=ctx,
            repo=repo,
            interactor=interactor,
            stat_key="junk_removed",
        )

        # Подэтап 1.2: Очистка тегов
        self._clean_tag_junk(current_files, ctx, repo, interactor)

        return current_files

    def _clean_tag_junk(
        self, files: list[Path], ctx: SessionContext, repo: TagRepository, interactor: UserInteractor
    ) -> None:
        """Очищает мусор из тегов исполнителя и названия."""
        candidates = []
        for f in files:
            if not f.exists():
                continue
            tags = repo.get_tags(f)
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
            if ctx.dry_run:
                ctx.log_debug(f'[DRY RUN] Очистил бы теги: {f.name}')
                repo.simulate_update_tags(f, new_artist, new_title)
                cleaned_count += 1
            elif repo.update_tags(f, new_artist, new_title):
                interactor.display(f'  Теги очищены: {f.name}')
                cleaned_count += 1

        if cleaned_count:
            ctx.stats['tags_cleaned'] = cleaned_count
            interactor.display(f'  Очищено тегов: {cleaned_count}')


class NormalizationStage:
    """Этап 2: Нормализация тире и пробелов в именах файлов."""

    @property
    def name(self) -> str:
        return "Нормализация тире и пробелов"

    def execute(
        self,
        files: list[Path],
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> list[Path]:
        def normalize_text(text: str) -> str:
            text = normalize_dash(text)
            text = normalize_filename_stem(text)
            return text

        candidates = [f for f in files if normalize_text(f.stem) != f.stem]

        return process_rename_batch(
            files=files,
            candidates=candidates,
            transform=normalize_text,
            description="файлов с нестандартными тире или лишними пробелами",
            ctx=ctx,
            repo=repo,
            interactor=interactor,
            stat_key="dash_normalized",
        )


class TagRenamingStage:
    """Этап 3: Переименование файлов по тегам с нормализацией метаданных."""

    @property
    def name(self) -> str:
        return "Переименование по тегам"

    def execute(
        self,
        files: list[Path],
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> list[Path]:
        interactor.display('\n  [Подэтап 3.1] Нормализация участников (feat./ft./featuring)')
        interactor.display('  Приводим все вариации к единому формату: (feat. Guest)')
        files = self._normalize_feat(files, ctx, repo, interactor)

        interactor.display('\n  [Подэтап 3.2] Нормализация разделителей исполнителей')
        interactor.display("  Приводим все разделители к запятой с пробелом: 'Artist1, Artist2'")
        files = self._normalize_separators(files, ctx, repo, interactor)

        interactor.display('\n  [Подэтап 3.3] Анализ уточнений в скобках')
        interactor.display('  Находим уточнения (ремастеринг, версии, саундтреки)')
        interactor.display('  и предлагаем удалить лишние из тегов.')
        files = self._process_annotations(files, ctx, repo, interactor)

        interactor.display('\n  [Подэтап 3.4] Формирование новых имён файлов по тегам')
        files = self._rename_by_tags(files, ctx, repo, interactor)

        interactor.display('\n  [Подэтап 3.5] Обработка расхождений имён и тегов')
        files = self._handle_mismatches(files, ctx, repo, interactor)

        interactor.display('\n' + '=' * 60)
        interactor.display('  Этап нормализации тегов и имён завершён.')
        interactor.display('  Переход к поиску дубликатов.')
        interactor.display('=' * 60)

        return files

    # === Подэтап 3.1: feat ===

    def _normalize_feat(
        self, files: list[Path], ctx: SessionContext, repo: TagRepository, interactor: UserInteractor
    ) -> list[Path]:
        feat_candidates: list[tuple[Path, str, str, str, str, str]] = []
        for f in files:
            if not f.exists():
                continue
            tags = repo.get_tags(f)
            if not tags:
                continue

            artist, title = str(tags.artist), str(tags.title)
            if not (FEAT_VARIANTS_PATTERN.search(artist) or FEAT_VARIANTS_PATTERN.search(title)):
                continue

            new_artist, new_title = normalize_feat_pair(artist, title)
            new_artist = remove_feat_guests_from_artist(new_artist, new_title)

            if new_artist != artist or new_title != title:
                new_filename = build_safe_filename(new_artist, new_title, f.suffix)
                feat_candidates.append((f, artist, title, new_artist, new_title, new_filename))

        if not feat_candidates:
            interactor.display('  Файлов для нормализации (feat.) не найдено.')
            return files

        interactor.display(f'\nНайдено {len(feat_candidates)} файлов для нормализации (feat.):')
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
            if ctx.dry_run:
                ctx.log_debug(f'[DRY RUN] Нормализовал бы (feat.): {f.name}')
                repo.simulate_update_tags(f, new_artist, new_title)
                normalized_count += 1
            elif repo.update_tags(f, new_artist, new_title):
                ctx.log_debug(f'Нормализован (feat.): {f.name}')
                normalized_count += 1

        if normalized_count:
            interactor.display(f'  Нормализовано (feat.): {normalized_count}')

        return files

    # === Подэтап 3.2: разделители ===

    def _normalize_separators(
        self, files: list[Path], ctx: SessionContext, repo: TagRepository, interactor: UserInteractor
    ) -> list[Path]:
        sep_candidates: list[tuple[Path, str, str, str]] = []
        for f in files:
            if not f.exists():
                continue
            tags = repo.get_tags(f)
            if not tags:
                continue
            artist, title = str(tags.artist), str(tags.title)
            new_artist = normalize_artist_separators(artist)
            if new_artist != artist:
                sep_candidates.append((f, artist, title, new_artist))

        if not sep_candidates:
            interactor.display('  Файлов для нормализации разделителей не найдено.')
            return files

        interactor.display(f'\nНайдено {len(sep_candidates)} файлов для нормализации разделителей:')
        for i, (f, artist, title, new_artist) in enumerate(sep_candidates, 1):
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
            if ctx.dry_run:
                ctx.log_debug(f'[DRY RUN] Нормализовал бы разделители: {f.name}')
                repo.simulate_update_tags(f, new_artist, title)
                normalized_count += 1
            elif repo.update_tags(f, new_artist, title):
                ctx.log_debug(f'Нормализован (разделители): {f.name}')
                normalized_count += 1

        if normalized_count:
            interactor.display(f'  Нормализовано разделителей: {normalized_count}')

        return files

    # === Подэтап 3.3: уточнения в скобках ===

    def _process_annotations(
        self, files: list[Path], ctx: SessionContext, repo: TagRepository, interactor: UserInteractor
    ) -> list[Path]:
        annotations = self._find_tag_annotations(files, repo)
        if not annotations:
            return files

        # Автоудаление уточнений с недопустимыми символами
        problematic = [ann for ann in annotations if INVALID_FILENAME_CHARS.search(ann)]
        if problematic:
            interactor.display(
                f'\n  Внимание: {len(problematic)} уточнений содержат '
                f'недопустимые символы и будут удалены автоматически:'
            )
            for ann in problematic:
                interactor.display(f'    - ({ann})')

            removed_count = 0
            for ann in problematic:
                for f in annotations[ann]:
                    if not f.exists():
                        continue
                    tags = repo.get_tags(f)
                    if tags:
                        new_title = remove_annotation_from_text(tags.title, ann)
                        if new_title != tags.title:
                            if ctx.dry_run:
                                ctx.log_debug(f'[DRY RUN] Автоудалил бы: ({ann}) из {f.name}')
                                repo.simulate_update_tags(f, tags.artist, new_title)
                                removed_count += 1
                            elif repo.update_tags(f, tags.artist, new_title):
                                ctx.log_debug(f'Автоудалено: ({ann}) из {f.name}')
                                removed_count += 1
            if removed_count:
                interactor.display(f'  Автоматически удалено: {removed_count}')
                for ann in problematic:
                    del annotations[ann]

        if not annotations:
            return files

        categories = self._categorize_annotations(annotations)

        interactor.display(f'\nНайдено {len(annotations)} уникальных уточнений в {len(categories)} категориях:')

        for cat_key, cat_data in categories.items():
            total_files = sum(len(file_list) for file_list in cat_data['items'].values())
            interactor.display(f'\n  {cat_data["name"]} ({total_files} файлов):')
            for i, (ann, file_list) in enumerate(
                sorted(cat_data['items'].items(), key=lambda x: len(x[1]), reverse=True), 1
            ):
                interactor.display(f'    {i}. ({ann}) — {len(file_list)} файлов')

        interactor.display('\nКакие категории удалить из тегов?')
        interactor.display('  Полные названия: remaster, soundtrack, version, other')
        interactor.display('  Сокращения:      r,        s,          v,       o')
        choice = interactor.prompt('  (y - все, n - оставить все) [y]: ', default='y')

        shortcuts = {'r': 'remaster', 's': 'soundtrack', 'v': 'version', 'o': 'other'}

        to_remove: set[str] = set()
        if choice in ('y', 'yes', 'д', 'да'):
            for cat_data in categories.values():
                to_remove.update(cat_data['items'].keys())
        elif choice not in ('n', 'no', 'н', 'нет', ''):
            selected_cats = [c.strip() for c in choice.split(',')]
            for cat_input in selected_cats:
                cat_key = shortcuts.get(cat_input, cat_input)
                if cat_key in categories:
                    to_remove.update(categories[cat_key]['items'].keys())
                else:
                    interactor.display(f'  Неизвестная категория: {cat_input}')

        if to_remove:
            removed_count = 0
            for ann in to_remove:
                for f in annotations[ann]:
                    if not f.exists():
                        continue
                    tags = repo.get_tags(f)
                    if tags:
                        new_title = remove_annotation_from_text(tags.title, ann)
                        if new_title != tags.title:
                            if ctx.dry_run:
                                ctx.log_debug(f'[DRY RUN] Удалил бы из тега: ({ann}) из {f.name}')
                                repo.simulate_update_tags(f, tags.artist, new_title)
                                removed_count += 1
                            elif repo.update_tags(f, tags.artist, new_title):
                                ctx.log_debug(f'Удалено из тега: ({ann}) из {f.name}')
                                removed_count += 1
            interactor.display(f'  Удалено уточнений из тегов: {removed_count}')

        return files

    # === Подэтап 3.4: формирование имён по тегам ===

    def _rename_by_tags(
        self, files: list[Path], ctx: SessionContext, repo: TagRepository, interactor: UserInteractor
    ) -> list[Path]:
        ctx.log_debug('Подэтап 3.4: формирование имён из тегов')

        interactor.display('  Поиск файлов с недопустимыми символами...')
        invalid_files = self._collect_invalid_filename_files(files, repo)

        strategies: dict[str, tuple[str, dict]] = {}
        if invalid_files:
            interactor.display(
                f'\n  Найдено {sum(len(v) for v in invalid_files.values())} файлов '
                f'с недопустимыми символами в {len(invalid_files)} группах'
            )

            priority_order = ['bilingual_title', 'artist_name']
            other_keys = [k for k in invalid_files.keys() if k not in priority_order]
            ordered_keys = priority_order + sorted(other_keys)

            for problem_key in ordered_keys:
                if problem_key not in invalid_files:
                    continue
                examples = invalid_files[problem_key]
                strategy, params = self._choose_strategy_for_group(problem_key, examples, interactor)
                strategies[problem_key] = (strategy, params)

        safe_candidates: list[tuple[Path, str]] = []
        mismatch_candidates: list[tuple[Path, str, str]] = []
        missing: list[Path] = []

        for f in files:
            if not f.exists():
                continue

            file_title = extract_title_from_filename(f.stem)
            tags = repo.get_tags(f)
            ctx.log_debug(f'Анализ: {f.name}, теги: {tags}, извлечённое название: {file_title!r}')

            if tags:
                artist = remove_junk(str(tags.artist))
                title = remove_junk(str(tags.title))
                raw_name = f'{artist} - {title}'
                raw_name = normalize_title_spacing(raw_name)
                user_already_chose = False

                invalid_chars = set(INVALID_FILENAME_CHARS.findall(raw_name))
                if invalid_chars:
                    if '/' in invalid_chars:
                        slash_type = self._categorize_slash_usage(raw_name)
                        if slash_type == 'bilingual_title':
                            problem_key = 'bilingual_title'
                        elif slash_type == 'artist_name':
                            problem_key = 'artist_name'
                        else:
                            problem_key = 'other'
                    else:
                        chars_str = ''.join(sorted(invalid_chars))
                        problem_key = f'other:{chars_str}'

                    if problem_key in strategies:
                        strategy, params = strategies[problem_key]

                        if strategy != 'skip':
                            strategy_obj = StrategyFactory.create(strategy, params, interactor, f.name)
                            base_name = strategy_obj.apply(
                                raw_name, set(INVALID_FILENAME_CHARS.findall(raw_name))
                            )
                            if base_name:
                                user_already_chose = True
                            else:
                                ctx.log_debug(f'Пропущен (стратегия вернула None): {f.name}')
                                continue
                        else:
                            ctx.log_debug(f'Пропущен (стратегия skip): {f.name}')
                            continue
                    else:
                        ctx.log_debug(f'Пропущен (нет стратегии): {f.name}')
                        continue
                else:
                    base_name = raw_name

                new_name = f'{base_name}{f.suffix}'

                if new_name == f.name:
                    continue

                if user_already_chose or self._titles_match(file_title, title):
                    safe_candidates.append((f, new_name))
                else:
                    mismatch_candidates.append((f, file_title, new_name))
            else:
                missing.append(f)

        if safe_candidates:
            labels = [f'{f.name}  ->  {new_name}' for f, new_name in safe_candidates]
            selected_indices = select_indices(
                labels,
                f'Найдено {len(safe_candidates)} файлов для переименования по тегам (названия совпадают)',
                interactor,
            )

            renamed_count = 0
            duplicates_found: list[tuple[Path, Path]] = []

            for i in selected_indices:
                old_path, new_name = safe_candidates[i]
                new_path = old_path.with_name(new_name)

                status, result_path = safe_rename(old_path, new_path, ctx, repo)

                if status == 'renamed':
                    ctx.log_debug(f'Переименован: {old_path.name} -> {new_path.name}')
                    ctx.increment_stat('renamed_by_tags')
                    renamed_count += 1
                elif status == 'duplicate_exists':
                    duplicates_found.append((old_path, new_path))

            if renamed_count:
                interactor.display(f'  Переименовано файлов: {renamed_count}')

            if duplicates_found:
                self._handle_rename_duplicates(duplicates_found, ctx, repo, interactor)

        # Сохраняем missing для этапа 7
        ctx.missing_files = missing
        # Сохраняем mismatch_candidates для подэтапа 3.5
        self._pending_mismatches = mismatch_candidates

        return files

    # === Подэтап 3.5: обработка расхождений ===

    def _handle_mismatches(
        self, files: list[Path], ctx: SessionContext, repo: TagRepository, interactor: UserInteractor
    ) -> list[Path]:
        mismatch_candidates: list[tuple[Path, str, str]] = getattr(self, '_pending_mismatches', [])
        if not mismatch_candidates:
            return files

        # Фильтруем несуществующие и уже корректные
        active_mismatch: list[tuple[Path, str, str]] = []
        for f, file_title, old_new_name in mismatch_candidates:
            if not f.exists():
                ctx.log_debug(f'Файл {f.name} больше не существует, пропускаю')
                continue

            tags = repo.get_tags(f)
            if not tags:
                continue

            artist = remove_junk(str(tags.artist))
            title = remove_junk(str(tags.title))
            current_new_name = build_safe_filename(artist, title, f.suffix)

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

        interactor.display(
            f'\n  Найдено {len(active_mismatch)} файлов с расхождением названий (файл vs теги):'
        )

        categorized: dict[str, list[tuple[Path, str, str, str, str]]] = {
            'A': [], 'B': [], 'C': [], 'D': [], 'E': [],
        }

        for f, file_title, new_name in active_mismatch:
            tags = repo.get_tags(f)
            if not tags:
                continue
            tag_artist, tag_title = tags.artist, tags.title
            category = categorize_mismatch(f, file_title, tag_title, tag_artist)
            categorized[category].append((f, file_title, new_name, tag_artist, tag_title))

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
                    parsed = parse_artist_title(f.stem)
                    if parsed:
                        cleaned_artist, cleaned_title = clean_extracted_tags(parsed[0], parsed[1])
                        new_tag_str = f'{cleaned_artist} - {cleaned_title}'
                        interactor.display('      Файл останется без изменений')
                        interactor.display(f'      Теги: {tag_str}')
                        interactor.display(f'         → {new_tag_str}')
                    else:
                        new_tag_str = file_title or f.stem
                        interactor.display('      Файл останется без изменений')
                        interactor.display(f'      Теги: {tag_str}')
                        interactor.display(f'         → {new_tag_str}')
                elif cat_key == 'E':
                    interactor.display(f'      Файл: {file_title or "(нет)"}')
                    interactor.display(f'      Теги: {tag_str}')
                    interactor.display(f'      → {new_name}')
                else:
                    interactor.display(f'      → {new_name}')

            if cat_key != 'E':
                default = category_defaults[cat_key]
                action_name = (
                    'переименовать по тегам' if default == 't' else 'обновить теги из имён'
                )

                choice = interactor.prompt(
                    f"\nПрименить '{action_name}' ко всей группе? (y/n) [y]: ", default='y'
                )

                if choice in ('', 'y', 'yes', 'д', 'да'):
                    for f, file_title, new_name, tag_artist, tag_title in categorized[cat_key]:
                        if default == 't':
                            new_path = f.with_name(new_name)
                            status, result_path = safe_rename(f, new_path, ctx, repo)
                            handle_rename_result(status, f, result_path, ctx, 'renamed_by_tags')
                        else:
                            parsed = parse_artist_title(f.stem)
                            if parsed:
                                cleaned_artist, cleaned_title = clean_extracted_tags(
                                    parsed[0], parsed[1]
                                )
                                if ctx.dry_run:
                                    ctx.log_debug(f'[DRY RUN] Обновил бы теги из имени: {f.name}')
                                    repo.simulate_update_tags(f, cleaned_artist, cleaned_title)
                                    ctx.increment_stat('tags_written')
                                elif repo.update_tags(f, cleaned_artist, cleaned_title):
                                    ctx.log_debug(f'Теги обновлены из имени: {f.name}')
                                    ctx.increment_stat('tags_written')
                else:
                    interactor.display(f'  Группа {cat_key} пропущена.')

            else:
                interactor.display('\nДля каждого файла выберите действие:')
                interactor.display('  t - использовать данные из тега (переименовать файл)')
                interactor.display('  f - использовать данные из файла (обновить тег)')
                interactor.display('  n - пропустить')

                last_action = 'n'

                for f, file_title, old_new_name, tag_artist, tag_title in categorized[cat_key]:
                    interactor.display(f'\n  {f.name}')
                    interactor.display(f'      Файл: {file_title or "(нет)"}')
                    interactor.display(f'      Теги: {tag_artist} - {tag_title}')

                    raw_name = f'{tag_artist} - {tag_title}'
                    raw_name = normalize_title_spacing(raw_name)
                    cleaned = INVALID_FILENAME_CHARS.sub('', raw_name)
                    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
                    current_new_name = f'{cleaned}{f.suffix}'

                    if last_action == 't':
                        interactor.display(f'      → Новое имя (t): {current_new_name}')
                    elif last_action == 'f':
                        parsed = parse_artist_title(f.stem)
                        if parsed:
                            interactor.display(f'      → Новые теги (f): {parsed[0]} - {parsed[1]}')
                        else:
                            interactor.display(f'      → Новые теги (f): {file_title or f.stem}')
                    else:
                        interactor.display(f'      → Если t: {current_new_name}')
                        parsed = parse_artist_title(f.stem)
                        if parsed:
                            interactor.display(f'      → Если f: {parsed[0]} - {parsed[1]}')

                    choice = interactor.prompt(
                        f'      Действие [t/f/n] ({last_action}): ', default=last_action
                    )

                    if choice == last_action and not choice:
                        interactor.display(f'      [Использовано предыдущее действие: {choice}]')
                    else:
                        last_action = choice

                    if choice == 't':
                        new_path = f.with_name(current_new_name)
                        status, result_path = safe_rename(f, new_path, ctx, repo)
                        handle_rename_result(status, f, result_path, ctx, 'renamed_by_tags')
                    elif choice == 'f':
                        parsed = parse_artist_title(f.stem)
                        if parsed:
                            if ctx.dry_run:
                                ctx.log_debug(f'[DRY RUN] Обновил бы теги из имени: {f.name}')
                                repo.simulate_update_tags(f, parsed[0], parsed[1])
                                ctx.increment_stat('tags_written')
                            elif repo.update_tags(f, parsed[0], parsed[1]):
                                ctx.log_debug(f'Теги обновлены из имени: {f.name}')
                                ctx.increment_stat('tags_written')
                    else:
                        interactor.display(f'      Пропущен: {f.name}')

        return files

    # === Обработка дубликатов при переименовании ===

    def _handle_rename_duplicates(
        self,
        duplicates_found: list[tuple[Path, Path]],
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> None:
        interactor.display(f'\n  При переименовании обнаружено {len(duplicates_found)} дубликатов:')

        for i, (old_path, new_path) in enumerate(duplicates_found, 1):
            old_info = file_info_line(old_path)
            existing_info = file_info_line(new_path)

            old_tags = repo.get_tags(old_path)
            existing_tags = repo.get_tags(new_path)
            tags_match = (
                old_tags
                and existing_tags
                and old_tags.artist.lower().strip() == existing_tags.artist.lower().strip()
                and old_tags.title.lower().strip() == existing_tags.title.lower().strip()
            )
            tags_note = ' (теги совпадают)' if tags_match else ''

            old_score = (old_path.stat().st_size, name_quality_score(old_path))
            new_score = (new_path.stat().st_size, name_quality_score(new_path))
            best_is_old = old_score >= new_score

            best_marker = ' [лучшее]' if best_is_old else ''
            existing_marker = ' [лучшее]' if not best_is_old else ''

            interactor.display(f'\n  {i}. {new_path.name}{tags_note}')
            interactor.display(f'      1. {old_path.name} ({old_info}){best_marker}')
            interactor.display(f'      2. {new_path.name} ({existing_info}){existing_marker}')

            if best_is_old:
                interactor.display('      → Предлагается: оставить 1 (лучшее), удалить 2')
            else:
                interactor.display('      → Предлагается: оставить 2 (лучшее), удалить 1')

        choice = interactor.prompt(
            f'\n  Объединить {len(duplicates_found)} дубликатов по лучшему качеству? (y/n) [y]: ',
            default='y',
        )

        if choice in ('', 'y', 'yes', 'д', 'да'):
            merged_count = 0
            for old_path, new_path in duplicates_found:
                status, result_path = safe_rename(old_path, new_path, ctx, repo)
                handle_rename_result(status, old_path, result_path, ctx, 'renamed_by_tags')
                if status in ('renamed', 'kept_existing'):
                    merged_count += 1

            interactor.display(f'  Объединено дубликатов: {merged_count}')
        else:
            interactor.display('  Дубликаты пропущены.')

    # === Приватные вспомогательные методы (чистая логика) ===

    def _find_tag_annotations(
        self, files: list[Path], repo: TagRepository
    ) -> dict[str, list[Path]]:
        """Находит уникальные скобки в тегах названий."""
        annotations: dict[str, list[Path]] = {}
        bracket_pattern = re.compile(r'[\(\[]([^\)\]]+)[\)\]]')

        for f in files:
            if not f.exists():
                continue
            tags = repo.get_tags(f)
            if tags:
                title = tags.title
                matches = bracket_pattern.findall(title)
                for match in matches:
                    match = match.strip()
                    if not match:
                        continue
                    from config import JUNK_PATTERN
                    if JUNK_PATTERN.search(f'({match})'):
                        continue
                    if FEAT_VARIANTS_PATTERN.match(match):
                        continue
                    if match not in annotations:
                        annotations[match] = []
                    if f not in annotations[match]:
                        annotations[match].append(f)

        return annotations

    def _categorize_annotations(
        self, annotations: dict[str, list[Path]]
    ) -> dict[str, dict[str, Any]]:
        """Группирует уточнения по категориям."""
        categories: dict[str, dict[str, Any]] = {
            'remaster': {
                'name': 'Ремастеринг',
                'pattern': re.compile(r'(?:remaster|remastered|remix)', re.I),
                'items': {},
            },
            'soundtrack': {
                'name': 'Саундтреки (from/From)',
                'pattern': re.compile(r'^(?:from|From)\s', re.I),
                'items': {},
            },
            'version': {
                'name': 'Версии треков (Edit/Version/Mix)',
                'pattern': re.compile(r'(?:edit|version|mix|single|album|radio)', re.I),
                'items': {},
            },
            'other': {
                'name': 'Прочее',
                'pattern': None,
                'items': {},
            },
        }

        for ann, file_list in annotations.items():
            categorized = False
            for cat_key, cat_data in categories.items():
                if cat_key == 'other':
                    continue
                if cat_data['pattern'] and cat_data['pattern'].search(ann):
                    cat_data['items'][ann] = file_list
                    categorized = True
                    break
            if not categorized:
                categories['other']['items'][ann] = file_list

        return {k: v for k, v in categories.items() if v['items']}

    def _collect_invalid_filename_files(
        self, files: list[Path], repo: TagRepository
    ) -> dict[str, list[tuple[Path, str, str]]]:
        """Собирает файлы с недопустимыми символами и группирует их."""
        problems: dict[str, list[tuple[Path, str, str]]] = {}

        for f in files:
            if not f.exists():
                continue

            tags = repo.get_tags(f)
            if not tags:
                continue

            artist = remove_junk(str(tags.artist))
            title = remove_junk(str(tags.title))
            raw_name = f'{artist} - {title}'
            raw_name = normalize_title_spacing(raw_name)

            invalid_chars = set(INVALID_FILENAME_CHARS.findall(raw_name))
            if not invalid_chars:
                continue

            cleaned = INVALID_FILENAME_CHARS.sub('', raw_name)
            cleaned = re.sub(r'\s+', ' ', cleaned).strip()
            if f'{cleaned}{f.suffix}' == f.name:
                continue

            if '/' in invalid_chars:
                slash_type = self._categorize_slash_usage(raw_name)
                if slash_type == 'bilingual_title':
                    problem_key = 'bilingual_title'
                elif slash_type == 'artist_name':
                    problem_key = 'artist_name'
                else:
                    problem_key = 'other'
            else:
                chars_str = ''.join(sorted(invalid_chars))
                problem_key = f'other:{chars_str}'

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
        if problem_key == 'bilingual_title':
            default_strategy = 'first_title'
        elif problem_key == 'artist_name':
            default_strategy = 'remove'
        else:
            default_strategy = 'remove'

        if problem_key.startswith('other:'):
            chars_str = problem_key[6:]
        else:
            chars_str = '/'

        interactor.display('\n  Будет применено по умолчанию: ', )

        if problem_key == 'bilingual_title':
            interactor.display('оставить первое название')
        elif problem_key == 'artist_name':
            interactor.display("удалить '/'")
        else:
            interactor.display(f"удалить '{chars_str}'")

        interactor.display('')
        for i, (f, current_name, proposed_name) in enumerate(examples, 1):
            strategy_obj = StrategyFactory.create(default_strategy, {}, interactor, f.name)
            preview_result = strategy_obj.apply(
                proposed_name, set(INVALID_FILENAME_CHARS.findall(proposed_name))
            )
            interactor.display(f'  {i}. {current_name}')
            interactor.display(f'     {proposed_name}')
            if preview_result:
                interactor.display(f'     → {preview_result}')
            else:
                interactor.display(f'     → {proposed_name}')

        interactor.display(f'\n  Как обработать все {len(examples)} файлов?')

        if problem_key == 'bilingual_title':
            interactor.display("  1. Оставить первое название (до '/') [по умолчанию]")
            interactor.display("  2. Оставить второе название (после '/')")
            interactor.display('  3. Ввести имя вручную для каждого файла')
            interactor.display('  4. Пропустить все файлы')

            choice = interactor.prompt('  Выберите вариант [1]: ', default='1')

            try:
                idx = int(choice)
            except ValueError:
                idx = 1

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

            choice = interactor.prompt('  Выберите вариант [1]: ', default='1')

            try:
                idx = int(choice)
            except ValueError:
                idx = 1

            if idx == 1:
                return ('remove', {})
            if idx == 2:
                return ('manual', {})
            return ('skip', {})

        interactor.display(f"  1. Удалить символы '{chars_str}' [по умолчанию]")
        interactor.display(f"  2. Заменить '{chars_str}' на '-'")
        interactor.display('  3. Ввести имя вручную для каждого файла')
        interactor.display('  4. Пропустить все файлы')

        choice = interactor.prompt('  Выберите вариант [1]: ', default='1')

        try:
            idx = int(choice)
        except ValueError:
            idx = 1

        if idx == 1:
            return ('remove', {})
        if idx == 2:
            return ('replace', {'char': '-'})
        if idx == 3:
            return ('manual', {})
        return ('skip', {})

    def _categorize_slash_usage(self, raw_name: str) -> str:
        """Определяет тип использования '/' в имени."""
        match = re.match(r'^(.+?)\s+-\s+(.+)$', raw_name)
        if match:
            artist_part = match.group(1)
            title_part = match.group(2)

            if '/' in artist_part:
                return 'artist_name'
            if '/' in title_part:
                return 'bilingual_title'

        if '/' in raw_name:
            return 'artist_name'

        return 'other'

    def _titles_match(self, file_title: str, tag_title: str) -> bool:
        """Проверяет, совпадают ли названия (с допуском на различия)."""
        f = file_title.lower().strip()
        t = tag_title.lower().strip()
        if f == t:
            return True
        if f in t or t in f:
            return True
        return False


class TagWritingStage:
    """Этап 4: Запись тегов из имени файла для файлов без тегов."""

    @property
    def name(self) -> str:
        return "Запись тегов из имени файла"

    def execute(
        self,
        files: list[Path],
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> list[Path]:
        tags_to_write: list[tuple[Path, str, str]] = []

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

        if not tags_to_write:
            return files

        # Обработка уточнений в скобках
        tags_to_write = self._handle_annotations(tags_to_write, ctx, interactor)

        labels: list[str] = []
        for f, artist, title in tags_to_write:
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
            interactor.display(f'  Записано тегов: {written_count}')
        if renamed_count:
            interactor.display(f'  Переименовано файлов: {renamed_count}')

        return files

    def _handle_annotations(
        self,
        tags_to_write: list[tuple[Path, str, str]],
        ctx: SessionContext,
        interactor: UserInteractor,
    ) -> list[tuple[Path, str, str]]:
        """Обрабатывает уточнения в скобках перед записью тегов."""
        bracket_pattern = re.compile(r'[\(\[]([^\)\]]+)[\)\]]')
        files_with_annotations: list[tuple[int, Path, str, str, list[str]]] = []

        for i, (f, artist, title) in enumerate(tags_to_write):
            matches = bracket_pattern.findall(title)
            real_matches = [m for m in matches if not FEAT_VARIANTS_PATTERN.match(m)]
            if real_matches:
                files_with_annotations.append((i, f, artist, title, real_matches))

        if not files_with_annotations:
            return tags_to_write

        interactor.display(
            f'\n  Внимание: {len(files_with_annotations)} файлов содержат уточнения в скобках:'
        )
        for i, f, artist, title, matches in files_with_annotations:
            interactor.display(f'    {f.name}')
            interactor.display(f'      Уточнения: {", ".join(f"({m})" for m in matches)}')

        choice = interactor.prompt('\n  Удалить уточнения из тегов и имён? (y/n) [y]: ', default='y')

        if choice in ('', 'y', 'yes', 'д', 'да'):
            for i, f, artist, title, matches in files_with_annotations:
                new_title = title
                for ann in matches:
                    new_title = remove_annotation_from_text(new_title, ann)
                tags_to_write[i] = (f, artist, new_title)
            interactor.display('  Уточнения будут удалены перед записью.')
        else:
            interactor.display('  Уточнения будут сохранены.')

        return tags_to_write


class DuplicateResolutionStage:
    """Этап 5: Поиск и объединение дубликатов."""

    @property
    def name(self) -> str:
        return "Поиск и объединение дубликатов"

    def execute(
        self,
        files: list[Path],
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> list[Path]:
        return process_duplicates(files, ctx, repo, interactor)


class ArtistNormalizationStage:
    """Этап 6: Нормализация имён исполнителей."""

    @property
    def name(self) -> str:
        return "Нормализация имён исполнителей"

    def execute(
        self,
        files: list[Path],
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> list[Path]:
        if ctx.target_dir is not None:
            files = [
                f for f in ctx.target_dir.iterdir()
                if f.suffix.lower() in SUPPORTED_EXTENSIONS
            ]
            ctx.log_debug(f'Пересканировано файлов перед этапом 6: {len(files)}')

        artist_variations = find_artist_variations(files, repo)
        unique_normalized: set[str] = set()

        if not artist_variations:
            return files

        interactor.display(
            f'\nНайдено {len(artist_variations)} исполнителей с разными вариантами написания.'
        )

        for key, entries in artist_variations.items():
            artist_counts: dict[str, int] = {}
            for _, _, individual_artist in entries:
                artist_counts[individual_artist] = artist_counts.get(individual_artist, 0) + 1

            sorted_artists = sorted(artist_counts.items(), key=lambda x: x[1], reverse=True)

            interactor.display(f'\nИсполнитель: {key}')
            interactor.display('Варианты написания:')
            for i, (artist, count) in enumerate(sorted_artists, 1):
                interactor.display(f'  {i}. {artist} ({count} файлов)')

            choice = interactor.prompt('Выберите номер варианта (n - пропустить) [1]: ')
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
                    interactor.display('Некорректный ввод, выбираю вариант 1.')
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
                            interactor.display(
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
        return files


class ManualInputStage:
    """Этап 7: Ручной ввод для файлов без метаданных."""

    @property
    def name(self) -> str:
        return "Ручной ввод для файлов без метаданных"

    def execute(
        self,
        files: list[Path],
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> list[Path]:
        if ctx.target_dir is not None:
            files = [
                f for f in ctx.target_dir.iterdir()
                if f.suffix.lower() in SUPPORTED_EXTENSIONS
            ]
            ctx.log_debug(f'Пересканировано файлов перед этапом 7: {len(files)}')

        still_missing: list[Path] = []

        actually_missing = [
            f for f in ctx.missing_files if f.exists() and repo.get_tags(f) is None
        ]
        ctx.log_debug(
            f'Файлов без тегов после Этапа 6: {len(actually_missing)} '
            f'(было {len(ctx.missing_files)})'
        )

        if not actually_missing:
            ctx._still_missing = still_missing  # type: ignore[attr-defined]
            return files

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
            interactor.display(f'\n  Файл: {f.name}')
            interactor.display(f'  Предполагаемое название: {title}')
            artist = interactor.prompt('  Введите исполнителя (Enter - пропустить): ').strip()
            if not artist:
                still_missing.append(f)
                continue

            raw_name = f'{artist} - {title}'

            if INVALID_FILENAME_CHARS.search(raw_name):
                base_name = clean_filename_safely(raw_name, f.name, ctx, interactor)
                if base_name == 'SKIP':
                    interactor.display(f'  Оставлен без изменений: {f.name}')
                    continue
            else:
                base_name = raw_name

            new_name = f'{base_name}{f.suffix}'
            new_path = f.with_name(new_name)
            status, result_path = safe_rename(f, new_path, ctx, repo)
            handle_rename_result(status, f, result_path, ctx, 'manual_input')

        ctx._still_missing = still_missing  # type: ignore[attr-defined]
        return files
