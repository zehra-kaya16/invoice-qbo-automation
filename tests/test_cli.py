from types import SimpleNamespace

import cli
from app.schemas.documents import (
    DocumentType,
)


class FakeResult:
    def model_dump(self):
        return {
            "vendor": "Test Vendor",
            "amount": "100.00",
        }


class FakeExtractor:
    def __init__(self):
        self.bank_statement_call = None

    def classify_document(
        self,
        image_data,
    ):
        return DocumentType.INVOICE

    def extract_receipt(
        self,
        image_data,
    ):
        return FakeResult()

    def extract_bank_statement(
        self,
        page_images,
        document_id,
    ):
        self.bank_statement_call = {
            "page_images": page_images,
            "document_id": document_id,
        }

        return FakeResult()

    def extract_check(
        self,
        image_data,
    ):
        return FakeResult()


def test_cli_extract_invoice(
    tmp_path,
    monkeypatch,
):
    file_path = (
        tmp_path / "invoice.jpg"
    )

    file_path.write_bytes(
        b"fake-image"
    )

    extractor = FakeExtractor()

    monkeypatch.setattr(
        cli,
        "get_extractor",
        lambda: extractor,
    )

    args = SimpleNamespace(
        file=str(file_path),
        type="invoice",
        json=True,
    )

    cli.cmd_extract(args)


def test_cli_bank_statement_supplies_document_id(
    tmp_path,
    monkeypatch,
):
    file_path = (
        tmp_path / "statement.jpg"
    )

    file_path.write_bytes(
        b"fake-statement"
    )

    extractor = FakeExtractor()

    monkeypatch.setattr(
        cli,
        "get_extractor",
        lambda: extractor,
    )

    args = SimpleNamespace(
        file=str(file_path),
        type="bank_statement",
        json=True,
    )

    cli.cmd_extract(args)

    call = (
        extractor.bank_statement_call
    )

    assert call is not None

    assert call["page_images"] == [
        b"fake-statement"
    ]

    assert call[
        "document_id"
    ].startswith("cli_")

def test_cli_uses_anthropic_when_openai_key_missing(
    monkeypatch,
):
    monkeypatch.delenv(
        "OPENAI_API_KEY",
        raising=False,
    )

    monkeypatch.setenv(
        "ANTHROPIC_API_KEY",
        "test-anthropic-key",
    )

    captured = {}

    class FakeExtractor:
        def __init__(
            self,
            api_key,
            provider,
        ):
            captured["api_key"] = api_key
            captured["provider"] = provider

    monkeypatch.setattr(
        "app.services.extraction.extractor.DocumentExtractor",
        FakeExtractor,
    )

    cli.get_extractor()

    assert captured == {
        "api_key": "test-anthropic-key",
        "provider": "anthropic",
    }