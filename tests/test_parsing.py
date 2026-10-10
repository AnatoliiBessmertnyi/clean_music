"""Тесты для функций парсинга."""

from parsing import (
    extract_title_from_filename,
    is_track_number,
    parse_artist_title,
)


class TestIsTrackNumber:
    """Тесты для is_track_number."""

    def test_simple_number(self) -> None:
        assert is_track_number('01') is True

    def test_number_with_dot(self) -> None:
        assert is_track_number('1.') is True

    def test_track_word(self) -> None:
        assert is_track_number('Track 5') is True

    def test_not_track_number(self) -> None:
        assert is_track_number('Title') is False

    def test_three_digits(self) -> None:
        assert is_track_number('123') is True


class TestParseArtistTitle:
    """Тесты для parse_artist_title."""

    def test_basic_format(self) -> None:
        result = parse_artist_title('Artist - Title')
        assert result == ('Artist', 'Title')

    def test_with_spaces(self) -> None:
        result = parse_artist_title('Artist Name - Song Title')
        assert result == ('Artist Name', 'Song Title')

    def test_track_number_first(self) -> None:
        result = parse_artist_title('01 - Title')
        assert result is None

    def test_no_dash(self) -> None:
        result = parse_artist_title('Artist Title')
        assert result is None

    def test_en_dash(self) -> None:
        result = parse_artist_title('Artist – Title')
        assert result == ('Artist', 'Title')

    def test_em_dash(self) -> None:
        result = parse_artist_title('Artist — Title')
        assert result == ('Artist', 'Title')


class TestExtractTitleFromFilename:
    """Тесты для extract_title_from_filename."""

    def test_with_track_number(self) -> None:
        result = extract_title_from_filename('01. Title')
        assert result == 'Title'

    def test_with_track_word(self) -> None:
        result = extract_title_from_filename('Track 5. Title')
        assert result == 'Title'

    def test_without_track_number(self) -> None:
        result = extract_title_from_filename('Artist - Title')
        assert result == 'Artist - Title'

    def test_dash_separator(self) -> None:
        result = extract_title_from_filename('01 - Title')
        assert result == 'Title'
