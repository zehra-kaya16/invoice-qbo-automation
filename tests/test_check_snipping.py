import io

from PIL import Image

from app.services.extraction.extractor import (
    DocumentExtractor,
)


def make_page() -> bytes:
    image = Image.new(
        "RGB",
        (1000, 1000),
        "white",
    )

    buffer = io.BytesIO()

    image.save(
        buffer,
        format="PNG",
    )

    return buffer.getvalue()


def test_no_check_regions_returns_empty(
    monkeypatch,
    tmp_path,
):
    extractor = DocumentExtractor(
        api_key="test"
    )

    monkeypatch.setattr(
        extractor,
        "_detect_check_regions",
        lambda image: [],
    )

    result = (
        extractor.snip_checks_from_statement(
            page_images=[make_page()],
            document_id="doc_test",
        )
    )

    assert result == []

def test_check_region_is_snipped(
    monkeypatch,
    tmp_path,
):
    extractor = DocumentExtractor(
        api_key="test"
    )

    monkeypatch.chdir(
        tmp_path
    )

    monkeypatch.setattr(
        extractor,
        "_detect_check_regions",
        lambda image: [
            {
                "x1": 100,
                "y1": 200,
                "x2": 900,
                "y2": 600,
            }
        ],
    )

    monkeypatch.setattr(
        extractor,
        "_extract_check_fields",
        lambda image: {
            "check_number": "1001",
            "payee": "Books by Bessie",
            "amount": 550.00,
            "date": "2026-09-15",
            "memo": "Test check",
        },
    )

    result = (
        extractor.snip_checks_from_statement(
            page_images=[make_page()],
            document_id="doc_test",
        )
    )

    assert len(result) == 1

    check = result[0]

    assert check.check_number == "1001"
    assert check.payee == "Books by Bessie"
    assert float(check.amount) == 550.00
    assert check.memo == "Test check"

    assert (
        tmp_path
        / check.image_path
    ).exists()