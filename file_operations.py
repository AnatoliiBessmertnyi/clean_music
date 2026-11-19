"""Типизированные результаты файловых операций."""

from collections.abc import Callable
from pathlib import Path

from context import SessionContext
from file_state import OperationResult, OperationStatus
from interactor import UserInteractor
from pipeline_utils import safe_rename
from report_events import log_operation
from tag_repository import TagRepository

type RenameFn = Callable[
    [Path, Path, SessionContext, TagRepository, UserInteractor], tuple[str, Path | None]
]

_CONFLICT_REASON = 'конфликт целевого имени; оба файла сохранены'
_RENAME_RESULTS = {
    'renamed': (OperationStatus.SUCCESS, 'файл переименован'),
    'unchanged': (OperationStatus.NOOP, 'имя файла не изменилось'),
    'planned': (OperationStatus.SIMULATED, 'переименование запланировано'),
    'duplicate_exists': (OperationStatus.CONFLICT, _CONFLICT_REASON),
    'skipped': (OperationStatus.ERROR, 'переименование пропущено'),
    'failed': (OperationStatus.UNCERTAIN, 'результат переименования не подтверждён'),
}


class FileExecutor:
    def __init__(self, rename_fn: RenameFn = safe_rename) -> None:
        self._rename_fn = rename_fn

    def rename(
        self,
        old_path: Path,
        new_path: Path,
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> OperationResult:
        registry = ctx.registry
        record = registry.register(old_path.absolute())
        target = new_path.absolute()

        if ctx.dry_run:
            owner = registry.id_for(target)
            if owner is not None and owner != record.id:
                ctx.block_path(old_path, _CONFLICT_REASON)
                interactor.display(
                    f'  Конфликт имени: {new_path.name}; оба файла сохранены.'
                )
                log_operation(
                    'rename',
                    old_path,
                    'конфликт',
                    _CONFLICT_REASON,
                    target=new_path,
                    proposed=True,
                )
                reason = ctx.get_block_reason(old_path) or _CONFLICT_REASON
                return OperationResult(OperationStatus.CONFLICT, new_path, reason)

        status, path = self._rename_fn(old_path, new_path, ctx, repo, interactor)
        try:
            operation_status, default_reason = _RENAME_RESULTS[status]
        except KeyError:
            raise ValueError(f'Неизвестный статус переименования: {status!r}') from None

        if operation_status is OperationStatus.SUCCESS:
            owner = registry.id_for(target)
            if owner is not None and owner != record.id:
                registry.mark_missing(owner)

        if operation_status in (OperationStatus.SUCCESS, OperationStatus.SIMULATED):
            registry.update_path(record.id, target)
        elif operation_status in (OperationStatus.ERROR, OperationStatus.UNCERTAIN):
            path = None

        reason = ctx.get_block_reason(old_path) or default_reason
        return OperationResult(operation_status, path, reason)
