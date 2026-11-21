"""Типизированные результаты файловых операций."""

from collections.abc import Callable
from pathlib import Path

from audio_verification import find_ffprobe
from context import SessionContext
from file_state import FileRecord, OperationResult, OperationStatus
from interactor import UserInteractor
from pipeline_utils import modification_allowed, safe_rename
from report_events import log_operation
from tag_publication import PublicationError, publish_tags
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

    def write_tags(
        self,
        path: Path,
        artist: str,
        title: str,
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> OperationResult:
        record = ctx.registry.track(path)
        current = ctx.present_path(record.id)
        if current is None:
            reason = 'файл отсутствует'
            log_operation(
                'tags',
                record.path,
                'отказ',
                reason,
                artist=artist,
                title=title,
                proposed=ctx.dry_run,
            )
            return OperationResult(OperationStatus.ERROR, None, reason)

        if not modification_allowed(current, ctx, interactor):
            reason = ctx.get_block_reason(current) or '??????????? ?? ?????????'
            log_operation(
                'tags',
                current,
                'блокировка',
                reason,
                artist=artist,
                title=title,
                proposed=ctx.dry_run,
            )
            return OperationResult(OperationStatus.ERROR, current, reason)

        def publish(
            artist: str | None, title: str | None
        ) -> tuple[OperationStatus, str]:
            return self._publish_tag_change(
                current, record, artist, title, ctx, repo, interactor
            )

        result = repo.apply_tags(
            current, artist, title, simulated=ctx.dry_run, publish=publish
        )
        if result.is_model_change:
            ctx.registry.mark_content_modeled(record.id)
        if (
            result.is_confirmed_change or result.is_model_change
        ) and current in ctx.missing_files:
            ctx.missing_files.remove(current)
        if result.status in (OperationStatus.ERROR, OperationStatus.UNCERTAIN):
            ctx.block_path(current, result.reason)
            if result.reason != 'ffprobe недоступен':
                interactor.display_error(f'{current.name}: {result.reason}')
        return result

    @staticmethod
    def _publish_tag_change(
        current: Path,
        record: FileRecord,
        artist: str | None,
        title: str | None,
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> tuple[OperationStatus, str]:
        try:
            command = find_ffprobe()
        except OSError as exc:
            if not ctx.ffprobe_missing_reported:
                interactor.display_error(str(exc))
                ctx.ffprobe_missing_reported = True
            return OperationStatus.ERROR, 'ffprobe недоступен'
        if ctx.recovery is None or record.identity is None:
            return (
                OperationStatus.ERROR,
                'нет блокировки восстановления или идентичности файла',
            )
        try:
            identity = publish_tags(
                ctx.recovery,
                current.absolute(),
                record.identity,
                repo,
                command,
                artist,
                title,
            )
        except PublicationError as exc:
            if exc.confirmed is None:
                return OperationStatus.UNCERTAIN, str(exc)
            ctx.registry.confirm_replacement(record.id, exc.confirmed)
            if ctx.report is not None:
                ctx.report.record_tag_publication(current, exc.confirmed)
            ctx.block_path(current, str(exc))
            interactor.display_error(
                f'{current.name}: теги записаны, очистка не завершена: {exc}'
            )
            return (
                OperationStatus.SUCCESS,
                f'теги записаны, очистка не завершена: {exc}',
            )
        except OSError as exc:
            return OperationStatus.ERROR, str(exc)
        ctx.registry.confirm_replacement(record.id, identity)
        if ctx.report is not None:
            ctx.report.record_tag_publication(current, identity)
        return OperationStatus.SUCCESS, 'запись выбранных artist/title подтверждена'

    def rename(
        self,
        old_path: Path,
        new_path: Path,
        ctx: SessionContext,
        repo: TagRepository,
        interactor: UserInteractor,
    ) -> OperationResult:
        registry = ctx.registry
        record = registry.track(old_path.absolute())
        if not record.present:
            return OperationResult(OperationStatus.ERROR, None, 'файл отсутствует')
        if str(record.path.absolute()) != str(old_path.absolute()):
            old_path = record.path
        target = new_path.absolute()
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
            missing_ids = ctx.file_ids(ctx.missing_files)
            registry.update_path(
                record.id,
                target,
                simulated=operation_status is OperationStatus.SIMULATED,
            )
            ctx.missing_files = [
                entry.path
                for file_id in missing_ids
                if (entry := registry.record_of(file_id)).present
            ]
        elif operation_status in (OperationStatus.ERROR, OperationStatus.UNCERTAIN):
            path = None

        reason = ctx.get_block_reason(old_path) or default_reason
        if operation_status is OperationStatus.UNCERTAIN:
            for candidate in (old_path, new_path):
                if not ctx.is_path_blocked(candidate):
                    ctx.block_path(candidate, reason)
        return OperationResult(operation_status, path, reason)
