"""Unit tests for auth.py crypto utilities — no Supabase required."""
import pytest
from unittest.mock import patch, MagicMock

FAKE_FERNET_KEY = "Ek8kxT_yt_mCLExr8VxWjQC1gKZOlEePFl37fVH3kJI="


def _make_fernet_secrets():
    return {"encryption": {"fernet_key": FAKE_FERNET_KEY},
            "supabase": {"url": "http://fake", "service_role_key": "fake"}}


def test_hash_and_verify_password():
    from auth import hash_password, verify_password
    h = hash_password("secret123")
    assert h != "secret123"
    assert verify_password("secret123", h)
    assert not verify_password("wrong", h)


def test_encrypt_decrypt_ics_url():
    with patch("streamlit.secrets", _make_fernet_secrets()):
        from auth import encrypt_ics_url, decrypt_ics_url
        url = "https://www.shiftadmin.com/schedule_ical.php?cd=v2&u=test&h=abc123"
        ciphertext = encrypt_ics_url(url)
        assert ciphertext != url
        assert decrypt_ics_url(ciphertext) == url


def test_accrual_rate():
    from auth import accrual_rate
    assert accrual_rate("<5")    == 9.231
    assert accrual_rate("5-10")  == 10.769
    assert accrual_rate("11-20") == 11.385
    assert accrual_rate("21+")   == 12.000


def test_accrual_rate_invalid():
    from auth import accrual_rate
    with pytest.raises(KeyError):
        accrual_rate("invalid")
