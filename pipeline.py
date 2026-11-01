from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol, Sequence

# Импорты из уже созданных модулей вашего проекта
from context import SessionContext
from interactor import UserInteractor
from tag_repository import TagRepository

logger = logging.getLogger(__name__)


class ProcessingStage(Protocol):
    """
    Протокол (контракт) для этапа обработки аудиофайлов.
    Использует структурную типизацию для гибкости и строгой проверки mypy.
    """

    @property
    def name(self) -> str:
        """Человекочитаемое название этапа для логирования и UI."""
        ...

    def execute(
        self,
        files: list[Path],
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> list[Path]:
        """
        Выполняет бизнес-логику конкретного этапа.
        
        :param files: Текущий список файлов для обработки.
        :param ctx: Контекст сессии (содержит dry_run, пути и глобальные настройки).
        :param repo: Кэширующий репозиторий тегов.
        :param interactor: Интерфейс для запросов к пользователю (DI).
        :return: Список файлов после обработки (может быть отфильтрован или переименован).
        """
        ...


class AudioProcessingPipeline:
    """
    Оркестратор конвейера обработки.
    Принимает набор этапов и последовательно выполняет их, 
    передавая результат (список файлов) от одного этапа к другому.
    """

    def __init__(self, stages: Sequence[ProcessingStage]) -> None:
        # Сохраняем как list, чтобы гарантировать поддержку len() и итерирования
        self._stages: list[ProcessingStage] = list(stages)

    def run(
        self,
        initial_files: list[Path],
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> list[Path]:
        """Запускает конвейер обработки."""
        current_files = initial_files
        total_stages = len(self._stages)

        for index, stage in enumerate(self._stages, start=1):
            # Красивый вывод, идентичный старой функции stage()
            interactor.display(f'\n{"=" * 60}')
            interactor.display(f'[Этап {index}/{total_stages}] {stage.name}')
            interactor.display(f'{"=" * 60}')
            ctx.log_debug(f'Начинаю этап {index}/{total_stages}: {stage.name}')
            
            # Выполняем этап
            current_files = stage.execute(current_files, ctx, repo, interactor)
            
            interactor.display(f'\n✅ Этап "{stage.name}" успешно завершен.')

        return current_files
