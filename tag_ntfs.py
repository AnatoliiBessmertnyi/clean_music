"""Операции с защитой и идентичностью обычных файлов Windows/NTFS."""

import ctypes as c
import os
import stat
import sys
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from ctypes import wintypes as w
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from file_state import FileIdentity, check_directory, regular_file_info

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
    path: Path,
    access: int,
    share: int,
    disposition: int = 3,
    security: object = None,
    *,
    directory: bool = False,
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
    flags = 0x00200000 | (0x02000000 if directory else 0)
    handle = create(str(path), access, share, security, disposition, flags, None)
    if handle is None or handle == _INVALID_HANDLE:
        raise _win_error()
    return int(handle)


def _close(handle: int) -> None:
    _kernel('CloseHandle', w.BOOL, w.HANDLE)(handle)


def require_no_ads(handle: int) -> None:
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
        require_no_ads(handle)
    return Attributes(
        _security(handle), flags & _ALLOWED, info.st_birthtime_ns, info.st_mtime_ns
    )


@contextmanager
def guarded_file(
    path: Path, *, delete: bool = False, write: bool = False, exclusive: bool = False
) -> Iterator[tuple[int, BinaryIO]]:
    """Запрещает конкурирующее открытие для изменения или удаления."""
    if sys.platform != 'win32':
        raise OSError('требуется локальный NTFS в Windows')
    import msvcrt

    expected = regular_file_info(path)
    access = 0x80000000 | (0x10000 if delete else 0)  # GENERIC_READ | DELETE
    access |= 0x40000000 if write else 0
    handle = _open(path, access, 0 if exclusive else 1)
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


def _query(handle: int, kind: int, size: int) -> bytes:
    get = _kernel(
        'GetFileInformationByHandleEx', w.BOOL, w.HANDLE, c.c_int, c.c_void_p, w.DWORD
    )
    buffer = c.create_string_buffer(size)
    if not get(handle, kind, buffer, size):
        raise _win_error()
    return buffer.raw


def require_path_binding(path: Path, handle: int, *, directory: bool = False) -> None:
    """Сверяет имя через открытие на чтение атрибутов, совместимое с DELETE."""
    probe = _open(path, 0x80, 7, directory=directory)
    try:
        flags = int.from_bytes(_query(probe, 9, 8)[:4], 'little')
        if (
            flags & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or bool(flags & stat.FILE_ATTRIBUTE_DIRECTORY) != directory
            or _query(probe, 18, 24) != _query(handle, 18, 24)
        ):
            raise OSError(f'имя больше не указывает на проверенный объект: {path}')
    finally:
        _close(probe)


def require_single_live_link(handle: int) -> None:
    # FILE_STANDARD_INFO: NumberOfLinks, DeletePending, Directory.
    info = _query(handle, 1, 24)
    if int.from_bytes(info[16:20], 'little') != 1 or info[20] or info[21]:
        raise OSError('требуется обычный файл с одной ссылкой без ожидающего удаления')
    flags = int.from_bytes(_query(handle, 9, 8)[:4], 'little')
    if flags & (stat.FILE_ATTRIBUTE_REPARSE_POINT | stat.FILE_ATTRIBUTE_READONLY):
        raise OSError('найдена точка повторной обработки или атрибут «только чтение»')


@contextmanager
def guarded_directory(path: Path) -> Iterator[int]:
    """Удерживает проверенную цепочку каталогов на время удаления."""
    path = path.absolute()
    check_directory(path)
    with ExitStack() as stack:
        for parent in (*reversed(path.parents), path):
            # FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES, без FILE_SHARE_DELETE.
            handle = _open(parent, 0x81, 3, directory=True)
            stack.callback(_close, handle)
            require_path_binding(parent, handle, directory=True)
        yield handle


def read_attributes(handle: int, stream: BinaryIO) -> Attributes:
    return _attributes(handle, os.fstat(stream.fileno()))


class _SecurityAttributes(c.Structure):
    _fields_ = [('length', w.DWORD), ('descriptor', c.c_void_p), ('inherit', w.BOOL)]


def _restore_inherited_dacl(handle: int, descriptor: c.c_void_p) -> None:
    present, defaulted = w.BOOL(), w.BOOL()
    acl = c.c_void_p()
    get_acl = _api(
        'advapi32',
        'GetSecurityDescriptorDacl',
        w.BOOL,
        c.c_void_p,
        c.POINTER(w.BOOL),
        c.POINTER(c.c_void_p),
        c.POINTER(w.BOOL),
    )
    if not get_acl(descriptor, c.byref(present), c.byref(acl), c.byref(defaulted)):
        raise _win_error()
    if not present or not acl:
        raise OSError('cannot restore absent or NULL DACL')
    set_info = _api(
        'advapi32',
        'SetSecurityInfo',
        w.DWORD,
        w.HANDLE,
        w.DWORD,
        w.DWORD,
        c.c_void_p,
        c.c_void_p,
        c.c_void_p,
        c.c_void_p,
    )
    # SE_FILE_OBJECT, DACL_SECURITY_INFORMATION | UNPROTECTED_DACL_SECURITY_INFORMATION.
    error = set_info(handle, 1, 0x20000004, None, None, acl, None)
    if error:
        raise _win_error(error)


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
    with ExitStack() as stack:
        stack.callback(_kernel('LocalFree', c.c_void_p, c.c_void_p), descriptor)
        sa = _SecurityAttributes(c.sizeof(_SecurityAttributes), descriptor, False)
        expected = attributes.security
        inherited = 'D:AI(' in expected
        access = 0xC0000000 | (0x40000 if inherited else 0)  # WRITE_DAC
        handle = _open(path, access, 0, 1, c.byref(sa))
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
        except OSError:
            _close(handle)
            raise
        work = stack.enter_context(os.fdopen(fd, 'wb'))
        info = os.fstat(fd)
        register((info.st_dev, info.st_ino))
        actual = _attributes(handle, info, empty=True).security
        if inherited and actual == expected.replace('D:AI(', 'D:(', 1):
            _restore_inherited_dacl(handle, descriptor)
            actual = _security(handle)
        if actual != expected:
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
