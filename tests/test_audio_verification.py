"""Реальные синтетические контейнеры и проверка payload после Mutagen.save()."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from mutagen import File
from mutagen.id3 import TIT2, TXXX
from mutagen.mp4 import MP4FreeForm

from audio_verification import audio_signature


@pytest.mark.parametrize(
    ('extension', 'codec', 'title', 'artist'),
    [
        ('mp3', 'libmp3lame', 'TIT2', 'TPE1'),
        ('m4a', 'aac', '©nam', '©ART'),
        ('mp4', 'aac', '©nam', '©ART'),
        ('flac', 'flac', 'title', 'artist'),
        ('ogg', 'libvorbis', 'title', 'artist'),
        ('wma', 'wmav2', 'Title', 'Author'),
    ],
)
def test_audio_verification(
    tmp_path: Path, extension: str, codec: str, title: str, artist: str
) -> None:
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if ffmpeg is None or ffprobe is None:
        pytest.skip('для синтетических контейнеров нужны ffmpeg и ffprobe')
    original = tmp_path / f'original.{extension}'
    work = tmp_path / f'work.{extension}'
    subprocess.run(
        [
            ffmpeg,
            '-v',
            'error',
            '-f',
            'lavfi',
            '-i',
            'sine=frequency=997:sample_rate=44100:duration=2',
            '-c:a',
            codec,
            '-metadata',
            'title=Original',
            '-metadata',
            'artist=Synthetic',
            str(original),
        ],
        check=True,
    )
    initial = audio_signature(ffprobe, original)
    audio = File(original)
    assert audio is not None
    multiple = ['First', 'Second']
    if extension == 'mp3':
        extra = 'TXXX:fixture'
        audio[extra] = TXXX(encoding=3, desc='fixture', text=multiple)
    elif extension in ('m4a', 'mp4'):
        extra = '----:com.example:fixture'
        audio[extra] = [MP4FreeForm(item.encode()) for item in multiple]
    else:
        extra = 'WM/Genre' if extension == 'wma' else 'genre'
        audio[extra] = multiple
    audio.save()
    assert audio_signature(ffprobe, original) == initial
    reference = File(original)
    assert reference is not None
    preserved = reference[extra]
    assert len(preserved.text if extension == 'mp3' else preserved) == 2
    shutil.copyfile(original, work)
    expected = audio_signature(ffprobe, original)
    for value in ('Updated title', 'Second updated title'):
        audio = File(work)
        assert audio is not None
        audio[title] = TIT2(encoding=3, text=[value]) if extension == 'mp3' else [value]
        audio.save()
        updated = File(work)
        assert updated is not None
        assert str(updated[title] if extension == 'mp3' else updated[title][0]) == value
        assert (
            str(updated[artist] if extension == 'mp3' else updated[artist][0])
            == 'Synthetic'
        )
        actual = updated[extra]
        if extension == 'mp3':
            assert actual.text == preserved.text
        elif extension == 'wma':
            assert [(type(item), item.value) for item in actual] == [
                (type(item), item.value) for item in preserved
            ]
        else:
            assert actual == preserved
        assert audio_signature(ffprobe, work) == expected
    raw = subprocess.check_output(
        [
            ffprobe,
            '-v',
            'error',
            '-select_streams',
            'a',
            '-show_packets',
            '-of',
            'json',
            str(original),
        ]
    )
    packets = json.loads(raw)['packets']
    if extension == 'wma':
        # ASF pos указывает на начало контейнерного пакета, не на payload.
        packet_offset = bytes(original.read_bytes()).find(
            bytes.fromhex('3626b2758e66cf11a6d900aa0062ce6c')
        )
        assert packet_offset >= 0
        offset = packet_offset + 50 + 250
    elif extension == 'ogg':
        page = int(packets[len(packets) // 2]['pos'])
        data = original.read_bytes()
        assert data[page : page + 4] == b'OggS'
        body = page + 27 + data[page + 26]
        offset = body + sum(data[page + 27 : body]) // 2
    else:
        packet = packets[len(packets) // 2]
        offset = int(packet['pos']) + int(packet['size']) // 2
    shutil.copyfile(original, work)
    with work.open('r+b') as stream:
        stream.seek(offset)
        previous = stream.read(1)
        stream.seek(offset)
        stream.write(bytes([previous[0] ^ 1]))
    if extension == 'ogg':
        with pytest.raises(OSError, match='ffprobe'):
            audio_signature(ffprobe, work)
    else:
        assert audio_signature(ffprobe, work)[0] != expected[0]
