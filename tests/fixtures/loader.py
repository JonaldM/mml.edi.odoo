"""Fixture file loader for EDI parser tests."""
import os

FIXTURES_DIR = os.path.dirname(__file__)


def load_fixture(filename: str) -> bytes:
    """Load a fixture file as bytes."""
    path = os.path.join(FIXTURES_DIR, filename)
    with open(path, "rb") as f:
        return f.read()
