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


def _mock_supabase(rows=None, insert_ok=True):
    """Returns a mock Supabase client that returns `rows` on select."""
    client = MagicMock()
    select_result = MagicMock()
    select_result.data = rows or []
    client.table.return_value.select.return_value.ilike.return_value.execute.return_value = select_result
    client.table.return_value.select.return_value.eq.return_value.execute.return_value = select_result
    insert_result = MagicMock()
    insert_result.data = [{"id": "fake-uuid"}] if insert_ok else []
    client.table.return_value.insert.return_value.execute.return_value = insert_result
    update_result = MagicMock()
    update_result.data = [{"id": "fake-uuid"}]
    client.table.return_value.update.return_value.eq.return_value.execute.return_value = update_result
    return client


def test_register_success():
    with patch("streamlit.secrets", _make_fernet_secrets()), \
         patch("auth._get_client") as mock_get_client:
        from auth import register, hash_password
        mock_get_client.return_value = _mock_supabase(rows=[])
        ok, err = register("Smith", "pass123", 94.37, "5-10", 45.0,
                           "https://shiftadmin.com/ical?u=test&h=abc")
        assert ok is True
        assert err == ""


def test_register_duplicate():
    with patch("streamlit.secrets", _make_fernet_secrets()), \
         patch("auth._get_client") as mock_get_client:
        from auth import register
        mock_get_client.return_value = _mock_supabase(rows=[{"id": "existing"}])
        ok, err = register("Smith", "pass123", 94.37, "5-10", 45.0, "https://x.com")
        assert ok is False
        assert "already exists" in err


def test_login_success():
    from auth import hash_password
    hashed = hash_password("secret")
    with patch("streamlit.secrets", _make_fernet_secrets()), \
         patch("auth._get_client") as mock_get_client, \
         patch("auth.encrypt_ics_url", return_value="ENCRYPTED"), \
         patch("auth.decrypt_ics_url", return_value="https://real-url.com"):
        from auth import login
        fake_row = {
            "id": "uuid-1", "last_name": "Smith", "password_hash": hashed,
            "base_rate": 94.37, "tenure_bracket": "5-10",
            "pto_balance": 45.0, "ics_url_encrypted": "ENCRYPTED",
        }
        mock_get_client.return_value = _mock_supabase(rows=[fake_row])
        user = login("Smith", "secret")
        assert user is not None
        assert user["ics_url"] == "https://real-url.com"
        assert "password_hash" not in user
        assert "ics_url_encrypted" not in user


def test_login_wrong_password():
    from auth import hash_password
    hashed = hash_password("correct")
    with patch("streamlit.secrets", _make_fernet_secrets()), \
         patch("auth._get_client") as mock_get_client:
        from auth import login
        fake_row = {"id": "uuid-1", "last_name": "Smith",
                    "password_hash": hashed, "base_rate": 94.37,
                    "tenure_bracket": "5-10", "pto_balance": 45.0,
                    "ics_url_encrypted": "ENCRYPTED"}
        mock_get_client.return_value = _mock_supabase(rows=[fake_row])
        user = login("Smith", "wrong")
        assert user is None
