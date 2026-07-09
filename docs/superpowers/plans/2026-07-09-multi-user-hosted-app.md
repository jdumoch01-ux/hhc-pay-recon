# Multi-User Hosted Pay Reconciliation App — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform the local single-user Streamlit app into a hosted multi-user app on Streamlit Community Cloud, with Supabase-backed auth and per-user settings synced across all devices.

**Architecture:** Each provider registers with last name + password; their base rate, tenure bracket, PTO balance, and ShiftAdmin ICS URL are stored in Supabase (ICS URL Fernet-encrypted at application level). On login, settings load from Supabase and are held in `st.session_state` for the session. All existing reconciliation logic is unchanged — `schedule.py` and `pay_rules.py` are refactored to accept per-user parameters instead of reading from `config.toml`.

**Tech Stack:** Python 3.13, Streamlit, Supabase Python client (`supabase`), `cryptography` (Fernet), `bcrypt`, pdfplumber, icalendar — deployed on Streamlit Community Cloud, database on Supabase free tier.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `auth.py` | **Create** | All auth logic: Supabase client, bcrypt, Fernet, register/login/update |
| `requirements.txt` | **Create** | Pinned dependencies for Streamlit Cloud |
| `.streamlit/secrets.toml.template` | **Create** | Developer reference for required secrets |
| `.gitignore` | **Create/update** | Exclude `secrets.toml` and `__pycache__` |
| `tests/test_auth.py` | **Create** | Unit tests for crypto utilities (no Supabase required) |
| `schedule.py` | **Modify** | Accept `ics_url: str | None` param in `load_shifts` + `load_all_shifts` |
| `pay_rules.py` | **Modify** | Accept `base_rate_override` + `accrual_override` in `PayConfig.load` |
| `app.py` | **Modify** | Auth gate, per-user config, settings tab, other earnings, PTO update gate |
| `config.toml` | **Modify** | Remove `ics_url` and `base_rate` (now per-user in Supabase) |

---

## Task 1: Supabase project setup

**Files:**
- Reference only (manual steps in Supabase dashboard)

- [ ] **Step 1: Create a Supabase project**

  Go to https://supabase.com → New project. Note the project URL and service role key from Settings → API.

- [ ] **Step 2: Run this SQL in the Supabase SQL editor**

```sql
CREATE TABLE users (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  last_name TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  base_rate FLOAT NOT NULL,
  tenure_bracket TEXT NOT NULL
    CHECK (tenure_bracket IN ('<5', '5-10', '11-20', '21+')),
  pto_balance FLOAT NOT NULL DEFAULT 0.0,
  ics_url_encrypted TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Case-insensitive unique constraint on last_name
CREATE UNIQUE INDEX users_last_name_idx ON users (LOWER(last_name));

-- Enable RLS (defense in depth; app uses service role key which bypasses RLS,
-- but this prevents any anon-key access to the table)
ALTER TABLE users ENABLE ROW LEVEL SECURITY;

-- Auto-update updated_at on any row change
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER users_updated_at
  BEFORE UPDATE ON users
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();
```

- [ ] **Step 3: Note credentials**

  From Supabase → Settings → API, copy:
  - Project URL (e.g. `https://abcdefgh.supabase.co`)
  - `service_role` secret key (NOT the anon key)

---

## Task 2: Requirements and secrets template

**Files:**
- Create: `requirements.txt`
- Create: `.streamlit/secrets.toml.template`
- Create/update: `.gitignore`

- [ ] **Step 1: Create `requirements.txt`**

```
streamlit>=1.35.0
pandas>=2.0.0
pdfplumber>=0.10.0
icalendar>=5.0.0
pytz>=2024.1
certifi>=2024.0.0
supabase>=2.4.0
cryptography>=42.0.0
bcrypt>=4.1.0
```

Run: `cd ~/pay-reconciliation && .venv/bin/pip install supabase cryptography bcrypt`
Expected: all three packages install without error.

- [ ] **Step 2: Create `.streamlit/secrets.toml.template`**

```bash
mkdir -p ~/pay-reconciliation/.streamlit
```

Contents of `.streamlit/secrets.toml.template`:
```toml
# Copy to .streamlit/secrets.toml and fill in real values.
# NEVER commit secrets.toml — it is gitignored.

[supabase]
url = "https://your-project-id.supabase.co"
service_role_key = "eyJ..."  # Supabase → Settings → API → service_role

[encryption]
# Generate once with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
fernet_key = ""
```

- [ ] **Step 3: Generate a Fernet key and create your real secrets.toml**

```bash
cd ~/pay-reconciliation
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Copy the output. Create `.streamlit/secrets.toml` using the template above with your real Supabase URL, service role key, and the Fernet key you just generated.

- [ ] **Step 4: Update `.gitignore`**

```
__pycache__/
*.pyc
.venv/
actuals.json
.streamlit/secrets.toml
```

- [ ] **Step 5: Verify secrets load correctly**

```bash
cd ~/pay-reconciliation && .venv/bin/python -c "
import tomllib
with open('.streamlit/secrets.toml', 'rb') as f:
    s = tomllib.load(f)
print('supabase url:', s['supabase']['url'])
print('fernet key length:', len(s['encryption']['fernet_key']))
print('OK')
"
```

Expected: prints your Supabase URL and a 44-character Fernet key, no errors.

---

## Task 3: `auth.py` — crypto and accrual utilities

**Files:**
- Create: `auth.py`
- Create: `tests/test_auth.py`

- [ ] **Step 1: Write failing tests for crypto utilities**

Create `tests/__init__.py` (empty) and `tests/test_auth.py`:

```python
"""Unit tests for auth.py crypto utilities — no Supabase required."""
import pytest
from unittest.mock import patch, MagicMock

# Provide fake secrets so auth.py doesn't call st.secrets at import time
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
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd ~/pay-reconciliation && .venv/bin/pip install pytest -q && .venv/bin/pytest tests/test_auth.py -v 2>&1 | head -20
```

Expected: ImportError or ModuleNotFoundError — `auth` doesn't exist yet.

- [ ] **Step 3: Create `auth.py` with crypto utilities**

```python
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
```

- [ ] **Step 4: Run tests — expect all to pass**

```bash
cd ~/pay-reconciliation && .venv/bin/pytest tests/test_auth.py -v
```

Expected:
```
tests/test_auth.py::test_hash_and_verify_password PASSED
tests/test_auth.py::test_encrypt_decrypt_ics_url PASSED
tests/test_auth.py::test_accrual_rate PASSED
tests/test_auth.py::test_accrual_rate_invalid PASSED
4 passed
```

- [ ] **Step 5: Commit**

```bash
cd ~/pay-reconciliation && git add auth.py tests/ requirements.txt .gitignore .streamlit/secrets.toml.template && git commit -m "feat: add auth crypto utilities and accrual rate table"
```

---

## Task 4: `auth.py` — Supabase operations

**Files:**
- Modify: `auth.py`
- Modify: `tests/test_auth.py`

- [ ] **Step 1: Write failing tests for Supabase operations**

Add to `tests/test_auth.py`:

```python
def _mock_supabase(rows=None, insert_ok=True):
    """Returns a mock Supabase client that returns `rows` on select."""
    client = MagicMock()
    select_result = MagicMock()
    select_result.data = rows or []
    # Chain: .table().select().ilike().execute() or .table().select().eq().execute()
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
        mock_get_client.return_value = _mock_supabase(rows=[])  # no existing user
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
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd ~/pay-reconciliation && .venv/bin/pytest tests/test_auth.py -v -k "register or login" 2>&1 | tail -10
```

Expected: ImportError on `_get_client` — function not defined yet.

- [ ] **Step 3: Add Supabase operations to `auth.py`**

Append to `auth.py` after the existing crypto functions:

```python
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
            "last_name":       last_name.strip(),
            "password_hash":   hash_password(password),
            "base_rate":       base_rate,
            "tenure_bracket":  tenure_bracket,
            "pto_balance":     pto_balance,
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


def verify_current_password(user_id: str, password: str) -> bool:
    """Check a user's current password without full login flow."""
    client = _get_client()
    result = client.table("users").select("password_hash").eq("id", user_id).execute()
    if not result.data:
        return False
    return verify_password(password, result.data[0]["password_hash"])
```

- [ ] **Step 4: Run all auth tests**

```bash
cd ~/pay-reconciliation && .venv/bin/pytest tests/test_auth.py -v
```

Expected: 8 tests, all PASSED.

- [ ] **Step 5: Commit**

```bash
cd ~/pay-reconciliation && git add auth.py tests/test_auth.py && git commit -m "feat: add Supabase register/login/update_settings to auth.py"
```

---

## Task 5: Refactor `schedule.py` — accept `ics_url` parameter

**Files:**
- Modify: `schedule.py`

- [ ] **Step 1: Update `load_shifts` to accept optional `ics_url`**

In `schedule.py`, replace the `load_shifts` function:

```python
def load_shifts(ics_url: str | None = None) -> list[Shift]:
    """Load shifts from the ICS feed only (rolling ~8-month window)."""
    cfg = load_config()["schedule"]
    url = ics_url or cfg["ics_url"]
    text = fetch_ics(url)
    return parse_ics(text, ZoneInfo(cfg["local_tz"]))
```

- [ ] **Step 2: Update `load_all_shifts` to accept optional `ics_url`**

Replace `load_all_shifts`:

```python
def load_all_shifts(ics_url: str | None = None) -> list[Shift]:
    """
    Load shifts from both the ICS feed (current/future) and any historical
    ShiftAdmin PDF exports found in config [schedule] pdf_dir.
    Deduplicates by start time — ICS entries take precedence.
    """
    cfg_sched = load_config()["schedule"]
    local_tz = ZoneInfo(cfg_sched["local_tz"])

    ics_shifts = load_shifts(ics_url=ics_url)
    ics_keys = {s.start: s for s in ics_shifts}

    pdf_dir = cfg_sched.get("pdf_dir", "")
    pdf_shifts: list[Shift] = []
    if pdf_dir:
        from parse_schedule_pdf import load_historical_shifts
        pdfs = sorted(Path(pdf_dir).glob("schedule_*.pdf"))
        if pdfs:
            pdf_shifts = load_historical_shifts(
                [str(p) for p in pdfs], cfg_sched["local_tz"]
            )

    combined = list(ics_shifts)
    for s in pdf_shifts:
        if s.start not in ics_keys:
            combined.append(s)

    return sorted(combined, key=lambda s: s.start)
```

- [ ] **Step 3: Verify local usage still works**

```bash
cd ~/pay-reconciliation && .venv/bin/python -c "
from schedule import load_all_shifts
shifts = load_all_shifts()  # no arg → reads from config.toml as before
print(f'Loaded {len(shifts)} shifts — OK')
"
```

Expected: `Loaded N shifts — OK` with no errors.

- [ ] **Step 4: Commit**

```bash
cd ~/pay-reconciliation && git add schedule.py && git commit -m "refactor: load_shifts/load_all_shifts accept optional ics_url param"
```

---

## Task 6: Refactor `pay_rules.py` — accept per-user overrides

**Files:**
- Modify: `pay_rules.py`

- [ ] **Step 1: Update `PayConfig.load` to accept overrides**

In `pay_rules.py`, replace the `load` classmethod with:

```python
@classmethod
def load(
    cls,
    base_rate_override: float | None = None,
    accrual_override: float | None = None,
) -> "PayConfig":
    with CONFIG_PATH.open("rb") as f:
        cfg = tomllib.load(f)
    p = cfg["pay"]
    d = cfg["differentials"]
    q = cfg["pto"]
    return cls(
        base_rate=base_rate_override if base_rate_override is not None else p["base_rate"],
        perdiem_rate=p["perdiem_rate"],
        admin_hours_per_week=p["admin_hours_per_week"],
        ot_threshold=p["ot_threshold"],
        min_qualifying_hours=d["min_qualifying_hours"],
        evening_rate=d["evening_rate"],
        evening_start=time.fromisoformat(d["evening_start"]),
        evening_end=time.fromisoformat(d["evening_end"]),
        night_rate=d["night_rate"],
        night_start=time.fromisoformat(d["night_start"]),
        night_end=time.fromisoformat(d["night_end"]),
        weekend_rate=d["weekend_rate"],
        weekend_start_day=d["weekend_start_day"],
        weekend_start_time=time.fromisoformat(d["weekend_start_time"]),
        weekend_end_day=d["weekend_end_day"],
        weekend_end_time=time.fromisoformat(d["weekend_end_time"]),
        holiday_pct=d["holiday_pct"],
        ot_evening_rate=d["ot_evening_rate"],
        ot_weekend_rate=d["ot_weekend_rate"],
        pto_standard_weekly_hours=q["standard_weekly_hours"],
        pto_accrual_per_period=accrual_override if accrual_override is not None else q["accrual_per_period"],
    )
```

- [ ] **Step 2: Verify backward compatibility**

```bash
cd ~/pay-reconciliation && .venv/bin/python -c "
from pay_rules import PayConfig
cfg = PayConfig.load()
print(f'base_rate={cfg.base_rate}  accrual={cfg.pto_accrual_per_period}  — OK')
cfg2 = PayConfig.load(base_rate_override=91.72, accrual_override=9.231)
print(f'override base_rate={cfg2.base_rate}  accrual={cfg2.pto_accrual_per_period}  — OK')
"
```

Expected:
```
base_rate=94.37  accrual=11.08  — OK
override base_rate=91.72  accrual=9.231  — OK
```

- [ ] **Step 3: Update `config.toml` — remove personal fields**

Remove the `ics_url` line from `[schedule]` and the `base_rate` line from `[pay]`. The file should now look like:

```toml
[schedule]
local_tz = "America/New_York"
pdf_dir = "/Users/joshdumoch/Desktop/Schedule"

[pay]
perdiem_rate       = 110.00
admin_hours_per_week = 4.0
ot_threshold       = 80.0

[differentials]
min_qualifying_hours = 4.0
evening_rate  = 8.40
evening_start = "15:00"
evening_end   = "23:00"
night_rate    = 11.00
night_start   = "23:00"
night_end     = "07:00"
weekend_rate       = 11.00
weekend_start_day  = 4
weekend_start_time = "23:00"
weekend_end_day    = 6
weekend_end_time   = "23:00"
holiday_pct = 0.50
ot_evening_rate = 8.40
ot_weekend_rate = 14.00

[pto]
standard_weekly_hours = 36.0
accrual_per_period = 11.08
```

Note: `ics_url` and `base_rate` are now per-user in Supabase. `pdf_dir` remains for local use only and is ignored on Streamlit Cloud (no local filesystem). `accrual_per_period` in config is the fallback/default only. The differential rates shown above (`evening_rate = 8.40`, `night_rate = 11.00`, `weekend_rate = 11.00`) reflect the corrected rates confirmed from the Jul 2, 2026 back pay stub — these replace the old $6.72 evening and $8.40 weekend rates.

- [ ] **Step 4: Run all auth tests to confirm nothing broke**

```bash
cd ~/pay-reconciliation && .venv/bin/pytest tests/test_auth.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
cd ~/pay-reconciliation && git add pay_rules.py config.toml && git commit -m "refactor: PayConfig.load accepts base_rate/accrual overrides; remove personal fields from config.toml"
```

---

## Task 7: `app.py` — auth gate and login/register UI

**Files:**
- Modify: `app.py`

- [ ] **Step 1: Add `import auth` and update cached loaders**

At the top of `app.py`, add `import auth` after the existing imports.

Replace the two cached loader functions:

```python
@st.cache_data(show_spinner="Fetching schedule from ShiftAdmin…")
def _load_results(ics_url: str) -> list[PeriodResult]:
    from schedule import load_all_shifts
    shifts = load_all_shifts(ics_url=ics_url)
    return reconcile(shifts=shifts)


@st.cache_data(show_spinner=False)
def _load_cfg(base_rate: float, accrual_per_period: float) -> PayConfig:
    return PayConfig.load(
        base_rate_override=base_rate,
        accrual_override=accrual_per_period,
    )
```

- [ ] **Step 2: Add `_show_auth_page` function**

Add this new function before `main()`:

```python
TENURE_OPTIONS = ["<5", "5-10", "11-20", "21+"]
TENURE_LABELS  = [
    "Less than 5 years  (9.231h/period)",
    "5–10 years         (10.769h/period)",
    "11–20 years        (11.385h/period)",
    "21+ years          (12.000h/period)",
]


def _show_auth_page() -> None:
    st.title("💵 HHC Pay Reconciliation")
    st.caption("Charlotte Hungerford Hospital — APP Pay Audit Tool")
    st.divider()

    tab_login, tab_register = st.tabs(["Login", "Create Account"])

    with tab_login:
        with st.form("login_form"):
            last_name = st.text_input("Last name")
            password  = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Login", use_container_width=True)
        if submitted:
            user = auth.login(last_name.strip(), password)
            if user:
                st.session_state["user"] = user
                st.rerun()
            else:
                st.error("Invalid last name or password.")

    with tab_register:
        with st.form("register_form"):
            last_name   = st.text_input("Last name")
            password    = st.text_input("Password", type="password")
            confirm_pw  = st.text_input("Confirm password", type="password")
            base_rate   = st.number_input(
                "Base hourly rate ($)", min_value=50.0, max_value=300.0,
                value=94.37, step=0.01,
            )
            tenure_idx  = st.selectbox(
                "Years of service (determines PTO accrual)",
                range(len(TENURE_LABELS)),
                format_func=lambda i: TENURE_LABELS[i],
            )
            pto_balance = st.number_input(
                "Current PTO balance (hours) — from your most recent pay stub",
                min_value=0.0, max_value=500.0, value=0.0, step=0.5,
            )
            ics_url = st.text_input(
                "ShiftAdmin ICS URL",
                type="password",
                help="From ShiftAdmin → My Schedule → Subscribe to Calendar. "
                     "Contains your personal auth token — stored encrypted.",
            )
            submitted = st.form_submit_button("Create account", use_container_width=True)

        if submitted:
            tenure_bracket = TENURE_OPTIONS[tenure_idx]
            if password != confirm_pw:
                st.error("Passwords don't match.")
            elif not all([last_name.strip(), password, ics_url.strip()]):
                st.error("Last name, password, and ShiftAdmin URL are required.")
            else:
                ok, err = auth.register(
                    last_name.strip(), password, base_rate,
                    tenure_bracket, pto_balance, ics_url.strip(),
                )
                if ok:
                    user = auth.login(last_name.strip(), password)
                    st.session_state["user"] = user
                    st.rerun()
                else:
                    st.error(err)
```

- [ ] **Step 3: Update `_build_sidebar` to accept user and show logout**

Replace the `_build_sidebar` function:

```python
def _build_sidebar(cfg: PayConfig, user: dict) -> None:
    with st.sidebar:
        st.title("💵 HHC Pay Recon")
        st.caption(f"Logged in as **{user['last_name']}**")
        st.divider()
        st.subheader("Rates")
        st.caption(
            f"Base: **${cfg.base_rate}/hr**  \n"
            f"OT (per diem): **${cfg.perdiem_rate}/hr**  \n"
            f"Admin: **{cfg.admin_hours_per_week:.0f}h/wk** (unconditional)  \n"
            f"OT threshold: **{cfg.ot_threshold:.0f}h** biweekly  \n\n"
            f"Eve diff: **+${cfg.evening_rate}/hr** (15:00–23:00, ≥4h)  \n"
            f"Night diff: **+${cfg.night_rate}/hr** (23:00–07:00, ≥4h)  \n"
            f"Wknd diff: **+${cfg.weekend_rate}/hr** (Fri 23:00–Sun 23:00, ≥4h)  \n"
            f"Holiday: **+50% base** on HHC holidays  \n"
        )
        st.divider()
        if st.button("🔄 Refresh schedule", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
        if st.button("🚪 Logout", use_container_width=True):
            del st.session_state["user"]
            st.cache_data.clear()
            st.rerun()
```

- [ ] **Step 4: Update `main()` to gate on auth and pass user through**

Replace the `main()` function:

```python
def main() -> None:
    if "user" not in st.session_state:
        _show_auth_page()
        return

    user    = st.session_state["user"]
    accrual = auth.accrual_rate(user["tenure_bracket"])
    cfg     = _load_cfg(float(user["base_rate"]), accrual)
    results = _load_results(user["ics_url"])
    actuals = _load_actuals()

    _build_sidebar(cfg, user)

    st.title("💵 HHC Pay Reconciliation")

    if not results:
        st.warning("No shifts loaded — check your ShiftAdmin URL in Settings.")
        return

    tab_sched, tab_audit, tab_settings = st.tabs(
        ["📋 Schedule", "🔍 Year Audit", "⚙️ Settings"]
    )

    with tab_sched:
        df = _build_summary_df(results, actuals, cfg)
        st.caption("Click a row to see the full engine breakdown and compare against a stub.")
        event = st.dataframe(
            df.style.map(_delta_css, subset=["Δ"]),
            on_select="rerun",
            selection_mode="single-row",
            use_container_width=True,
            hide_index=True,
        )
        selected = event.selection.rows
        if selected:
            idx = selected[0]
            st.divider()
            _show_detail(results[idx], actuals, cfg, results)
        else:
            st.divider()
            total_est  = sum(r.total_estimated_gross() for r in results)
            total_eve  = sum(r.evening_pay() + r.ot_evening_pay() for r in results)
            total_wknd = sum(r.weekend_pay() + r.ot_weekend_pay() for r in results)
            total_hol  = sum(r.holiday_pay() for r in results)
            cc1, cc2, cc3, cc4 = st.columns(4)
            cc1.metric("Total Estimated Gross", f"${total_est:,.2f}")
            cc2.metric("Evening Differentials", f"${total_eve:,.2f}")
            cc3.metric("Weekend Differentials", f"${total_wknd:,.2f}")
            cc4.metric("Holiday Differentials", f"${total_hol:,.2f}")
            if actuals:
                paid   = sum(actuals.values())
                unpaid = [r for r in results if r.period.label not in actuals]
                st.caption(
                    f"Actual gross entered for {len(actuals)} period(s): ${paid:,.2f}. "
                    f"{len(unpaid)} period(s) estimated only."
                )
            st.divider()
            _show_pto_projection(results, cfg)

    with tab_audit:
        _show_year_audit(results, cfg, user)

    with tab_settings:
        _show_settings(user)
```

- [ ] **Step 5: Manual smoke test — verify auth gate works**

```bash
cd ~/pay-reconciliation && .venv/bin/streamlit run app.py
```

Open in browser. Confirm: login/register screen appears (not the main app). Confirm the "Create Account" tab shows all fields. Stop the server with Ctrl+C.

- [ ] **Step 6: Commit**

```bash
cd ~/pay-reconciliation && git add app.py && git commit -m "feat: add auth gate, login/register UI, per-user config injection"
```

---

## Task 8: `app.py` — Settings tab

**Files:**
- Modify: `app.py`

- [ ] **Step 1: Add `_show_settings` function**

Add before `main()`:

```python
def _show_settings(user: dict) -> None:
    st.subheader("Account Settings")
    st.caption(f"Account: **{user['last_name']}**")

    with st.form("settings_form"):
        base_rate = st.number_input(
            "Base hourly rate ($)",
            min_value=50.0, max_value=300.0,
            value=float(user["base_rate"]), step=0.01,
        )
        tenure_idx = st.selectbox(
            "Years of service",
            range(len(TENURE_OPTIONS)),
            index=TENURE_OPTIONS.index(user["tenure_bracket"]),
            format_func=lambda i: TENURE_LABELS[i],
        )
        pto_balance = st.number_input(
            "PTO balance (hours)",
            min_value=0.0, max_value=500.0,
            value=float(user["pto_balance"]), step=0.5,
        )
        ics_url = st.text_input(
            "ShiftAdmin ICS URL (leave blank to keep current)",
            type="password",
        )
        st.divider()
        st.subheader("Change Password")
        current_pw = st.text_input("Current password", type="password",
                                   key="settings_current_pw")
        new_pw     = st.text_input("New password", type="password",
                                   key="settings_new_pw")
        confirm_pw = st.text_input("Confirm new password", type="password",
                                   key="settings_confirm_pw")

        save = st.form_submit_button("Save changes", use_container_width=True)

    if save:
        updates: dict = {
            "base_rate":      base_rate,
            "tenure_bracket": TENURE_OPTIONS[tenure_idx],
            "pto_balance":    pto_balance,
        }
        if ics_url.strip():
            updates["ics_url"] = ics_url.strip()
        if new_pw:
            if not current_pw:
                st.error("Enter your current password to set a new one.")
                return
            if new_pw != confirm_pw:
                st.error("New passwords don't match.")
                return
            if not auth.verify_current_password(user["id"], current_pw):
                st.error("Current password is incorrect.")
                return
            updates["password"] = new_pw

        ok, err = auth.update_settings(user["id"], updates)
        if ok:
            for k, v in updates.items():
                if k not in ("password",):
                    st.session_state["user"][k] = v
            if "ics_url" in updates:
                st.session_state["user"]["ics_url"] = updates["ics_url"]
            st.success("Settings saved.")
            st.cache_data.clear()
            st.rerun()
        else:
            st.error(f"Save failed: {err}")
```

- [ ] **Step 2: Manual smoke test**

Start the app, register a test account, log in, open Settings tab. Confirm all fields populate with the registered values. Change base rate and save. Confirm success message and that the sidebar refreshes to show the new rate. Stop server.

- [ ] **Step 3: Commit**

```bash
cd ~/pay-reconciliation && git add app.py && git commit -m "feat: add settings tab with base rate, tenure, PTO balance, ICS URL, password change"
```

---

## Task 9: `app.py` — Other Earnings section in Stub Audit

**Files:**
- Modify: `app.py`

- [ ] **Step 1: Add `_show_other_earnings` function**

Add before `_show_year_audit`:

```python
def _show_other_earnings(stubs: list[StubData]) -> None:
    """Display non-reconciled earnings lines (corrections, CME stipends, bonuses)."""
    other_lines = [
        {
            "Advice Date": s.advice_date.strftime("%b %d, %Y") if s.advice_date else "—",
            "Description": e.description,
            "Hours": f"{e.hours:.2f}" if e.hours else "—",
            "Amount": f"${e.current_amt:,.2f}",
            "YTD": f"${e.ytd_amt:,.2f}",
        }
        for s in stubs
        for e in s.earnings
        if e.category == "other" and e.current_amt != 0.0
    ]
    if not other_lines:
        st.caption("No other earnings on uploaded stubs.")
        return
    total = sum(
        e.current_amt
        for s in stubs for e in s.earnings
        if e.category == "other"
    )
    st.metric("Total Other Earnings", f"${total:,.2f}")
    st.dataframe(
        pd.DataFrame(other_lines),
        hide_index=True,
        use_container_width=True,
    )
```

- [ ] **Step 2: Update `_show_year_audit` signature and call `_show_other_earnings`**

Find the `_show_year_audit` function definition and update its signature:

```python
def _show_year_audit(results: list[PeriodResult], cfg: PayConfig, user: dict) -> None:
```

At the end of `_show_year_audit`, after the existing `st.tabs(...)` block, add:

```python
    if stubs:
        st.divider()
        st.subheader("💰 Other Earnings")
        st.caption(
            "Pay corrections, CME stipends, bonuses, and other non-reconciled lines. "
            "Displayed for tracking only — no engine comparison."
        )
        _show_other_earnings(stubs)
```

(The `stubs` variable is already defined earlier in `_show_year_audit` from the PDF upload step.)

- [ ] **Step 3: Manual smoke test**

Start app, log in, go to Year Audit, upload `paystubs.pdf`. Confirm the "Other Earnings" section appears below the three audit sub-tabs showing the lump sum / CME lines. Stop server.

- [ ] **Step 4: Commit**

```bash
cd ~/pay-reconciliation && git add app.py && git commit -m "feat: add Other Earnings section to stub audit (corrections, CME, bonuses)"
```

---

## Task 10: `app.py` — PTO auto-update gate

**Files:**
- Modify: `app.py`

- [ ] **Step 1: Add `_show_pto_update_gate` function**

Add before `_show_year_audit`:

```python
def _show_pto_update_gate(
    stubs: list[StubData],
    cfg: PayConfig,
    user: dict,
) -> None:
    """
    After stub upload: run PTO audit on the most recent stub.
    If clean, offer to update stored PTO balance. If discrepant, block and flag.
    """
    from reconcile_stubs import audit_pto

    if not stubs:
        return

    latest_stub = max(stubs, key=lambda s: s.advice_date or date.min)
    if latest_stub.pto_balance <= 0:
        return

    pto_rows = audit_pto([latest_stub], cfg)
    discrepant_weeks = [
        w for r in pto_rows for w in r.weeks
        if abs(w["diff"]) > 0.5
    ]

    st.divider()
    st.subheader("📋 PTO Balance Update")
    st.caption(
        f"Most recent stub: **{latest_stub.advice_date}**  |  "
        f"Reported balance: **{latest_stub.pto_balance:.2f}h**  |  "
        f"Stored balance: **{user['pto_balance']:.2f}h**"
    )

    if not discrepant_weeks:
        st.success("PTO audit clean — no discrepancies in the most recent stub.")
        if abs(latest_stub.pto_balance - float(user["pto_balance"])) > 0.1:
            if st.button(
                f"Update stored balance to {latest_stub.pto_balance:.2f}h",
                use_container_width=True,
            ):
                ok, err = auth.update_settings(
                    user["id"], {"pto_balance": latest_stub.pto_balance}
                )
                if ok:
                    st.session_state["user"]["pto_balance"] = latest_stub.pto_balance
                    st.success(
                        f"PTO balance updated to {latest_stub.pto_balance:.2f}h."
                    )
                else:
                    st.error(f"Update failed: {err}")
        else:
            st.caption("Stored balance already matches stub — no update needed.")
    else:
        st.warning(
            f"PTO discrepancy detected in {len(discrepant_weeks)} week(s) — "
            "balance not updated automatically. Review the PTO Audit tab."
        )
        if st.checkbox("I've reviewed the discrepancy and want to override anyway"):
            if st.button(
                f"Force update to {latest_stub.pto_balance:.2f}h",
                type="primary",
                use_container_width=True,
            ):
                ok, err = auth.update_settings(
                    user["id"], {"pto_balance": latest_stub.pto_balance}
                )
                if ok:
                    st.session_state["user"]["pto_balance"] = latest_stub.pto_balance
                    st.success(
                        f"PTO balance updated to {latest_stub.pto_balance:.2f}h."
                    )
                else:
                    st.error(f"Update failed: {err}")
```

- [ ] **Step 2: Call `_show_pto_update_gate` from `_show_year_audit`**

In `_show_year_audit`, after the `_show_other_earnings(stubs)` call, add:

```python
        _show_pto_update_gate(stubs, cfg, user)
```

- [ ] **Step 3: Manual smoke test**

Start app, log in, go to Year Audit, upload `paystubs.pdf`. Confirm:
- PTO Balance Update section appears at the bottom
- If most recent stub's PTO audit is clean, the update button appears
- Click the button → success message and stored balance updates (visible in Settings tab)
Stop server.

- [ ] **Step 4: Run all tests**

```bash
cd ~/pay-reconciliation && .venv/bin/pytest tests/ -v
```

Expected: 8 passed, 0 failed.

- [ ] **Step 5: Commit**

```bash
cd ~/pay-reconciliation && git add app.py && git commit -m "feat: PTO auto-update gate — blocks update on discrepancy, requires manual override"
```

---

## Task 11: GitHub private repo + Streamlit Cloud deployment

**Files:**
- No code changes — setup only

- [ ] **Step 1: Create a private GitHub repo**

On GitHub.com → New repository → name it `hhc-pay-recon` → set to **Private** → do not add README (you'll push existing code).

- [ ] **Step 2: Push the project**

```bash
cd ~/pay-reconciliation
git remote add origin https://github.com/YOUR_USERNAME/hhc-pay-recon.git
git branch -M main
git push -u origin main
```

Confirm: all commits push, repo is private on GitHub.

- [ ] **Step 3: Deploy to Streamlit Community Cloud**

Go to https://share.streamlit.io → New app → Connect GitHub → select `hhc-pay-recon` → branch `main` → main file `app.py` → click Deploy.

- [ ] **Step 4: Add secrets to Streamlit Cloud**

In the Streamlit Cloud app settings → Secrets, paste the full contents of your local `.streamlit/secrets.toml`:

```toml
[supabase]
url = "https://your-project-id.supabase.co"
service_role_key = "eyJ..."

[encryption]
fernet_key = "your-fernet-key"
```

Click Save. The app will redeploy automatically.

- [ ] **Step 5: Verify deployed app**

Open the Streamlit Cloud URL. Confirm:
- Login/register screen loads
- Register creates an account (check Supabase dashboard → Table Editor → users to confirm the row appears)
- Login works and the main Schedule tab loads with live ShiftAdmin data
- Settings tab allows editing fields
- Year Audit PDF upload works

- [ ] **Step 6: Share the URL**

The Streamlit Cloud URL (e.g. `https://hhc-pay-recon.streamlit.app`) is the link you share with colleagues. Each provider registers their own account on first visit.
