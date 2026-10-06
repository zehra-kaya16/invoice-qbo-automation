from types import SimpleNamespace

import pytest

from app.services.extraction.extractor import (
    DocumentExtractor,
)


def test_unknown_ai_provider_is_rejected():
    with pytest.raises(
        ValueError,
        match="Unknown provider",
    ):
        DocumentExtractor(
            api_key="test-key",
            provider="invalid",
        )


def test_openai_vision_request():
    extractor = DocumentExtractor.__new__(
        DocumentExtractor
    )

    extractor.provider = "openai"
    extractor.model = "gpt-4o"

    message = SimpleNamespace(
        content="openai-result"
    )

    choice = SimpleNamespace(
        message=message
    )

    completions = SimpleNamespace(
        create=lambda **kwargs: (
            SimpleNamespace(
                choices=[choice]
            )
        )
    )

    chat = SimpleNamespace(
        completions=completions
    )

    extractor.client = SimpleNamespace(
        chat=chat
    )

    result = extractor._vision_request(
        b"image",
        "test prompt",
    )

    assert result == "openai-result"


def test_anthropic_vision_request():
    extractor = DocumentExtractor.__new__(
        DocumentExtractor
    )

    extractor.provider = "anthropic"
    extractor.model = (
        "claude-3-5-sonnet-20241022"
    )

    content = SimpleNamespace(
        text="anthropic-result"
    )

    messages = SimpleNamespace(
        create=lambda **kwargs: (
            SimpleNamespace(
                content=[content]
            )
        )
    )

    extractor.client = SimpleNamespace(
        messages=messages
    )

    result = extractor._vision_request(
        b"image",
        "test prompt",
    )

    assert result == "anthropic-result"