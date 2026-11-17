"""Наблюдаемые операции: данные отчёта без управления конвейером."""

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger('clean_music')


@dataclass(frozen=True)
class Operation:
    kind: str
    path: str
    result: str
    reason: str
    proposed: bool = False
    target: str | None = None
    artist: str | None = None
    title: str | None = None
    identity: tuple[int, int] | None = None
    stage: str = ''
    originals: tuple[str, ...] = ()


def log_operation(
    kind: str,
    path: Path,
    result: str,
    reason: str,
    *,
    target: Path | None = None,
    artist: str | None = None,
    title: str | None = None,
    proposed: bool = False,
) -> None:
    """Записывает факт/предложение, не меняя пути, теги и решения обработки."""
    identity = None
    observed = target if kind == 'rename' and result == 'успех' and target else path
    try:
        info = observed.lstat()
        identity = (info.st_dev, info.st_ino)
    except OSError:
        pass
    operation = Operation(
        kind=kind,
        path=str(path.absolute()),
        result=result,
        reason=reason,
        proposed=proposed or result == 'ПРЕДЛОЖЕНИЕ',
        target=str(target.absolute()) if target is not None else None,
        artist=artist,
        title=title,
        identity=identity,
    )
    prefix = '[ПРЕДЛОЖЕНИЕ] ' if operation.proposed else ''
    logger.info(
        '%sОПЕРАЦИЯ %s',
        prefix,
        json.dumps(asdict(operation), ensure_ascii=False),
        extra={'operation': operation},
    )
