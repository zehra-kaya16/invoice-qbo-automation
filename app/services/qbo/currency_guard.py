import re

from fastapi import HTTPException


def check_document_currency(client, data, vendor, account, bank_account=None):
    currency = data.currency
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise HTTPException(400, "Document currency must be an explicit three-letter code")
    try:
        preferences = client.get_currency_preferences()
    except Exception as exc:
        raise HTTPException(502, "Could not read QBO currency preferences; no transaction was created") from exc
    home = preferences.get("home_currency")
    multi = preferences.get("multicurrency_enabled")
    if not isinstance(home, str) or not re.fullmatch(r"[A-Z]{3}", home) or type(multi) is not bool:
        raise HTTPException(502, "QBO currency preferences are incomplete; no transaction was created")
    if currency != home:
        raise HTTPException(400, f"Document currency {currency} differs from QBO home currency {home}; foreign-currency creation is not supported")
    for label, entity in (("vendor", vendor), ("expense account", account), ("bank account", bank_account)):
        if entity is None:
            continue
        selected = entity.get("currency")
        if selected is None and not multi:
            selected = home
        # Expense accounts may have no currency even in multicurrency QBO.
        if selected is None and label == "expense account":
            continue
        if selected != currency:
            raise HTTPException(400, f"Selected {label} currency is missing or differs from document currency {currency}")
    return {"document_currency": currency, "home_currency": home,
            "multicurrency_enabled": multi, "policy": "home_currency_only"}
