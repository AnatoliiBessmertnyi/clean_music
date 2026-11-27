"""Контракт подтверждения удаления по дескриптору на локальном NTFS."""

from pathlib import Path
from shutil import copyfile

from duplicate_analysis import find_candidate_groups
from duplicate_removal import file_stamp, remove_exact_copies
from file_state import FileRegistry, OperationStatus, inspect_file, regular_file_info
from tag_repository import TagRepository
from tests.test_tag_publication import windows
from tests.test_tag_repository import write_mp3


@windows
def test_remove_exact_copy_by_handle(tmp_path: Path) -> None:
    winner = write_mp3(tmp_path / 'Artist - Song.mp3', artist='Artist', title='Song')
    redundant = tmp_path / 'Artist - Song (1).mp3'
    copyfile(winner, redundant)
    original = inspect_file(winner), winner.read_bytes()
    paths = [winner, redundant]
    registry, repo = FileRegistry(), TagRepository()
    for path in paths:
        registry.register(path, identity=inspect_file(path))
    [group] = find_candidate_groups(paths, repo)
    expected = {path: file_stamp(regular_file_info(path)) for path in paths}

    [(path, status, reason)] = remove_exact_copies(
        group, winner, (redundant,), expected, registry, repo
    )
    assert path == redundant and status is OperationStatus.SUCCESS, reason
    assert not redundant.exists()
    assert (inspect_file(winner), winner.read_bytes()) == original
