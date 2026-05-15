from localmail import secrets


def test_password_roundtrip():
    assert secrets.get_password("acct") is None
    secrets.set_password("acct", "hunter2")
    assert secrets.get_password("acct") == "hunter2"
    secrets.delete_password("acct")
    assert secrets.get_password("acct") is None


def test_refresh_token_uses_separate_slot_from_password():
    secrets.set_password("acct", "imap-pw")
    secrets.set_refresh_token("acct", "rt-abc")
    assert secrets.get_password("acct") == "imap-pw"
    assert secrets.get_refresh_token("acct") == "rt-abc"


def test_delete_password_is_idempotent():
    secrets.delete_password("missing")  # must not raise
    secrets.delete_refresh_token("missing")
