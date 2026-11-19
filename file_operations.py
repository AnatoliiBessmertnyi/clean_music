"""Типизированные результаты файловых операций."""

from collections.abc import Callable
from pathlib import Path
from weakref import WeakKeyDictionary

from context import SessionContext
from file_state import FileRegistry, OperationResult, OperationStatus
from interactor import UserInteractor
from pipeline_utils import safe_rename, simulate_rename
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
        self._simulated_paths: WeakKeyDictionary[FileRegistry, dict[int, Path]] = (
            WeakKeyDictionary()
        )

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
        simulated = self._simulated_paths.get(registry, {})
        is_model_source = (
            ctx.dry_run
            and simulated.get(record.id) == record.path
            and record.path == old_path.absolute()
        )

        if is_model_source:
            reason = ctx.get_block_reason(old_path)
            if reason is None and ctx.is_path_blocked(new_path):
                reason = 'целевой путь заблокирован'
            if reason is None and old_path.absolute().parent != target.parent:
                reason = 'переименование между каталогами не поддерживается'
            if reason is not None:
                ctx.block_path(old_path, reason)
                interactor.display_error(f'{old_path.name}: {reason}')
                log_operation(
                    'rename', old_path, 'отказ', reason, target=new_path, proposed=True
                )
                return OperationResult(OperationStatus.ERROR, None, reason)

        if ctx.dry_run:
            owner = registry.id_for(target)
            occupied = owner is not None and owner != record.id
            if is_model_source:
                if str(old_path.absolute()) == str(target):
                    return OperationResult(
                        OperationStatus.NOOP, old_path, 'имя файла не изменилось'
                    )
                try:
                    target.lstat()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    reason = str(exc)
                    ctx.block_path(old_path, reason)
                    ctx.block_path(new_path, reason)
                    interactor.display_error(f'{old_path.name}: {reason}')
                    log_operation(
                        'rename',
                        old_path,
                        'отказ',
                        reason,
                        target=new_path,
                        proposed=True,
                    )
                    return OperationResult(OperationStatus.UNCERTAIN, None, reason)
                else:
                    occupied = True
            if occupied:
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

        path: Path | None
        if is_model_source:
            simulate_rename(old_path, new_path, repo, interactor)
            status, path = 'planned', new_path
        else:
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
            if operation_status is OperationStatus.SIMULATED:
                self._simulated_paths.setdefault(registry, {})[record.id] = target
            else:
                simulated.pop(record.id, None)
        elif operation_status in (OperationStatus.ERROR, OperationStatus.UNCERTAIN):
            path = None

        reason = ctx.get_block_reason(old_path) or default_reason
        return OperationResult(operation_status, path, reason)
