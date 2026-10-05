import pytest

from app.api.documents import documents_db


@pytest.fixture(autouse=True)
def clear_documents_db():
    documents_db.clear()

    yield

    documents_db.clear()