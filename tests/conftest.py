import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core import database
from app.models.database import Base


@pytest.fixture(autouse=True)
def isolated_document_database(tmp_path, monkeypatch):
    """Every test gets its own database, never the developer's real DB."""
    from app.core.config import settings
    storage_path = str(tmp_path / "private-objects")
    monkeypatch.setattr(settings, "storage_type", "local")
    monkeypatch.setattr(settings, "local_storage_path", storage_path)
    monkeypatch.setenv("STORAGE_TYPE", "local")
    monkeypatch.setenv("LOCAL_STORAGE_PATH", storage_path)
    path = tmp_path / "api_documents.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    try:
        yield factory
    finally:
        engine.dispose()


def pytest_configure(config):
    config.addinivalue_line("markers", "auth_live: exercise real login, cookies, CSRF and tenant access")


@pytest.fixture(autouse=True)
def authenticated_workflow_actor(request):
    from app.main import app
    from tests.auth_helpers import override_authenticated_api
    old = app.dependency_overrides.copy()
    if request.node.get_closest_marker("auth_live") is None:
        override_authenticated_api(app)
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old)
