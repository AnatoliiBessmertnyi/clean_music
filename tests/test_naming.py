"""Тесты для функций работы с именами."""

from naming import (
    build_safe_filename,
    normalize_dash,
    normalize_title_spacing,
    remove_junk,
)


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
