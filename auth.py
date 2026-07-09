"""Auth utilities: Supabase user management, bcrypt password hashing, Fernet ICS encryption."""
from __future__ import annotations

import bcrypt
import streamlit as st
from cryptography.fernet import Fernet

ACCRUAL_RATES: dict[str, float] = {
    "<5":    9.231,
    "5-10":  10.769,
    "11-20": 11.385,
    "21+":   12.000,
}


def accrual_rate(tenure_bracket: str) -> float:
    return ACCRUAL_RATES[tenure_bracket]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def _fernet() -> Fernet:
    key = st.secrets["encryption"]["fernet_key"].encode()
    return Fernet(key)


def encrypt_ics_url(url: str) -> str:
    return _fernet().encrypt(url.encode()).decode()


def decrypt_ics_url(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()


from supabase import create_client, Client


def _get_client() -> Client:
    url = st.secrets["supabase"]["url"]
    key = st.secrets["supabase"]["service_role_key"]
    return create_client(url, key)


def register(
    last_name: str,
    password: str,
    base_rate: float,
    tenure_bracket: str,
    pto_balance: float,
    ics_url: str,
) -> tuple[bool, str]:
    """Create a new user account. Returns (success, error_message)."""
    client = _get_client()
    existing = client.table("users").select("id").ilike("last_name", last_name).execute()
    if existing.data:
        return False, "An account with that last name already exists."
    try:
        client.table("users").insert({
            "last_name":         last_name.strip(),
            "password_hash":     hash_password(password),
            "base_rate":         base_rate,
            "tenure_bracket":    tenure_bracket,
            "pto_balance":       pto_balance,
            "ics_url_encrypted": encrypt_ics_url(ics_url),
        }).execute()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def login(last_name: str, password: str) -> dict | None:
    """Verify credentials and return user dict (with decrypted ics_url), or None."""
    client = _get_client()
    result = client.table("users").select("*").ilike("last_name", last_name).execute()
    if not result.data:
        return None
    row = dict(result.data[0])
    if not verify_password(password, row["password_hash"]):
        return None
    row["ics_url"] = decrypt_ics_url(row.pop("ics_url_encrypted"))
    del row["password_hash"]
    return row


def update_settings(user_id: str, fields: dict) -> tuple[bool, str]:
    """
    Update user settings by user id. Accepted keys: base_rate, tenure_bracket,
    pto_balance, ics_url (encrypted automatically), password (hashed automatically).
    """
    client = _get_client()
    to_write: dict = {}
    for k, v in fields.items():
        if k == "ics_url":
            to_write["ics_url_encrypted"] = encrypt_ics_url(v)
        elif k == "password":
            to_write["password_hash"] = hash_password(v)
        else:
            to_write[k] = v
    try:
        client.table("users").update(to_write).eq("id", user_id).execute()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def save_stub(
    user_id: str,
    period_start: str,
    period_end: str | None,
    raw_gross: float,
    pto_balance: float,
    advice_number: str,
    earnings: list[dict],
) -> tuple[bool, str]:
    """Upsert a parsed stub into Supabase (keyed by user_id + period_start)."""
    client = _get_client()
    try:
        client.table("stubs").upsert(
            {
                "user_id":       user_id,
                "period_start":  period_start,
                "period_end":    period_end,
                "raw_gross":     raw_gross,
                "pto_balance":   pto_balance,
                "advice_number": advice_number,
                "earnings_json": earnings,
            },
            on_conflict="user_id,period_start",
        ).execute()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def load_stubs(user_id: str) -> list[dict]:
    """Return all stub rows for this user ordered by period_start."""
    client = _get_client()
    result = (
        client.table("stubs")
        .select("*")
        .eq("user_id", user_id)
        .order("period_start")
        .execute()
    )
    return result.data or []


def verify_current_password(user_id: str, password: str) -> bool:
    """Check a user's current password without full login flow."""
    client = _get_client()
    result = client.table("users").select("password_hash").eq("id", user_id).execute()
    if not result.data:
        return False
    return verify_password(password, result.data[0]["password_hash"])
