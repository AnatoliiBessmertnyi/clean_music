"""Конкретные этапы (Stages) конвейера обработки аудиофайлов."""

import re
from pathlib import Path

from config import JUNK_PATTERN
from context import SessionContext
from interactor import UserInteractor
from naming import normalize_dash, normalize_filename_stem, remove_junk
from pipeline import ProcessingStage
from pipeline_utils import process_rename_batch, select_indices
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
