from sqlalchemy import create_engine, inspect

from app.models.database import Base


def test_database_models_create_expected_tables():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:"
    )

    Base.metadata.create_all(engine)

    inspector = inspect(engine)

    table_names = set(
        inspector.get_table_names()
    )

    expected_tables = {
        "companies",
        "users",
        "qbo_connections",
        "documents",
        "extracted_data",
        "line_items",
        "extracted_transactions",
        "audit_logs",
    }

    assert expected_tables.issubset(
        table_names
    )