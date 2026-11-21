import os
from pathlib import Path

import pytest

from recovery_store import DIRECTORY_NAME, RecoveryStore


def test_diagnostic_does_not_create_state(tmp_path: Path) -> None:
    with RecoveryStore(tmp_path, diagnostic=True).session() as exists:
        assert not exists
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('diagnostic', [True, False])
def test_existing_foreign_directory_is_preserved(
    tmp_path: Path, diagnostic: bool
) -> None:
    directory = tmp_path / DIRECTORY_NAME
    directory.mkdir()
    foreign = directory / 'foreign.mp3'
    foreign.write_bytes(b'user file')
    with (
        pytest.raises(OSError),
        RecoveryStore(tmp_path, diagnostic=diagnostic).session(),
    ):
        pytest.fail('чужой каталог не должен использоваться')
    assert list(directory.iterdir()) == [foreign]
    assert foreign.read_bytes() == b'user file'


@pytest.mark.skipif(os.name != 'nt', reason='блокировка Windows')
def test_lock_and_record_lifecycle(tmp_path: Path) -> None:
    store = RecoveryStore(tmp_path)
    record = store.directory / ('a' * 32 + '.json')
    with store.session():
        store.write(record, {'step': 'intent'}, initial=True)
        assert store.read(record) == {'step': 'intent'}
        with pytest.raises(PermissionError):
            (store.directory / 'lock').unlink()
        with (
            pytest.raises(OSError, match='блокировку'),
            RecoveryStore(tmp_path).session(),
        ):
            pytest.fail('вторая сессия получила блокировку')
        store.write(record, {'step': 'done'}, initial=False)
        assert store.read(record) == {'step': 'done'}
    with pytest.raises(OSError, match='блокировки'):
        store.remove(record)
    with RecoveryStore(tmp_path, diagnostic=True).session() as exists:
        assert exists
        assert store.read(record) == {'step': 'done'}
    with store.session():
        store.remove(record)
        assert store.records() == []
