import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app


@pytest.fixture
def db(tmp_path):
    """Frische, leere Datenbank pro Test in einem temporären Ordner."""
    database = Database(tmp_path / "test.db")
    database.init_schema()
    return database


@pytest.fixture
def client(db):
    """API gegen die Test-Datenbank, ohne MQTT-Verbindung."""
    app = create_app(Settings(db_path=db.pfad), start_mqtt=False)
    with TestClient(app) as c:   # "with" führt den lifespan aus
        yield c
