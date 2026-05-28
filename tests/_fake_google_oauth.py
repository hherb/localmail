"""In-memory stand-in for the Google OAuth Flow used by api/admin/oauth.py.

Mirrors the FakeIMAPClient pattern in tests/_fake_imap.py. Not exposed via
conftest — tests opt in by importing and monkeypatching.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeCredentials:
    refresh_token: str
    token: str = 'fake-access-token'


@dataclass
class FakeFlow:
    """Stand-in for google_auth_oauthlib.flow.Flow."""

    redirect_uri: str = 'https://example.test/cb'
    client_secrets_file: object = None
    code_to_refresh: dict[str, str] = field(
        default_factory=lambda: {'good-code': 'refresh-xyz'}
    )
    exchanged_codes: list[str] = field(default_factory=list)

    def authorization_url(self, *, state: str, prompt: str = 'consent',
                          access_type: str = 'offline') -> tuple[str, str]:
        return (
            f'https://accounts.google.com/o/oauth2/auth?state={state}',
            state,
        )

    def fetch_token(self, *, code: str) -> dict:
        self.exchanged_codes.append(code)
        if code not in self.code_to_refresh:
            raise RuntimeError(f"unknown code {code!r}")
        return {'refresh_token': self.code_to_refresh[code]}

    @property
    def credentials(self) -> FakeCredentials:
        # The real Flow exposes credentials after fetch_token().
        last_code = self.exchanged_codes[-1]
        return FakeCredentials(refresh_token=self.code_to_refresh[last_code])
