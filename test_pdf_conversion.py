from pathlib import Path

from dotenv import load_dotenv

from app.services.extraction.extractor import DocumentExtractor


load_dotenv()

pdf_path = Path("test-data/test_invoice.pdf")

pdf_data = pdf_path.read_bytes()

extractor = DocumentExtractor(
    api_key="not-used-for-pdf-conversion"
)

pages = extractor.pdf_to_page_images(pdf_data)

print(f"Page count: {len(pages)}")

for index, page in enumerate(pages, start=1):
    print(
        f"Page {index}: "
        f"{len(page)} bytes"
    )