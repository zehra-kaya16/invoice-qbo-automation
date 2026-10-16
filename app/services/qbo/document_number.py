"""Validate QBO's DocNumber without truncating a reviewed reference."""
from typing import Optional


def normalize_document_number(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('Document number must be text')
    value = value.strip()
    if not value:
        return None
    if len(value) > 21:
        raise ValueError('Document number exceeds the QBO limit of 21 characters; review it before pushing')
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('Document number must not contain control characters or line breaks')
    return value
