import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core import database


@pytest.fixture
def temporary_database(tmp_path, monkeypatch):
    database_path = tmp_path / "test_sessions.db"

    engine = create_engine(
        f"sqlite:///{database_path.as_posix()}"
    )

    session_factory = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=engine,
    )

    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE session_probe ("
                "id INTEGER PRIMARY KEY, "
                "value TEXT NOT NULL"
                ")"
            )
        )

    monkeypatch.setattr(
        database,
        "SessionLocal",
        session_factory,
    )

    try:
        yield engine
    finally:
        engine.dispose()


def test_successful_session_commits(temporary_database):
    with database.get_db_session() as db:
        db.execute(
            text(
                "INSERT INTO session_probe (id, value) "
                "VALUES (:id, :value)"
            ),
            {"id": 1, "value": "saved"},
        )

    with temporary_database.connect() as connection:
        value = connection.execute(
            text(
                "SELECT value FROM session_probe "
                "WHERE id = 1"
            )
        ).scalar_one()

    assert value == "saved"


def test_failed_session_rolls_back(temporary_database):
    with pytest.raises(
        RuntimeError,
        match="simulated failure",
    ):
        with database.get_db_session() as db:
            db.execute(
                text(
                    "INSERT INTO session_probe (id, value) "
                    "VALUES (:id, :value)"
                ),
                {"id": 1, "value": "must not remain"},
            )

            raise RuntimeError("simulated failure")

    with temporary_database.connect() as connection:
        count = connection.execute(
            text("SELECT COUNT(*) FROM session_probe")
        ).scalar_one()

    assert count == 0


def test_get_db_does_not_commit_implicitly(temporary_database):
    dependency = database.get_db()
    db = next(dependency)

    try:
        db.execute(
            text(
                "INSERT INTO session_probe (id, value) "
                "VALUES (:id, :value)"
            ),
            {"id": 1, "value": "uncommitted"},
        )
    finally:
        dependency.close()

    with temporary_database.connect() as connection:
        count = connection.execute(
            text("SELECT COUNT(*) FROM session_probe")
        ).scalar_one()

    assert count == 0