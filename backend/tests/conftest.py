import pytest

from app.db import Database


@pytest.fixture
def db(tmp_path):
    """Frische, leere Datenbank pro Test in einem temporären Ordner."""
    database = Database(tmp_path / "test.db")
    database.init_schema()
    return database
