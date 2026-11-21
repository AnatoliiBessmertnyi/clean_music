"""Права пустой копии проверяются до переноса байтов на Windows/NTFS."""

import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

import tag_ntfs

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='требуется Windows/NTFS')


def test_copy_checks_security_before_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = tmp_path / 'service'
    service.mkdir()
    subprocess.run(
        ['icacls', str(service), '/grant', '*S-1-1-0:(OI)(CI)(RX)'],
        stdout=subprocess.DEVNULL,
        check=True,
    )
    path = tmp_path / 'original.bin'
    path.write_bytes(b'synthetic payload')
    inherited = service / 'default.bin'
    inherited.write_bytes(b'diagnostic')
    with (
        tag_ntfs.guarded_file(path) as (source, contents),
        tag_ntfs.guarded_file(inherited) as (other, comparison),
    ):
        attributes = tag_ntfs.read_attributes(source, contents)
        assert (
            tag_ntfs.read_attributes(other, comparison).security != attributes.security
        )
        work = service / 'work.bin'
        registered = Mock()
        tag_ntfs.copy_protected(contents, work, attributes, registered)
        assert work.read_bytes() == b'synthetic payload'
        with tag_ntfs.guarded_file(work) as (work_handle, stream):
            assert (
                tag_ntfs.read_attributes(work_handle, stream).security
                == attributes.security
            )
        registered.assert_called_once()

        monkeypatch.setattr(tag_ntfs, '_security', lambda _: 'different')
        refused = service / 'refused.bin'
        with pytest.raises(OSError, match='права пустой рабочей копии'):
            tag_ntfs.copy_protected(contents, refused, attributes, Mock())
        assert refused.stat().st_size == 0
