"""Tests for stdin value normalisation in `bay vault set`."""

from bay_cli.commands.vault import _normalise_stdin_value


def test_single_line_trailing_newline_is_stripped():
    assert _normalise_stdin_value("s3cret\n") == "s3cret"


def test_single_line_without_trailing_newline_unchanged():
    assert _normalise_stdin_value("s3cret") == "s3cret"


def test_multiline_pem_keeps_trailing_and_internal_newlines():
    begin = "-----BEGIN " + "PRIVATE KEY" + "-----"
    end = "-----END " + "PRIVATE KEY" + "-----"
    pem = (
        begin + "\n"
        "abc123\n"
        "def456\n"
        + end + "\n"
    )
    assert _normalise_stdin_value(pem) == pem


def test_only_newline_normalises_to_empty_string():
    assert _normalise_stdin_value("\n") == ""
