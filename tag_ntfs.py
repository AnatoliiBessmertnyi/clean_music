"""Операции с защитой и идентичностью обычных файлов Windows/NTFS."""

import ctypes as c
import os
import stat
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from ctypes import wintypes as w
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from file_state import FileIdentity, regular_file_info

_ALLOWED = (
    stat.FILE_ATTRIBUTE_HIDDEN
    | stat.FILE_ATTRIBUTE_SYSTEM
    | stat.FILE_ATTRIBUTE_ARCHIVE
)
_SECURITY = 7  # OWNER_SECURITY_INFORMATION | GROUP_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
_INVALID_HANDLE = c.c_void_p(-1).value


def _api(dll: str, name: str, result: Any, *args: Any) -> Any:
    if sys.platform != 'win32':
        raise OSError('требуется локальный NTFS в Windows')
    fn = getattr(c.WinDLL(dll, use_last_error=True), name)
    fn.argtypes = args
    fn.restype = result
    return fn


def _kernel(name: str, result: Any, *args: Any) -> Any:
    return _api('kernel32', name, result, *args)


def _last_error() -> int:
    if sys.platform != 'win32':
        raise OSError('требуется локальный NTFS в Windows')
    return c.get_last_error()


def _win_error(code: int | None = None) -> OSError:
    if sys.platform != 'win32':
        return OSError('требуется локальный NTFS в Windows')
    return c.WinError(_last_error() if code is None else code)


def _security(handle: int) -> str:
    descriptor = c.c_void_p()
    text = c.c_void_p()
    get = _api(
        'advapi32',
        'GetSecurityInfo',
        w.DWORD,
        w.HANDLE,
        w.DWORD,
        w.DWORD,
        c.c_void_p,
        c.c_void_p,
        c.c_void_p,
        c.c_void_p,
        c.c_void_p,
    )
    convert = _api(
        'advapi32',
        'ConvertSecurityDescriptorToStringSecurityDescriptorW',
        w.BOOL,
        c.c_void_p,
        w.DWORD,
        w.DWORD,
        c.c_void_p,
        c.c_void_p,
    )
    free = _kernel('LocalFree', c.c_void_p, c.c_void_p)
    code = get(handle, 1, _SECURITY, None, None, None, None, c.byref(descriptor))
    if code:
        raise _win_error(code)
    try:
        if not convert(descriptor, 1, _SECURITY, c.byref(text), None):
            raise _win_error()
        return c.wstring_at(text)
    finally:
        free(text)
        free(descriptor)


def _open(
    path: Path, access: int, share: int, disposition: int = 3, security: object = None
) -> int:
    create = _kernel(
        'CreateFileW',
        w.HANDLE,
        w.LPCWSTR,
        w.DWORD,
        w.DWORD,
        c.c_void_p,
        w.DWORD,
        w.DWORD,
        w.HANDLE,
    )
    # FILE_FLAG_OPEN_REPARSE_POINT
    handle = create(str(path), access, share, security, disposition, 0x00200000, None)
    if handle is None or handle == _INVALID_HANDLE:
        raise _win_error()
    return int(handle)


def _close(handle: int) -> None:
    _kernel('CloseHandle', w.BOOL, w.HANDLE)(handle)


def _no_ads(handle: int) -> None:
    get = _kernel(
        'GetFileInformationByHandleEx', w.BOOL, w.HANDLE, c.c_int, c.c_void_p, w.DWORD
    )
    length = 1024
    while length <= 1024 * 1024:
        buffer = c.create_string_buffer(length)
        if get(handle, 7, buffer, length):  # FileStreamInfo
            offset = int.from_bytes(buffer.raw[:4], 'little')
            name_length = int.from_bytes(buffer.raw[4:8], 'little')
            if offset or buffer.raw[24 : 24 + name_length] != '::$DATA'.encode(
                'utf-16-le'
            ):
                raise OSError('обнаружен именованный поток NTFS')
            return
        error = _last_error()
        if error not in (122, 234):
            raise _win_error(error)
        length *= 2
    raise OSError('не удалось перечислить потоки NTFS')


@dataclass(frozen=True)
class Attributes:
    security: str
    flags: int
    created: int
    modified: int


def _attributes(
    handle: int, info: os.stat_result, *, empty: bool = False
) -> Attributes:
    if sys.platform != 'win32':
        raise OSError('требуется локальный NTFS в Windows')
    flags = info.st_file_attributes
    if flags & ~(_ALLOWED | stat.FILE_ATTRIBUTE_NORMAL):
        raise OSError('неподдерживаемые атрибуты NTFS')
    # Пустой файл может не иметь выделенного основного потока.
    if not empty:
        _no_ads(handle)
    return Attributes(
        _security(handle), flags & _ALLOWED, info.st_birthtime_ns, info.st_mtime_ns
    )


@contextmanager
def guarded_file(
    path: Path, *, delete: bool = False, write: bool = False
) -> Iterator[tuple[int, BinaryIO]]:
    """Запрещает конкурирующее открытие для изменения или удаления."""
    if sys.platform != 'win32':
        raise OSError('требуется локальный NTFS в Windows')
    import msvcrt

    expected = regular_file_info(path)
    access = 0x80000000 | (0x10000 if delete else 0)  # GENERIC_READ | DELETE
    access |= 0x40000000 if write else 0
    handle = _open(path, access, 1)
    try:
        fd = msvcrt.open_osfhandle(
            handle, (os.O_RDWR if write else os.O_RDONLY) | os.O_BINARY
        )
    except OSError:
        _close(handle)
        raise
    with os.fdopen(fd, 'r+b' if write else 'rb') as stream:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ) or info.st_nlink != 1:
            raise OSError('файл подменён перед открытием')
        yield handle, stream


def read_attributes(handle: int, stream: BinaryIO) -> Attributes:
    return _attributes(handle, os.fstat(stream.fileno()))


class _SecurityAttributes(c.Structure):
    _fields_ = [('length', w.DWORD), ('descriptor', c.c_void_p), ('inherit', w.BOOL)]


def copy_protected(
    source: BinaryIO,
    path: Path,
    attributes: Attributes,
    register: Callable[[FileIdentity], None],
) -> None:
    """Создаёт пустой объект с проверенными правами, затем переносит содержимое."""
    if sys.platform != 'win32':
        raise OSError('требуется локальный NTFS в Windows')
    import msvcrt
    import shutil

    descriptor = c.c_void_p()
    convert = _api(
        'advapi32',
        'ConvertStringSecurityDescriptorToSecurityDescriptorW',
        w.BOOL,
        w.LPCWSTR,
        w.DWORD,
        c.c_void_p,
        c.c_void_p,
    )
    if not convert(attributes.security, 1, c.byref(descriptor), None):
        raise _win_error()
    try:
        sa = _SecurityAttributes(c.sizeof(_SecurityAttributes), descriptor, False)
        handle = _open(path, 0xC0000000, 0, 1, c.byref(sa))
    finally:
        _kernel('LocalFree', c.c_void_p, c.c_void_p)(descriptor)
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
    except OSError:
        _close(handle)
        raise
    with os.fdopen(fd, 'wb') as work:
        info = os.fstat(fd)
        register((info.st_dev, info.st_ino))
        if _attributes(handle, info, empty=True).security != attributes.security:
            raise OSError('права пустой рабочей копии отличаются от оригинала')
        source.seek(0)
        shutil.copyfileobj(source, work, length=1024 * 1024)
        work.flush()
        os.fsync(fd)


class _BasicInfo(c.Structure):
    _fields_ = [
        ('created', c.c_int64),
        ('accessed', c.c_int64),
        ('modified', c.c_int64),
        ('changed', c.c_int64),
        ('flags', w.DWORD),
    ]


def restore_attributes(handle: int, expected: Attributes) -> None:
    ticks = expected.created // 100 + 116444736000000000
    _set_info(
        handle,
        0,
        _BasicInfo(ticks, 0, 0, 0, expected.flags or stat.FILE_ATTRIBUTE_NORMAL),
    )


def move_open_file(handle: int, target: Path) -> None:
    name = str(target.absolute()).encode('utf-16-le')

    class RenameInfo(c.Structure):
        _fields_ = [
            ('flags', w.DWORD),
            ('root', w.HANDLE),
            ('length', w.DWORD),
            ('name', c.c_char * (len(name) + 2)),
        ]

    info = RenameInfo(0, None, len(name))
    c.memmove(c.addressof(info) + RenameInfo.name.offset, name, len(name))
    _set_info(handle, 3, info)


def delete_open_file(handle: int) -> None:
    _set_info(handle, 4, w.BOOL(True))


def _set_info(handle: int, kind: int, info: c.Structure | w.BOOL) -> None:
    set_info = _kernel(
        'SetFileInformationByHandle', w.BOOL, w.HANDLE, c.c_int, c.c_void_p, w.DWORD
    )
    if not set_info(handle, kind, c.byref(info), c.sizeof(info)):
        raise _win_error()
