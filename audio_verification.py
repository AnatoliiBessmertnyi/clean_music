"""Проверка аудиопотока перед публикацией тегов."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

_FORMATS = {
    '.mp3': 'mp3',
    '.m4a': 'mov',
    '.mp4': 'mov',
    '.flac': 'flac',
    '.ogg': 'ogg',
    '.wma': 'asf',
}
_SHA256 = re.compile(r'SHA256:[a-f0-9]{64}')
_STREAM_FIELDS = (
    'index',
    'codec_name',
    'profile',
    'codec_tag_string',
    'sample_fmt',
    'sample_rate',
    'channels',
    'channel_layout',
    'bits_per_raw_sample',
    'time_base',
    'start_pts',
    'duration_ts',
    'extradata_size',
    'extradata_hash',
    'initial_padding',
    'trailing_padding',
)
_PACKET_FIELDS = (
    'stream_index',
    'pts',
    'dts',
    'duration',
    'size',
    'data_hash',
    'side_data_list',
)
_CODECS = {
    '.mp3': 'mp3',
    '.m4a': 'aac',
    '.mp4': 'aac',
    '.flac': 'flac',
    '.ogg': 'vorbis',
    '.wma': 'wmav2',
}


def find_ffmpeg_tool(name: str) -> str | None:
    for directory in os.get_exec_path():
        candidate = Path(directory)
        if candidate.is_absolute():
            command = shutil.which(str(candidate / name))
            if command is not None:
                return command
    return None


def find_ffprobe() -> str:
    command = find_ffmpeg_tool('ffprobe')
    if command is not None:
        return command
    raise OSError(
        'ffprobe не найден. Установите FFmpeg и добавьте каталог ffprobe в PATH.'
    )


def audio_signature(command: str, path: Path) -> tuple[str, str]:
    """Хранит только отпечатки между двумя вызовами проверки."""
    suffix = path.suffix.lower()
    if suffix not in _FORMATS:
        raise OSError('неподдерживаемый профиль контейнера')
    container = container_signature(path)
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(
            [
                command,
                '-v',
                'error',
                '-show_error',
                '-show_streams',
                '-show_packets',
                '-count_packets',
                '-show_data_hash',
                'sha256',
                '-of',
                'json',
                '-protocol_whitelist',
                'file',
                '-f',
                _FORMATS[suffix],
                str(path.absolute()),
            ],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode or result.stderr:
            raise OSError('ffprobe не смог полностью прочитать аудиопоток')
        output.seek(0)
        try:
            data = json.load(output)
            streams = [
                stream
                for stream in data['streams']
                if not stream.get('disposition', {}).get('attached_pic')
            ]
            (stream,) = streams
            if (
                'error' in data
                or stream['codec_type'] != 'audio'
                or stream['codec_name'] != _CODECS[suffix]
            ):
                raise ValueError
            if not all(
                stream.get(field) for field in ('sample_rate', 'channels', 'time_base')
            ):
                raise ValueError
            if suffix != '.mp3' and not _SHA256.fullmatch(
                stream.get('extradata_hash', '')
            ):
                raise ValueError
            # ASF: оценка ffprobe зависит от размещения данных. Оригинальные
            # времена File Properties и тайминг всех пакетов проверяются отдельно.
            fields = (
                tuple(key for key in _STREAM_FIELDS if key != 'duration_ts')
                if suffix == '.wma'
                else _STREAM_FIELDS
            )
            digest = hashlib.sha256(
                json.dumps([stream.get(key) for key in fields]).encode()
            )
            count = 0
            for packet in data['packets']:
                if packet['stream_index'] != stream['index']:
                    continue
                if (
                    not _SHA256.fullmatch(packet.get('data_hash', ''))
                    or int(packet['size']) <= 0
                ):
                    raise ValueError
                digest.update(
                    json.dumps(
                        [packet.get(key) for key in _PACKET_FIELDS], sort_keys=True
                    ).encode()
                )
                count += 1
            if not count or count != int(stream['nb_read_packets']):
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise OSError(
                'неподдерживаемый профиль или неполный результат ffprobe'
            ) from exc
    return digest.hexdigest(), container


def _read(stream: BinaryIO, count: int) -> bytes:
    data = stream.read(count)
    if len(data) != count:
        raise OSError('неполный контейнер')
    return data


def _hash_region(stream: BinaryIO, count: int) -> str:
    digest = hashlib.sha256()
    while count:
        block = _read(stream, min(count, 1024 * 1024))
        digest.update(block)
        count -= len(block)
    return digest.hexdigest()


def _mp3(stream: BinaryIO, size: int) -> str:
    header = _read(stream, 10)
    start = 0
    if header.startswith(b'ID3'):
        if header[3] != 4 or header[5] or any(n > 127 for n in header[6:]):
            raise OSError('неподдерживаемый заголовок ID3')
        size_without_header = 0
        for n in header[6:]:
            size_without_header = (size_without_header << 7) | n
        start = 10 + size_without_header
    stream.seek(size - 128 if size >= 128 else 0)
    if size >= 128 and stream.read(3) == b'TAG':
        raise OSError('сосуществующий ID3v1 не поддерживается при записи')
    if start >= size:
        raise OSError('нет MPEG-кадров')
    stream.seek(start)
    sync = _read(stream, 2)
    if sync[0] != 255 or sync[1] & 0xE6 != 0xE2:
        raise OSError('не найдена граница MPEG Layer III')
    stream.seek(start)
    return _hash_region(stream, size - start)


def _flac(stream: BinaryIO, size: int) -> str:
    if _read(stream, 4) != b'fLaC':
        raise OSError('ожидался native FLAC')
    digest = hashlib.sha256()
    first = True
    while True:
        header = _read(stream, 4)
        kind = header[0] & 127
        length = int.from_bytes(header[1:], 'big')
        if (
            (kind == 0) != first
            or (first and length != 34)
            or kind > 6
            or stream.tell() + length >= size
        ):
            raise OSError('неверные блоки FLAC')
        if kind not in (1, 4, 6):
            digest.update(bytes([kind]) + header[1:])
            digest.update(bytes.fromhex(_hash_region(stream, length)))
        else:
            stream.seek(length, 1)
        first = False
        if header[0] & 128:
            break
    digest.update(bytes.fromhex(_hash_region(stream, size - stream.tell())))
    return digest.hexdigest()


def container_signature(path: Path) -> str:
    """Учитывает данные, которые демультиплексор может скрывать."""
    with path.open('rb') as stream:
        size = stream.seek(0, 2)
        stream.seek(0)
        match path.suffix.lower():
            case '.mp3':
                return _mp3(stream, size)
            case '.flac':
                return _flac(stream, size)
            case '.mp4' | '.m4a':
                return _mp4(stream, size)
            case '.ogg':
                return _ogg(stream, size)
            case '.wma':
                return _asf(stream, size)
            case _:
                raise OSError('профиль контейнера ещё не подтверждён')


def _boxes(stream: BinaryIO, end: int) -> Iterator[tuple[bytes, int, int]]:
    while stream.tell() < end:
        start = stream.tell()
        header = _read(stream, 8)
        length = int.from_bytes(header[:4], 'big')
        if length == 1:
            length = int.from_bytes(_read(stream, 8), 'big')
        elif length == 0:
            length = end - start
        limit = start + length
        if not stream.tell() <= limit <= end:
            raise OSError('неверный размер блока MP4')
        yield header[4:], stream.tell(), limit
        stream.seek(limit)


def _mp4(stream: BinaryIO, size: int) -> str:
    top = list(_boxes(stream, size))
    media = [(start, end) for kind, start, end in top if kind == b'mdat']
    if len(media) != 1 or [kind for kind, _, _ in top].count(b'moov') != 1:
        raise OSError('ожидался нефрагментированный MP4 с одним mdat')
    media_start, media_end = media[0]
    digest = hashlib.sha256()

    def visit(kind: bytes, start: int, end: int) -> None:
        if kind in (b'free', b'skip'):
            return
        if kind == b'udta':
            stream.seek(start)
            for child, child_start, child_end in _boxes(stream, end):
                if child == b'meta':
                    stream.seek(child_start + 4)
                    if any(
                        meta not in (b'hdlr', b'ilst', b'free')
                        for meta, _, _ in _boxes(stream, child_end)
                    ):
                        raise OSError('неподдерживаемые метаданные MP4')
                elif child not in (b'free', b'skip'):
                    raise OSError('неподдерживаемый блок udta')
            return
        digest.update(kind)
        stream.seek(start)
        if kind in (b'moov', b'trak', b'mdia', b'minf', b'stbl', b'dinf'):
            children = list(_boxes(stream, end))
            if (
                kind == b'moov'
                and sum(child == b'trak' for child, _, _ in children) != 1
            ):
                raise OSError('MP4 должен содержать одну дорожку')
            for child, child_start, child_end in children:
                visit(child, child_start, child_end)
        elif kind == b'dref':
            if end - start != 20 or _read(stream, 20) != bytes.fromhex(
                '00000000000000010000000c75726c2000000001'
            ):
                raise OSError('внешняя или неизвестная ссылка на данные MP4')
        elif kind in (b'stco', b'co64'):
            width = 4 if kind == b'stco' else 8
            header = _read(stream, 8)
            count = int.from_bytes(header[4:], 'big')
            if header[:4] != bytes(4) or stream.tell() + count * width != end:
                raise OSError('неверная таблица смещений MP4')
            digest.update(header)
            for _ in range(count):
                offset = int.from_bytes(_read(stream, width), 'big')
                if not media_start <= offset < media_end:
                    raise OSError('смещение MP4 вне mdat')
                digest.update((offset - media_start).to_bytes(8, 'big'))
        else:
            digest.update(bytes.fromhex(_hash_region(stream, end - start)))
        digest.update(b'\x00')

    for kind, start, end in top:
        if kind not in (b'ftyp', b'moov', b'mdat', b'free', b'skip', b'wide'):
            raise OSError('неподдерживаемый блок верхнего уровня MP4')
        visit(kind, start, end)
    return digest.hexdigest()


def _ogg(stream: BinaryIO, size: int) -> str:
    digest = hashlib.sha256()
    serial = None
    sequence = None
    ordinal = 0
    packet = bytearray()
    ended = False
    while stream.tell() < size:
        header = _read(stream, 27)
        flags = header[5]
        current_serial = header[14:18]
        current_sequence = int.from_bytes(header[18:22], 'little')
        if (
            header[:5] != b'OggS\x00'
            or ended
            or flags & ~7
            or bool(flags & 2) != (serial is None)
            or bool(flags & 1) != bool(packet)
            or serial is not None
            and serial != current_serial
            or sequence is not None
            and current_sequence != (sequence + 1) % (1 << 32)
        ):
            raise OSError('неподдерживаемая структура Ogg')
        serial, sequence = current_serial, current_sequence
        previous = ordinal
        for length in _read(stream, header[26]):
            packet.extend(_read(stream, length))
            if length == 255:
                continue
            if ordinal < 3 and not packet.startswith(
                (b'\x01vorbis', b'\x03vorbis', b'\x05vorbis')[ordinal]
            ):
                raise OSError('ожидались заголовки Vorbis')
            if ordinal != 1:
                digest.update(hashlib.sha256(packet).digest())
            ordinal += 1
            packet.clear()
        ended = bool(flags & 4)
        if ordinal > max(previous, 3):
            digest.update(ordinal.to_bytes(8, 'little') + header[6:14])
    if not ended or packet or ordinal <= 3:
        raise OSError('неполный поток Vorbis')
    return digest.hexdigest()


def _asf(stream: BinaryIO, size: int) -> str:
    def guid(value: str) -> bytes:
        return UUID(value).bytes_le

    header = guid('75b22630-668e-11cf-a6d9-00aa0062ce6c')
    data = guid('75b22636-668e-11cf-a6d9-00aa0062ce6c')
    file_properties = guid('8cabdca1-a947-11cf-8ee4-00c00c205365')
    ignored = {
        guid('75b22633-668e-11cf-a6d9-00aa0062ce6c'),  # Content Description
        guid('d2d0a440-e307-11d2-97f0-00a0c95ea850'),  # Extended Content Description
        guid('1806d474-cadf-4509-a4ba-9aabcb96aae8'),  # Padding
    }
    extension = guid('5fbf03b5-a92e-11cf-8ee3-00c00c205365')
    metadata = {
        guid('c5f8cbea-5baf-4877-8467-aa8c44fa4cca'),
        guid('44231c94-9498-49d1-a141-1d134e457054'),
    }
    encrypted = {
        guid('2211b3fb-bd23-11d2-b4b7-00a0c955fc6e'),
        guid('298ae614-2622-4c17-b935-dae07ee9289c'),
    }
    stream_properties = guid('b7dc0791-a9b7-11cf-8ee6-00c00c205365')
    codec_list = guid('86d15240-311d-11d0-a3a4-00a0c90348f6')
    indices = {
        guid('33000890-e5b1-11cf-89f4-00a0c90349cb'),
        guid('d6e229d3-35da-11d1-9034-00a0c90349be'),
    }
    digest = hashlib.sha256()

    def object_header(limit: int) -> tuple[bytes, int]:
        start = stream.tell()
        kind = _read(stream, 16)
        length = int.from_bytes(_read(stream, 8), 'little')
        if not 24 <= length <= limit - start or kind in encrypted:
            raise OSError('неподдерживаемый объект ASF')
        return kind, start + length

    kind, end = object_header(size)
    if kind != header:
        raise OSError('ожидался заголовок ASF')
    count = int.from_bytes(_read(stream, 4), 'little')
    _read(stream, 2)
    for _ in range(count):
        kind, limit = object_header(end)
        if kind == file_properties:
            digest.update(kind + _read(stream, 16))
            _read(stream, 8)  # размер всего файла меняется с тегами
            digest.update(bytes.fromhex(_hash_region(stream, limit - stream.tell())))
        elif kind == extension:
            digest.update(kind + _read(stream, 18))
            if int.from_bytes(_read(stream, 4), 'little') != limit - stream.tell():
                raise OSError('неполное расширение ASF')
            while stream.tell() < limit:
                child, child_end = object_header(limit)
                if child not in metadata:
                    raise OSError('неподдерживаемое расширение ASF')
                stream.seek(child_end)
        elif kind in (stream_properties, codec_list):
            digest.update(kind)
            digest.update(bytes.fromhex(_hash_region(stream, limit - stream.tell())))
        elif kind not in ignored:
            raise OSError('неподдерживаемый заголовок ASF')
        stream.seek(limit)
    if stream.tell() != end:
        raise OSError('неполный заголовок ASF')
    kind, end = object_header(size)
    if kind != data:
        raise OSError('отсутствуют пакеты ASF')
    digest.update(bytes.fromhex(_hash_region(stream, end - stream.tell())))
    while stream.tell() < size:
        kind, end = object_header(size)
        if kind not in indices:
            raise OSError('неподдерживаемый индекс ASF')
        digest.update(kind)
        digest.update(bytes.fromhex(_hash_region(stream, end - stream.tell())))
    return digest.hexdigest()
