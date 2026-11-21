"""Проверки сохранности файлов и тегов в этапах конвейера."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

import pytest

import stages
from context import SessionContext
from file_operations import FileExecutor
from file_state import OperationStatus
from interactor import ConsoleInteractor, UserInteractor
from pipeline import AudioProcessingPipeline
from pipeline_utils import simulate_rename
from tag_repository import AudioTags, ReadStatus, TagRepository
from tests.test_pipeline_utils import Interactor, MemoryRepository, windows
from tests.test_tag_repository import write_mp3


@pytest.fixture
def model_platform_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Подменяет проверку NTFS в WSL для тестов с dry-run и кэшем в памяти."""
    if os.name != 'nt':
        guard = Mock(return_value=True)
        monkeypatch.setattr('file_operations.modification_allowed', guard)
        monkeypatch.setattr('pipeline_utils.modification_allowed', guard)


@pytest.mark.usefixtures('model_platform_guard')
@pytest.mark.parametrize(
    ('source_title', 'title'),
    [
        ('Песня (Live)', 'Песня (Live)'),
        ('Песня (Ирина Remix)', 'Песня (Ирина Remix)'),
        ('Песня (Acoustic)', 'Песня (Acoustic)'),
        ('Песня (Instrumental)', 'Песня (Instrumental)'),
        ('Песня [2024 Remaster]', 'Песня [2024 Remaster]'),
        ('Песня (Radio Edit)', 'Песня (Radio Edit)'),
        ('Песня (Album Version)', 'Песня (Album Version)'),
        ('Песня (Архивная запись)', 'Песня (Архивная запись)'),
        ('Песня (Live) [Ирина Remix]', 'Песня (Live) [Ирина Remix]'),
        ('Песня (Live) (vksaver)', 'Песня (Live)'),
        ('Песня (Live [vksaver])', 'Песня (Live)'),
        ('Песня (Live) (ft. Ирина)', 'Песня (Live) (feat. Ирина)'),
        ('Песня feat. Ирина (Live)', 'Песня feat. Ирина (Live)'),
        ('Песня (Концерт feat. Ирина)', 'Песня (Концерт feat. Ирина)'),
    ],
)
def test_tag_writing_preserves_annotations(
    tmp_path: Path, source_title: str, title: str
) -> None:
    source = tmp_path / f'Исполнитель - {source_title}.mp3'
    source.touch()
    ctx, repo, ui = SessionContext(True, False), MemoryRepository(), Interactor('y')
    [current] = stages.TagWritingStage().execute([source], ctx, repo, ui)
    assert repo.get_tags(current).complete_tags == AudioTags('Исполнитель', title)


@pytest.mark.parametrize('denied', [False, True])
def test_tag_writing_skips_unreadable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, denied: bool
) -> None:
    source = tmp_path / 'Filename Artist - Filename Title.mp3'
    source.write_bytes(b'corrupt')
    if denied:
        monkeypatch.setattr(
            'tag_repository.MP3', Mock(side_effect=PermissionError('denied'))
        )
    repo = TagRepository()
    write = Mock()
    monkeypatch.setattr(repo, 'apply_tags', write)
    ui = Interactor()
    ctx = SessionContext(False, False)
    assert stages.TagWritingStage().execute([source], ctx, repo, ui) == [source]
    assert not write.called and not ui.prompts
    assert source.read_bytes() == b'corrupt'


@pytest.mark.usefixtures('model_platform_guard')
@pytest.mark.parametrize('dry_run', [True, pytest.param(False, marks=windows)])
@pytest.mark.parametrize(
    ('artist', 'title', 'expected'),
    [
        ('AC/DC', '', AudioTags('AC/DC', 'Filename Title')),
        ('', 'Original / Title', AudioTags('Filename Artist', 'Original / Title')),
    ],
)
def test_tag_writing_fills_only_missing_field(
    tmp_path: Path, dry_run: bool, artist: str, title: str, expected: AudioTags
) -> None:
    source = write_mp3(
        tmp_path / 'Filename Artist - Filename Title.mp3', artist=artist, title=title
    )
    repo = TagRepository()
    before = source.read_bytes()
    ctx = SessionContext(dry_run, False)
    [result] = stages.TagWritingStage().execute([source], ctx, repo, Interactor())
    assert repo.get_tags(result).tags == expected
    if dry_run:
        assert not result.exists() and source.read_bytes() == before
        assert repo.read_tags_uncached(source).tags == AudioTags(artist, title)
    else:
        assert repo.read_tags_uncached(result).tags == expected


@pytest.mark.usefixtures('model_platform_guard')
@pytest.mark.parametrize('dry_run', [True, pytest.param(False, marks=windows)])
@pytest.mark.parametrize('artist', ['AC/DC', 'Исполнитель/Группа'])
def test_tag_renaming_preserves_metadata_on_second_pass(
    tmp_path: Path, dry_run: bool, artist: str
) -> None:
    source = tmp_path / 'Песня.mp3'
    source.touch()
    ctx, repo, ui = SessionContext(dry_run, False), MemoryRepository(), Interactor()
    assert repo.update_tags(
        source, f'{artist} featuring Ирина', 'Песня (Live) [A/B Remix]'
    )
    repo.writes.clear()
    expected = AudioTags(artist, 'Песня (Live) [A/B Remix] (feat. Ирина)')
    target = (
        tmp_path
        / f'{artist.replace("/", "")} - Песня (Live) [AB Remix] (feat. Ирина).mp3'
    )
    stage = stages.TagRenamingStage()

    assert stage.execute([source], ctx, repo, ui) == [target]
    assert repo.get_tags(target).complete_tags == expected
    assert stage.execute([target], ctx, repo, ui) == [target]
    assert repo.get_tags(target).complete_tags == expected
    assert source.exists() == dry_run
    assert target.exists() != dry_run
    assert len(repo.writes) == int(not dry_run)


@pytest.mark.usefixtures('model_platform_guard')
def test_manual_artist_keeps_case_through_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'Song.mp3'
    source.touch()
    ctx, repo = SessionContext(True, False), MemoryRepository()
    ctx.missing_files = [source]
    monkeypatch.setattr(stages, 'modification_allowed', Mock(return_value=True))
    monkeypatch.setattr('builtins.input', Mock(side_effect=['Y', 'McCartney']))

    [current] = stages.ManualInputStage().execute(
        [source], ctx, repo, ConsoleInteractor()
    )

    assert current.name == 'McCartney - Song.mp3'


@pytest.mark.parametrize('answer', ['N', 'ошибка'])
def test_invalid_filename_group_skip_never_calls_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    files = [tmp_path / 'First.mp3', tmp_path / 'Second.mp3']
    ctx, repo, ui = SessionContext(False, False), MemoryRepository(), Interactor(answer)
    for source in files:
        source.touch()
        repo.simulate_update_tags(source, 'AC/DC', source.stem)
    rename = Mock()
    monkeypatch.setattr(stages._file_executor, 'rename', rename)

    assert stages.TagRenamingStage().execute(files, ctx, repo, ui) == files
    assert len(ui.prompts) == 1
    rename.assert_not_called()


@pytest.mark.usefixtures('model_platform_guard')
@pytest.mark.parametrize('dry_run', [True, pytest.param(False, marks=windows)])
def test_mismatch_write_retains_artist_sanitized_in_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    source = tmp_path / 'ACDC - Correct title.mp3'
    source.touch()
    ctx, repo, ui = SessionContext(dry_run, False), MemoryRepository(), Interactor()
    repo.simulate_update_tags(source, 'AC/DC', 'Old title')
    monkeypatch.setattr(ui, 'prompt', Mock(side_effect=['1', 'f']))

    stage = stages.TagRenamingStage()
    assert stage.execute([source], ctx, repo, ui) == [source]
    assert repo.get_tags(source).complete_tags == AudioTags('AC/DC', 'Correct title')


@pytest.mark.usefixtures('model_platform_guard')
def test_encoding_repair_preserves_title_annotations(tmp_path: Path) -> None:
    source = tmp_path / 'Исполнитель - Песня.mp3'
    source.touch()
    ctx, repo, ui = SessionContext(True, False), MemoryRepository(), Interactor('y')
    repo.simulate_update_tags(source, 'ÐÐ', 'Песня (Live)')

    assert stages.TagRenamingStage().execute([source], ctx, repo, ui) == [source]
    assert repo.get_tags(source).complete_tags == AudioTags(
        'Исполнитель', 'Песня (Live)'
    )


@windows
@pytest.mark.parametrize('conflict', [False, True])
def test_junk_rename_failure_blocks_tag_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conflict: bool
) -> None:
    path = tmp_path / 'Artist - Title (vksaver).mp3'
    target = tmp_path / 'Artist - Title.mp3'
    path.write_bytes(b'original')
    repo, ctx, ui = MemoryRepository(), SessionContext(False, False), Interactor()
    repo.simulate_update_tags(path, 'Artist', 'Title (vksaver)')
    files = [path]
    if conflict:
        target.write_bytes(b'occupied')
        files.append(target)
    else:
        monkeypatch.setattr(Path, 'rename', Mock(side_effect=PermissionError('locked')))
    assert stages.JunkCleaningStage().execute(files, ctx, repo, ui) == files
    assert ctx.get_block_reason(path) == (
        'конфликт целевого имени; оба файла сохранены' if conflict else 'locked'
    )
    if conflict:
        assert target.read_bytes() == b'occupied'
    assert not repo.writes and not ctx.get_stat('junk_removed')
    assert path.read_bytes() == b'original'


@windows
@pytest.mark.parametrize('dry_run', [False, True])
def test_junk_then_normalization_preserves_file_id(
    tmp_path: Path, dry_run: bool
) -> None:
    old = tmp_path / 'Artist \u2013 Title (vksaver).mp3'
    middle = tmp_path / 'Artist \u2013 Title.mp3'
    final = tmp_path / 'Artist - Title.mp3'
    neighbor = tmp_path / 'Neighbor.mp3'
    old.write_bytes(b'original')
    neighbor.write_bytes(b'neighbor')
    ctx, repo, ui = SessionContext(dry_run, False), MemoryRepository(), Interactor()
    file_id = ctx.registry.register(old).id
    pipeline = AudioProcessingPipeline(
        [stages.JunkCleaningStage(), stages.NormalizationStage()]
    )

    assert pipeline.run([neighbor, old], ctx, repo, ui) == [neighbor, final]
    assert ctx.registry.path_of(file_id) == final
    assert ctx.registry.id_for(final) == file_id
    assert ctx.registry.id_for(old) is None and ctx.registry.id_for(middle) is None
    assert not middle.exists()
    assert old.exists() == dry_run
    assert final.exists() == (not dry_run)
    assert (old if dry_run else final).read_bytes() == b'original'
    assert neighbor.read_bytes() == b'neighbor'
    assert ctx.get_stat('junk_removed') == int(not dry_run)
    assert ctx.get_stat('dash_normalized') == int(not dry_run)


@windows
@pytest.mark.parametrize('outcome', ['success', 'simulated', 'denied'])
def test_tag_rename_passes_current_paths_to_mismatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    old = tmp_path / 'Title.mp3'
    new = tmp_path / 'Artist - Title.mp3'
    neighbor = tmp_path / 'Neighbor.mp3'
    untagged = tmp_path / 'Untagged.mp3'
    old.write_bytes(b'original')
    neighbor.write_bytes(b'neighbor')
    untagged.touch()
    ctx = SessionContext(outcome == 'simulated', False)
    repo, ui = MemoryRepository(), Interactor()
    repo.simulate_update_tags(old, 'Artist', 'Title')
    repo.simulate_update_tags(neighbor, 'Artist', 'Other')
    file_id = ctx.registry.register(old).id
    neighbor_id = ctx.registry.register(neighbor).id
    if outcome == 'denied':
        monkeypatch.setattr(Path, 'rename', Mock(side_effect=PermissionError('locked')))

    stage = stages.TagRenamingStage()
    handle_mismatches = Mock(side_effect=lambda files, *_: files)
    monkeypatch.setattr(stage, '_handle_mismatches', handle_mismatches)

    current = old if outcome == 'denied' else new
    expected = [neighbor, current, untagged]
    assert stage.execute([neighbor, old, untagged], ctx, repo, ui) == expected
    handle_mismatches.assert_called_once_with(expected, ctx, repo, ui)
    assert stage._pending_mismatches == [(neighbor, 'Neighbor', 'Artist - Other.mp3')]
    assert ctx.missing_files == [untagged]
    assert ctx.registry.path_of(file_id) == current
    assert ctx.registry.id_for(current) == file_id
    assert ctx.registry.path_of(neighbor_id) == neighbor
    assert ctx.registry.id_for(neighbor) == neighbor_id
    assert ctx.get_block_reason(old) == ('locked' if outcome == 'denied' else None)
    renamed = outcome == 'success'
    assert ctx.get_stat('renamed_by_tags') == int(renamed)
    assert old.exists() == (not renamed)
    assert new.exists() == renamed
    assert (new if renamed else old).read_bytes() == b'original'
    assert neighbor.read_bytes() == b'neighbor'


@windows
@pytest.mark.parametrize('dry_run', [False, True])
def test_tag_rename_applies_mismatches_in_input_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    individual = tmp_path / 'Artist - Original.mp3'
    safe = tmp_path / 'Matching.mp3'
    neighbor = tmp_path / 'Artist - Unchanged.mp3'
    group = tmp_path / 'Without separator.mp3'
    files = [individual, safe, neighbor, group]
    expected = [
        tmp_path / 'Artist - Corrected.mp3',
        tmp_path / 'Artist - Matching.mp3',
        neighbor,
        tmp_path / 'Artist - Grouped.mp3',
    ]
    ctx, repo, ui = SessionContext(dry_run, False), MemoryRepository(), Interactor()
    monkeypatch.setattr(ui, 'prompt', Mock(side_effect=['', 'y', 't']))
    titles = ['Corrected', 'Matching', 'Unchanged', 'Grouped']
    ids = []
    for source, title in zip(files, titles, strict=True):
        source.write_bytes(title.encode())
        repo.simulate_update_tags(source, 'Artist', title)
        ids.append(ctx.registry.register(source).id)

    assert stages.TagRenamingStage().execute(files, ctx, repo, ui) == expected
    assert [ctx.registry.id_for(path) for path in expected] == ids
    assert [ctx.registry.path_of(file_id) for file_id in ids] == expected
    assert ctx.get_stat('renamed_by_tags') == (0 if dry_run else 3)
    for source, target, title in zip(files, expected, titles, strict=True):
        assert (source if dry_run else target).read_bytes() == title.encode()
        if source != target:
            assert not (target if dry_run else source).exists()
            assert ctx.registry.id_for(source) is None


@windows
@pytest.mark.parametrize('outcome', ['conflict', 'denied', 'blocked'])
def test_tag_mismatch_keeps_original_path_on_rename_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    source = tmp_path / 'Without separator.mp3'
    target = tmp_path / 'Artist - Corrected.mp3'
    source.write_bytes(b'original')
    ctx, repo, ui = SessionContext(False, False), MemoryRepository(), Interactor('y')
    repo.simulate_update_tags(source, 'Artist', 'Corrected')
    file_id = ctx.registry.register(source).id
    files = [source]
    if outcome == 'conflict':
        target.write_bytes(b'occupied')
        files.append(target)
        target_id = ctx.registry.register(target).id
        reason = 'конфликт целевого имени; оба файла сохранены'
    elif outcome == 'denied':
        reason = 'locked'
        monkeypatch.setattr(Path, 'rename', Mock(side_effect=PermissionError(reason)))
    else:
        reason = 'ранее заблокирован'
        ctx.block_path(source, reason)

    assert stages.TagRenamingStage().execute(files, ctx, repo, ui) == files
    assert ctx.registry.path_of(file_id) == source
    assert ctx.registry.id_for(source) == file_id
    assert ctx.get_stat('renamed_by_tags') == 0
    assert ctx.get_block_reason(source) == reason
    assert source.read_bytes() == b'original'
    if outcome == 'conflict':
        assert target.read_bytes() == b'occupied'
        assert ctx.registry.id_for(target) == target_id
    else:
        assert not target.exists()
        assert ctx.get_block_reason(target) == (reason if outcome == 'denied' else None)


@windows
@pytest.mark.parametrize('answer', ['', 'y', 'yes'])
def test_tag_rename_conflict_keeps_both(tmp_path: Path, answer: str) -> None:
    old, new = tmp_path / 'Title.mp3', tmp_path / 'Artist - Title.mp3'
    old.write_bytes(b'old')
    new.write_bytes(b'new target')
    repo, ctx, ui = MemoryRepository(), SessionContext(False, False), Interactor(answer)
    repo.simulate_update_tags(old, 'Artist', 'Title')
    repo.simulate_update_tags(new, 'Artist', 'Title')
    assert stages.TagRenamingStage()._rename_by_tags([old, new], ctx, repo, ui) == [
        old,
        new,
    ]
    assert old.read_bytes() == b'old' and new.read_bytes() == b'new target'
    assert ctx.is_path_blocked(old) and not ctx.get_stat('renamed_by_tags')
    assert 'Объединение отключено' in '\n'.join(ui.messages)
    assert not any('Объединить' in prompt for prompt in ui.prompts)


@pytest.mark.parametrize('dry_run', [False, True])
def test_duplicate_stage_preserves_files_and_cache(
    tmp_path: Path, dry_run: bool
) -> None:
    files = [tmp_path / 'one.mp3', tmp_path / 'two.mp3']
    repo, ctx, ui = (
        MemoryRepository(),
        SessionContext(dry_run, False),
        Interactor('yes'),
    )
    for path, content in zip(files, [b'A', b'BBBB'], strict=True):
        path.write_bytes(content)
        repo.simulate_update_tags(path, 'Artist', 'Title')
    assert stages.DuplicateResolutionStage().execute(files, ctx, repo, ui) == files
    assert [path.read_bytes() for path in files] == [b'A', b'BBBB']
    assert all(repo.get_tags(path).complete_tags is not None for path in files)
    assert not ctx.get_stat('duplicates_removed') and not ui.prompts
    output = '\n'.join(ui.messages)
    assert 'нормализованные исполнитель и название из тегов' in output
    assert 'Байты различаются. Совпадение музыкальной записи не установлено' in output
    assert 'Недостаточно данных: формат, кодек' in output


@windows
@pytest.mark.parametrize('write_succeeds', [False, True])
def test_tag_writing_before_failed_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write_succeeds: bool
) -> None:
    path = tmp_path / 'Artist - Title (vksaver).mp3'
    path.write_bytes(b'original')
    repo, ctx, ui = MemoryRepository(), SessionContext(False, False), Interactor()
    repo.succeed = write_succeeds
    rename = Mock(side_effect=PermissionError('locked'))
    monkeypatch.setattr(Path, 'rename', rename)
    assert stages.TagWritingStage().execute([path], ctx, repo, ui) == [path]
    assert len(repo.writes) == 1
    assert rename.call_count == int(write_succeeds)
    assert ctx.get_stat('tags_written') == int(write_succeeds)
    assert ctx.get_stat('renamed_by_tags') == 0 and ctx.is_path_blocked(path)
    assert repo.get_tags(path).complete_tags == (
        AudioTags('Artist', 'Title') if write_succeeds else None
    )
    assert not path.with_name('Artist - Title.mp3').exists()
    assert path.read_bytes() == b'original'


@windows
def test_late_stages_keep_input_order_and_identity_without_rescan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'artist - Title (vksaver).mp3'
    intermediate = tmp_path / 'artist - Title.mp3'
    final = tmp_path / 'Artist - Title.mp3'
    neighbor = tmp_path / 'Artist - One.mp3'
    outsider = tmp_path / 'Untracked.mp3'
    for path in (source, neighbor, outsider):
        path.write_bytes((b'\xff\xfb\x90\x64' + bytes(413)) * 5)
    repo = TagRepository()
    assert repo.update_tags(neighbor, 'Artist', 'One')
    ctx, ui = SessionContext(False, False, target_dir=tmp_path), Interactor()
    variations = Mock(wraps=stages.find_artist_variations)
    monkeypatch.setattr(stages, 'find_artist_variations', variations)

    pipeline = AudioProcessingPipeline(
        [
            stages.TagWritingStage(),
            stages.ArtistNormalizationStage(),
            stages.ManualInputStage(),
        ]
    )
    assert pipeline.run([neighbor, source, source], ctx, repo, ui) == [neighbor, final]
    variations.assert_called_once_with([neighbor, intermediate], repo)
    file_id = ctx.registry.track(source).id
    assert ctx.registry.id_for(final) == file_id
    assert ctx.registry.id_for(outsider) is None
    assert ctx.registry.current_paths() == [neighbor, final]
    assert stages.TagWritingStage().execute([source, final, source], ctx, repo, ui) == [
        final
    ]
    assert ctx.registry.current_paths() == [neighbor, final]
    assert repo.read_tags_uncached(final).tags == AudioTags('Artist', 'Title')
    assert ctx.get_stat('tags_written') == ctx.get_stat('artists_normalized') == 1
    assert ctx.get_stat('renamed_by_tags') == 2


def test_missing_file_uses_model_path_and_disappeared_file_stays_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / 'Track  Name.mp3'
    current = tmp_path / 'Track Name.mp3'
    final = tmp_path / 'Artist - Track Name.mp3'
    gone = tmp_path / 'Gone.mp3'
    original.write_bytes(b'original')
    gone.touch()
    ctx, repo, ui = SessionContext(True, False), MemoryRepository(), Interactor()
    assert stages.TagRenamingStage().execute([original, gone], ctx, repo, ui) == [
        original,
        gone,
    ]
    original_id, gone_id = ctx.file_ids([original, gone])
    gone.unlink()
    assert ctx.present_path(gone_id) is None
    gone.touch()

    def model_rename(
        old: Path, new: Path, _: SessionContext, tags: TagRepository, ui: UserInteractor
    ) -> tuple[str, Path]:
        simulate_rename(old, new, tags, ui)
        return 'planned', new

    monkeypatch.setattr(stages, '_file_executor', FileExecutor(model_rename))
    monkeypatch.setattr('file_operations.modification_allowed', Mock(return_value=True))
    monkeypatch.setattr(stages, 'modification_allowed', Mock(return_value=True))
    assert stages.NormalizationStage().execute([original, gone], ctx, repo, ui) == [
        current
    ]
    assert ctx.missing_files == [current]
    monkeypatch.setattr(ui, 'prompt', Mock(side_effect=['', 'Artist']))
    assert stages.ManualInputStage().execute(
        [original, current, gone], ctx, repo, ui
    ) == [final]
    assert ctx.missing_files == [final]
    assert ctx.registry.id_for(final) == original_id
    assert not repo.writes and not ctx.get_stat('manual_input')
    assert (
        original.read_bytes() == b'original'
        and not current.exists()
        and not final.exists()
    )


def test_modeled_tag_write_after_rename_uses_current_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / 'Artist - Title (vksaver).mp3'
    current = tmp_path / 'Artist - Title.mp3'
    original.write_bytes(b'original')
    ctx, repo, ui = SessionContext(True, False), MemoryRepository(), Interactor()
    guard = Mock(return_value=True)
    monkeypatch.setattr('file_operations.modification_allowed', guard)

    def plan_rename(
        old: Path, new: Path, _: SessionContext, tags: TagRepository, ui: UserInteractor
    ) -> tuple[str, Path]:
        simulate_rename(old, new, tags, ui)
        return 'planned', new

    executor = FileExecutor(plan_rename)
    assert executor.rename(original, current, ctx, repo, ui).is_model_change
    assert executor.write_tags(
        original, 'Artist', 'Title', ctx, repo, ui
    ).is_model_change
    assert (
        executor.write_tags(current, 'Artist', 'Title', ctx, repo, ui).status
        is OperationStatus.NOOP
    )
    assert ctx.registry.current_paths() == [current]
    assert ctx.registry.track(original).id == ctx.registry.id_for(current)
    assert repo.get_tags(current).complete_tags == AudioTags('Artist', 'Title')
    assert all(call.args[0] == original for call in guard.call_args_list)
    assert (
        not repo.writes
        and original.read_bytes() == b'original'
        and not current.exists()
    )


@pytest.mark.parametrize('uncertain', [False, True])
def test_unconfirmed_tag_write_blocks_dependent_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uncertain: bool
) -> None:
    source = tmp_path / 'Artist - Title (vksaver).mp3'
    source.write_bytes(b'original')
    ctx, repo, ui = SessionContext(False, False), TagRepository(), Interactor()
    audio = Mock(tags={}, info=None)
    audio.save.side_effect = OSError('save failed')
    write_input = audio if uncertain else OSError('open failed')
    empty_audio = Mock(tags={}, info=None)
    parser_calls = [empty_audio, write_input]
    if uncertain:
        parser_calls.append(empty_audio)
    parser_calls.append(write_input)
    if uncertain:
        parser_calls.append(empty_audio)
    mp3 = Mock(side_effect=parser_calls)
    monkeypatch.setattr('tag_repository.MP3', mp3)
    monkeypatch.setattr('file_operations.modification_allowed', Mock(return_value=True))
    journal = Mock()
    monkeypatch.setattr('tag_repository.log_operation', journal)
    rename = Mock()
    executor = FileExecutor(rename)
    monkeypatch.setattr(stages, '_file_executor', executor)
    write_tags = Mock(wraps=executor.write_tags)
    monkeypatch.setattr(executor, 'write_tags', write_tags)

    assert stages.TagWritingStage().execute([source, source], ctx, repo, ui) == [source]
    assert write_tags.call_count == journal.call_count == 1
    # Та же ошибка на входе исполнителя возвращает типизированный исход.
    direct_ctx = SessionContext(False, False)
    outcome = executor.write_tags(source, 'Artist', 'Title', direct_ctx, repo, ui)
    assert outcome.status is (
        OperationStatus.UNCERTAIN if uncertain else OperationStatus.ERROR
    )
    assert audio.save.call_count == 2 * int(uncertain)
    assert not rename.called and ctx.is_path_blocked(source)
    assert (
        repo.get_tags(source).status is ReadStatus.EMPTY
        and source.read_bytes() == b'original'
    )
    assert ctx.get_stat('tags_written') == ctx.get_stat('renamed_by_tags') == 0


@windows
@pytest.mark.parametrize('blocked_before', [False, True])
def test_artist_write_failure_blocks_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked_before: bool
) -> None:
    canonical, variant = tmp_path / 'Artist - One.mp3', tmp_path / 'artist - Two.mp3'
    canonical.write_bytes(b'one')
    variant.write_bytes(b'two')
    repo = MemoryRepository()
    repo.simulate_update_tags(canonical, 'Artist', 'One')
    repo.simulate_update_tags(variant, 'artist', 'Two')
    repo.succeed = False
    ctx, ui = SessionContext(False, False, target_dir=tmp_path), Interactor()
    if blocked_before:
        ctx.block_path(variant, '')
    rename = Mock(side_effect=AssertionError('dependent rename'))
    monkeypatch.setattr(Path, 'rename', rename)
    stages.ArtistNormalizationStage().execute([canonical, variant], ctx, repo, ui)
    assert len(repo.writes) == (0 if blocked_before else 1)
    assert not rename.called and ctx.is_path_blocked(variant)
    assert canonical.read_bytes() == b'one' and variant.read_bytes() == b'two'


def test_manual_stage_keeps_blocked_missing_file(tmp_path: Path) -> None:
    path = tmp_path / 'Track.mp3'
    path.write_bytes(b'original')
    ctx = SessionContext(False, False, target_dir=tmp_path)
    ctx.missing_files = [path]
    ctx.block_path(path, '')
    ui, repo = Interactor(), MemoryRepository()
    stages.ManualInputStage().execute([path], ctx, repo, ui)
    assert ctx.missing_files == [path]
    assert not any('Введите исполнителя' in prompt for prompt in ui.prompts)
    assert not repo.writes and path.read_bytes() == b'original'


@windows
@pytest.mark.parametrize('dry_run', [False, True])
def test_tag_writing_rejects_hardlink(tmp_path: Path, dry_run: bool) -> None:
    path = tmp_path / 'Artist - Title.mp3'
    path.write_bytes(b'original')
    alias = tmp_path / 'alias.mp3'
    alias.hardlink_to(path)
    repo, ctx = MemoryRepository(), SessionContext(dry_run, False)
    stages.TagWritingStage().execute([path], ctx, repo, Interactor())
    assert not repo.writes and repo.get_tags(path).complete_tags is None
    assert ctx.is_path_blocked(path)
    assert path.read_bytes() == alias.read_bytes() == b'original'


@pytest.mark.skipif(
    os.name == 'nt' or not str(Path.cwd()).startswith('/mnt/'),
    reason='Требуется запуск из WSL на /mnt/...',
)
@pytest.mark.parametrize('dry_run', [False, True])
def test_wsl_mounted_tag_write_refused(dry_run: bool) -> None:
    # Только созданный этим тестом временный каталог на смонтированном томе.
    with TemporaryDirectory(prefix='.issue9-test-', dir=Path.cwd()) as directory:
        path = Path(directory) / 'Artist - Title.mp3'
        path.write_bytes(b'original')
        repo, ctx, ui = MemoryRepository(), SessionContext(dry_run, False), Interactor()
        stages.TagWritingStage().execute([path], ctx, repo, ui)
        assert ctx.is_path_blocked(path)
        assert not repo.writes and repo.get_tags(path).complete_tags is None
        assert not ctx.get_stat('tags_written')
        assert 'Модификация на монтированных файловых системах WSL' in '\n'.join(
            ui.messages
        )
        assert path.read_bytes() == b'original'
