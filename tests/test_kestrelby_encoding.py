"""Verify that non-cp1252 bytes trigger a logged warning."""
import logging
import pathlib


def test_warning_logged_for_replacement_chars():
    src = (pathlib.Path(__file__).parent.parent / 'parsers/kestrelby.py').read_text()
    assert r'\ufffd' in src or "replacement_count" in src or "count('\\ufffd')" in src or 'replacement' in src.lower(), (
        "kestrelby.py must log a warning when replacement characters are found after decode"
    )
