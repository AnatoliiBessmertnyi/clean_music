"""Тесты для функций работы с именами."""

import pytest

from naming import (
    build_safe_filename,
    normalize_dash,
    normalize_title_spacing,
    qualifier_name_candidate,
    remove_junk,
)
from strategies import BilingualTitleStrategy


@pytest.mark.parametrize('source', ['Part_Other-End', 'Part-Work.End', 'Part-Work End'])
def test_windows_form_requires_literal_text_outside_replacements(source: str) -> None:
    proposed, offer_choice = qualifier_name_candidate(
        'Title (Part-Work;End)', f'Title ({source})', 'Title (Part:Work;End)'
    )
    assert not offer_choice
    assert proposed == f'Title (Part-Work;End) ({source})'


class TestNormalizeTitleSpacing:
    """Тесты для normalize_title_spacing."""

    def test_space_before_bracket(self) -> None:
        assert normalize_title_spacing('Waiting(feat. X)') == 'Waiting (feat. X)'

    def test_multiple_spaces_before_bracket(self) -> None:
        assert normalize_title_spacing('Title  (feat. X)') == 'Title (feat. X)'

    def test_spaces_inside_brackets(self) -> None:
        assert normalize_title_spacing('Title ( feat. X )') == 'Title (feat. X)'

    def test_square_brackets(self) -> None:
        assert normalize_title_spacing('Title[remix]') == 'Title [remix]'

    def test_already_correct(self) -> None:
        assert normalize_title_spacing('Title (feat. X)') == 'Title (feat. X)'


class TestNormalizeDash:
    """Тесты для normalize_dash."""

    def test_en_dash(self) -> None:
        assert normalize_dash('Artist – Title') == 'Artist - Title'

    def test_em_dash(self) -> None:
        assert normalize_dash('Artist — Title') == 'Artist - Title'

    def test_regular_dash(self) -> None:
        assert normalize_dash('Artist - Title') == 'Artist - Title'


class TestRemoveJunk:
    """Тесты для remove_junk."""

    def test_vksaver(self) -> None:
        assert remove_junk('Artist - Title (vksaver)') == 'Artist - Title'

    def test_muzmo(self) -> None:
        assert remove_junk('Artist - Title [muzmo.ru]') == 'Artist - Title'

    def test_no_junk(self) -> None:
        assert remove_junk('Artist - Title') == 'Artist - Title'

    def test_multiple_junk(self) -> None:
        result = remove_junk('Artist - Title (vksaver) [zaycev.net]')
        assert result == 'Artist - Title'


class TestBuildSafeFilename:
    """Тесты для build_safe_filename."""

    def test_basic(self) -> None:
        assert build_safe_filename('Artist', 'Title', '.mp3') == 'Artist - Title.mp3'

    def test_invalid_chars_removed(self) -> None:
        result = build_safe_filename('AC/DC', 'Back in Black', '.mp3')
        assert result == 'ACDC - Back in Black.mp3'

    def test_colon_removed(self) -> None:
        result = build_safe_filename('Artist', 'Title: Subtitle', '.mp3')
        assert result == 'Artist - Title Subtitle.mp3'

    def test_question_mark_removed(self) -> None:
        result = build_safe_filename('Artist', 'What?', '.mp3')
        assert result == 'Artist - What.mp3'

    def test_feat_normalization(self) -> None:
        result = build_safe_filename('Artist', 'Title (feat. Guest)', '.mp3')
        assert result == 'Artist - Title (feat. Guest).mp3'

    def test_multiple_spaces_normalized(self) -> None:
        result = build_safe_filename('Artist', 'Title   (feat. Guest)', '.mp3')
        assert result == 'Artist - Title (feat. Guest).mp3'


@pytest.mark.parametrize('group', ['(w/o Guest)', '[w/o Guest]', '(feat. w/o Guest)'])
@pytest.mark.parametrize('first', [True, False])
def test_bilingual_title_only_splits_outside_groups(group: str, first: bool) -> None:
    strategy = BilingualTitleStrategy(first)
    safe = group.replace('/', '_')
    assert strategy.apply(f'Artist - Title {group}', {'/'}) == f'Artist - Title {safe}'
    chosen = 'English' if first else 'Second'
    assert (
        strategy.apply(f'Artist - English / Second {group}', {'/'})
        == f'Artist - {chosen} {safe}'
    )
    assert strategy.apply('Artist - English / Second', {'/'}) == f'Artist - {chosen}'
    assert strategy.apply('Artist - English / (Second)', {'/'}) == (
        'Artist - English' if first else 'Artist - (Second)'
    )
    # An internal slash before the separator must not select the wrong boundary.
    assert strategy.apply(f'Artist - English {group} / Second', {'/'}) == (
        f'Artist - English {safe}' if first else 'Artist - Second'
    )


def test_build_name_sanitizes_qualifier_before_matching() -> None:
    assert (
        build_safe_filename(
            'Artist',
            'Title (w/o Guest)',
            '.mp3',
            source_stem='Artist - Title (w_o Guest)',
        )
        == 'Artist - Title (w_o Guest).mp3'
    )
