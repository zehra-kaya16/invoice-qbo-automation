"""Explicit authenticated actor for legacy workflow tests; never used by the app."""
from app.core.auth import Principal, get_current_user
from app.services.persistence.auth_repository import company_uuid

TEST_PRINCIPAL = Principal("00000000-0000-0000-0000-000000000100", company_uuid("default"),
                           "default", "Test company", "workflow@example.test", "Test admin", "admin", "test-session", "test-csrf")


def override_authenticated_api(app):
    async def test_actor():
        return TEST_PRINCIPAL
    app.dependency_overrides[get_current_user] = test_actor
