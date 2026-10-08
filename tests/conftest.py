import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core import database
from app.models.database import Base


@pytest.fixture(autouse=True)
def isolated_document_database(tmp_path, monkeypatch):
    """Every test gets its own database, never the developer's real DB."""
    path = tmp_path / "api_documents.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    try:
        yield factory
    finally:
        engine.dispose()
