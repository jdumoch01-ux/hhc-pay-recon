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
