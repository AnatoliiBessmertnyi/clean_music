import os
from pathlib import Path

import pytest

from context import SessionContext
from file_state import FileRegistry, OperationResult, OperationStatus


def test_register_is_idempotent_by_normalized_absolute_path() -> None:
    registry = FileRegistry()
    path = Path('Track.mp3')
    entry = registry.register(path)
    assert registry.register(path.absolute()) == entry
    assert registry.id_for(path) == entry.id

    variant = Path('track.mp3')
    variant_entry = registry.register(variant)
    if os.path.normcase(path) == os.path.normcase(variant):
        assert variant_entry == entry
        assert registry.current_paths() == [path]
    else:
        assert variant_entry.id > entry.id
        assert registry.current_paths() == [path, variant]


def test_update_path_preserves_id_and_releases_old_path() -> None:
    registry = FileRegistry()
    source, target = Path('source.mp3'), Path('target.mp3')
    entry = registry.register(source)

    registry.update_path(entry.id, target)

    assert registry.id_for(source) is None
    assert registry.id_for(target) == entry.id
    assert registry.path_of(entry.id) == target
    assert registry.register(source).id > entry.id


def test_update_path_rejects_occupied_target_atomically() -> None:
    registry = FileRegistry()
    source, target = Path('source.mp3'), Path('target.mp3')
    first = registry.register(source)
    second = registry.register(target)

    with pytest.raises(ValueError, match='Путь уже зарегистрирован'):
        registry.update_path(first.id, target)

    assert registry.current_paths() == [source, target]
    assert registry.register(source) == first
    assert registry.register(target) == second


def test_current_paths_keeps_registration_order_and_omits_missing() -> None:
    registry = FileRegistry()
    first = registry.register(Path('z.mp3'))
    second = registry.register(Path('a.mp3'))
    third = registry.register(Path('m.mp3'))
    target = Path('new.mp3')

    registry.update_path(first.id, target)
    assert registry.current_paths() == [target, second.path, third.path]

    registry.mark_missing(second.id)
    assert registry.current_paths() == [target, third.path]
    assert registry.id_for(second.path) == second.id
    assert registry.path_of(second.id) == second.path
    assert not registry.register(second.path).present


@pytest.mark.parametrize(
    ('status', 'confirmed', 'model', 'blocks'),
    [
        (OperationStatus.SUCCESS, True, False, False),
        (OperationStatus.NOOP, False, False, False),
        (OperationStatus.SIMULATED, False, True, False),
        (OperationStatus.CONFLICT, False, False, False),
        (OperationStatus.ERROR, False, False, False),
        (OperationStatus.UNCERTAIN, False, False, True),
    ],
)
def test_operation_result_properties(
    status: OperationStatus, confirmed: bool, model: bool, blocks: bool
) -> None:
    result = OperationResult(status, None, 'причина')

    assert result.is_confirmed_change is confirmed
    assert result.is_model_change is model
    assert result.blocks_dependents is blocks


def test_session_contexts_have_independent_registries() -> None:
    first = SessionContext(dry_run=False, debug=False)
    second = SessionContext(dry_run=False, debug=False)
    path = Path('track.mp3')

    first.registry.register(path)

    assert first.registry.current_paths() == [path]
    assert second.registry.current_paths() == []
