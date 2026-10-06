#!/usr/bin/env python3

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


def get_extractor():
    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        print(
            "Error: OPENAI_API_KEY environment variable is not set."
        )
        sys.exit(1)

    from app.services.extraction.extractor import (
        DocumentExtractor,
    )

    return DocumentExtractor(
        api_key=api_key
    )


def load_document_pages(
    extractor,
    file_path: Path,
) -> list[bytes]:
    content = file_path.read_bytes()

    if file_path.suffix.lower() == ".pdf":
        pages = extractor.pdf_to_page_images(
            content
        )

        if not pages:
            raise ValueError(
                "PDF contains no readable pages."
            )

        return pages

    return [content]


def cmd_classify(args):
    file_path = Path(args.file)

    if not file_path.exists():
        print(
            f"Error: File not found: {file_path}"
        )
        sys.exit(1)

    extractor = get_extractor()

    pages = load_document_pages(
        extractor,
        file_path,
    )

    print(
        f"Classifying: {file_path.name}"
    )

    document_type = (
        extractor.classify_document(
            pages[0]
        )
    )

    print(
        f"\nDocument Type: "
        f"{document_type.value}"
    )


def cmd_extract(args):
    file_path = Path(args.file)

    if not file_path.exists():
        print(
            f"Error: File not found: {file_path}"
        )
        sys.exit(1)

    extractor = get_extractor()

    pages = load_document_pages(
        extractor,
        file_path,
    )

    from app.schemas.documents import (
        DocumentType,
    )

    if args.type:
        document_type = DocumentType(
            args.type
        )
    else:
        print(
            f"Classifying: {file_path.name}"
        )

        document_type = (
            extractor.classify_document(
                pages[0]
            )
        )

        print(
            f"Detected type: "
            f"{document_type.value}"
        )

    print("\nExtracting data...")

    if document_type in (
        DocumentType.RECEIPT,
        DocumentType.INVOICE,
        DocumentType.BILL,
    ):
        if len(pages) > 1:
            raise ValueError(
                "Multi-page receipt/invoice PDF "
                "extraction is not implemented yet."
            )

        result = extractor.extract_receipt(
            pages[0]
        )

    elif (
        document_type
        == DocumentType.BANK_STATEMENT
    ):
        result = (
            extractor.extract_bank_statement(
                page_images=pages,
                document_id=(
                    f"cli_{uuid.uuid4().hex[:12]}"
                ),
            )
        )

    elif (
        document_type
        == DocumentType.CHECK
    ):
        result = extractor.extract_check(
            pages[0]
        )

    else:
        print(
            "Unsupported document type: "
            f"{document_type.value}"
        )
        sys.exit(1)

    data = result.model_dump()

    if args.json:
        print(
            json.dumps(
                data,
                indent=2,
                default=str,
            )
        )
        return

    print("\n" + "=" * 50)
    print("EXTRACTED DATA")
    print("=" * 50)

    for key, value in data.items():
        if value is None:
            continue

        if key == "raw_text":
            continue

        if isinstance(value, list):
            print(f"\n{key}:")

            for item in value[:10]:
                print(
                    f"  - {item}"
                )

            if len(value) > 10:
                print(
                    "  ... and "
                    f"{len(value) - 10} more"
                )

        else:
            print(
                f"{key}: {value}"
            )


def cmd_server(args):
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Receipt AI - Document processor "
            "for QuickBooks Online"
        )
    )

    subparsers = parser.add_subparsers(
        dest="command",
        help="Commands",
    )

    classify_parser = (
        subparsers.add_parser(
            "classify",
            help="Classify a document",
        )
    )

    classify_parser.add_argument(
        "file",
        help="Path to document file",
    )

    classify_parser.set_defaults(
        func=cmd_classify
    )

    extract_parser = (
        subparsers.add_parser(
            "extract",
            help="Extract data from a document",
        )
    )

    extract_parser.add_argument(
        "file",
        help="Path to document file",
    )

    extract_parser.add_argument(
        "--type",
        "-t",
        choices=[
            "receipt",
            "invoice",
            "bill",
            "bank_statement",
            "check",
        ],
        help=(
            "Document type "
            "(auto-detected if omitted)"
        ),
    )

    extract_parser.add_argument(
        "--json",
        "-j",
        action="store_true",
        help="Output as JSON",
    )

    extract_parser.set_defaults(
        func=cmd_extract
    )

    server_parser = (
        subparsers.add_parser(
            "server",
            help="Run the API server",
        )
    )

    server_parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind to",
    )

    server_parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=8000,
        help="Port to listen on",
    )

    server_parser.add_argument(
        "--reload",
        "-r",
        action="store_true",
        help="Enable auto-reload",
    )

    server_parser.set_defaults(
        func=cmd_server
    )

    return parser


def main():
    parser = build_parser()

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()