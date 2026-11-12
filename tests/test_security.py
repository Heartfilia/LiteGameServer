import pytest

from litegame_server.security import hash_password, verify_password
from manage import read_password


@pytest.mark.parametrize("password", ["1234", "中文密码", "a" * 1024])
def test_password_allowed_lengths(password):
    encoded = hash_password(password)
    assert verify_password(password, encoded)
    assert not verify_password(password + "wrong", encoded)


@pytest.mark.parametrize("password", ["", "a", "abc", "a" * 1025])
def test_password_rejected_lengths(password):
    with pytest.raises(ValueError, match="4–1024"):
        hash_password(password)


def test_cli_accepts_four_character_password(monkeypatch):
    monkeypatch.setattr("manage.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("manage.getpass.getpass", lambda prompt: "1234")
    assert verify_password("1234", read_password())
