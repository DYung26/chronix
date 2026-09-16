"""Regression tests for shared Google authentication."""

from datetime import datetime, timedelta
from unittest.mock import patch

from google.oauth2.credentials import Credentials

from chronix.integrations.google_calendar.client import GoogleCalendarClient
from chronix.integrations.google_calendar.sync_service import CalendarSyncService
from chronix.integrations.google_docs.auth import OAuthAuth, SCOPES


def _valid_credentials() -> Credentials:
    return Credentials(
        token="access",
        refresh_token="refresh",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="id",
        client_secret="secret",
        scopes=SCOPES,
        expiry=datetime.now() + timedelta(hours=1),
    )


def test_oauth_reuses_cached_credentials_without_reloading_or_authenticating(tmp_path):
    auth = OAuthAuth(tmp_path / "credentials.json", tmp_path / "token.json")
    creds = _valid_credentials()
    auth._credentials = creds

    with patch("chronix.integrations.google_docs.auth.Credentials.from_authorized_user_file") as load:
        assert auth.get_credentials(interactive=False) is creds
        load.assert_not_called()

    with patch("chronix.integrations.google_docs.auth.build") as build:
        auth.get_service(interactive=False)
        assert build.call_args.kwargs["credentials"] is creds


def test_mcp_style_noninteractive_auth_never_starts_browser_when_credentials_unusable(tmp_path):
    auth = OAuthAuth(tmp_path / "credentials.json", tmp_path / "token.json")
    auth._credentials = Credentials(
        token=None,
        refresh_token=None,
        token_uri="https://oauth2.googleapis.com/token",
        client_id="id",
        client_secret="secret",
        scopes=SCOPES,
    )

    with (
        patch("chronix.integrations.google_docs.auth.webbrowser.open") as browser,
        patch("chronix.integrations.google_docs.auth.InstalledAppFlow.from_client_secrets_file") as flow,
    ):
        try:
            auth.get_credentials(interactive=False)
        except RuntimeError as exc:
            assert "interactive authorization" in str(exc)
        else:
            raise AssertionError("expected non-interactive authentication failure")

        browser.assert_not_called()
        flow.assert_not_called()


def test_expired_refreshable_credentials_are_refreshed_before_interactive_auth(tmp_path):
    auth = OAuthAuth(tmp_path / "credentials.json", tmp_path / "token.json")
    creds = _valid_credentials()
    creds.expiry = datetime.now() - timedelta(hours=1)
    auth._credentials = creds

    def refresh(_request):
        creds.token = "refreshed"
        creds.expiry = datetime.now() + timedelta(hours=1)

    with patch.object(creds, "refresh", side_effect=refresh) as refresh_mock:
        result = auth.get_credentials(interactive=False)

    assert result is creds
    assert creds.token == "refreshed"
    refresh_mock.assert_called_once()


def test_calendar_uses_the_same_auth_strategy_and_credentials(tmp_path):
    auth = OAuthAuth(tmp_path / "credentials.json", tmp_path / "token.json")
    creds = _valid_credentials()
    auth._credentials = creds

    client = GoogleCalendarClient(auth_strategy=auth)
    with patch("chronix.integrations.google_calendar.client.build") as build:
        client.authenticate(interactive=False)
        assert build.call_args.kwargs["credentials"] is creds

    sync_service = CalendarSyncService(auth_strategy=auth)
    assert sync_service.client.auth_strategy is auth
