# Multi-User Hosted Pay Reconciliation App

**Date:** 2026-07-09
**Status:** Approved

## Goal

Host the pay reconciliation app on Streamlit Community Cloud so providers in the group can use it from any device without installing Python. Each provider logs in with last name + password and gets a fully personalized experience: their schedule, their pay rules, their PTO projection, and their stub audit.

---

## Stack

| Layer | Service | Cost |
|---|---|---|
| App hosting | Streamlit Community Cloud | Free |
| Database + auth | Supabase (PostgreSQL) | Free tier |
| Source control | Private GitHub repo | Free |
| Encryption key storage | Streamlit Cloud secrets | Free |

---

## Per-User Data (stored in Supabase)

| Field | Type | Notes |
|---|---|---|
| `last_name` | text | Login username (case-insensitive) |
| `password_hash` | text | bcrypt |
| `base_rate` | float | $/hr regular |
| `tenure_bracket` | enum | `<5`, `5-10`, `11-20`, `21+` |
| `pto_balance` | float | Current verified balance in hours |
| `ics_url` | text | ShiftAdmin URL, Fernet-encrypted at application level |
| `created_at` | timestamp | |
| `updated_at` | timestamp | Updated on any settings change |

**Accrual rates by tenure bracket (EXEMPT, 40h standard):**

| Bracket | Accrual/period |
|---|---|
| < 5 years | 9.231h |
| 5–10 years | 10.769h |
| 11–20 years | 11.385h |
| 21+ years | 12.000h |

The accrual rate is derived from the bracket at runtime — not stored as a separate field.

---

## Security

- Passwords hashed with bcrypt before storage; never stored in plaintext
- ICS URL encrypted with Fernet (symmetric) before writing to Supabase; decrypted in-memory only during the session
- Fernet key stored in Streamlit Cloud secrets (`st.secrets`); never in the repo
- Supabase Row Level Security (RLS) enabled: each user can only read/write their own row
- HTTPS enforced by Streamlit Community Cloud

---

## Shared Config (not per-user)

Stored in `config.toml` (committed to repo) and Streamlit secrets where sensitive:

- Differential rates: evening $8.40, night $11.00, weekend $11.00
- OT differential rates: OT-evening $8.40, OT-weekend $14.00
- Admin hours: 4.0h/week
- OT threshold: 80h/biweekly period
- PTO standard weekly hours: 36h
- Pay period anchor date
- `local_tz`: America/New_York

Supabase connection credentials (URL + anon key) stored in Streamlit secrets.

---

## App Flow

### New user (first visit)
1. Landing page shows two tabs: **Login** and **Register**
2. Register: enter last name, choose password, enter base rate, select tenure bracket, enter current PTO balance, paste ShiftAdmin ICS URL
3. On submit: password is bcrypt-hashed, ICS URL is Fernet-encrypted, row written to Supabase
4. Redirect to main app

### Returning user (any device)
1. Landing page → Login tab
2. Enter last name + password
3. App fetches their row from Supabase, decrypts ICS URL in memory
4. All tabs load with their personal config

### Session state
During a session, decrypted settings are held in `st.session_state`. Nothing sensitive is written to disk or logs. Closing the browser clears the session; next visit requires login.

---

## App Tabs (post-login)

### Schedule
- Fetches live ICS feed using their stored URL
- Displays shifts for the current and upcoming pay periods
- Breaks down evening / night / weekend hours per shift and per week

### PTO Projection
- Uses their stored PTO balance and accrual rate
- Projects forward through the end of the next quarter (or user-selected horizon)
- Highlights year-end rollover cap risk

### Differential Check
- Schedule-based audit: compares ICS-derived hours to pay rules
- Shows expected vs. paid differentials for each category
- Flags gaps

### Stub Audit (PDF upload)
- User uploads one or more pay stub PDFs
- Parser extracts earnings lines and classifies them into two groups:

**Reconciled earnings** (engine comparison runs):
- Base, regular, over-standard
- Evening, night, weekend differentials
- OT differentials
- Holiday
- PTO
- Education shift hours (hours tracked, no dollar engine comparison — just displayed)

**Other earnings** (display only, no reconciliation):
- Pay corrections / lump sums
- CME stipends / educational stipend payments (dollar amounts, not shift hours)
- Quarterly bonuses
- Any line the parser cannot classify into a reconciled category

The distinction: education *hours* (shift time worked on education days) are reconciled-section; CME *stipend payments* (lump dollar amounts) are other-earnings.

Other earnings are shown in a separate "Other Earnings" section with description, hours (if applicable), current-period amount, and YTD amount. No engine comparison is run.

### PTO Balance Update Rule
After a stub is parsed, the PTO audit runs automatically:
- If all weeks in the period match the 36h rule (no discrepancy), the app offers to update the stored PTO balance with the stub's reported balance. User confirms.
- If any week has a discrepancy, the balance does **not** update automatically. The app shows the flag and allows a manual override with explicit confirmation.

### Settings
- Edit base rate, tenure bracket, PTO balance, ShiftAdmin ICS URL
- Password change (requires current password)
- Changes write to Supabase immediately

---

## Code Changes

### New file: `auth.py`
- Supabase client initialization
- `register(last_name, password, settings) → bool`
- `login(last_name, password) → user_row | None`
- `update_settings(user_id, fields) → bool`
- `encrypt_ics_url(url) → str` / `decrypt_ics_url(ciphertext) → str` (Fernet)
- `hash_password(pw) → str` / `verify_password(pw, hash) → bool` (bcrypt)

### Modified: `schedule.py`
- `load_all_shifts(ics_url: str)` — accept URL as parameter instead of reading from config
- Remove all direct `config.toml` reads for ICS URL

### Modified: `pay_rules.py`
- `PayConfig.load(base_rate: float | None, accrual_per_period: float | None)` — accept overrides
- If overrides provided, use them; otherwise fall back to config.toml defaults

### Modified: `app.py`
- Add login/register screen (shown when `st.session_state` has no authenticated user)
- Gate all existing tabs behind auth check
- Pass per-user `base_rate` and `accrual_per_period` into `PayConfig`
- Pass per-user `ics_url` into `load_all_shifts()`
- Add Settings tab
- Add "Other Earnings" display section in Stub Audit tab
- Implement PTO auto-update logic with discrepancy gate

### Modified: `config.toml`
- Remove `ics_url`, `base_rate` (now per-user in Supabase)
- Keep all shared pay rules

### New file: `requirements.txt`
- All existing dependencies (streamlit, pdfplumber, icalendar, pytz, toml, etc.)
- Add: `supabase`, `cryptography`, `bcrypt`

---

## Pay Period and Holiday Continuity

The biweekly pay period anchor (Dec 14, 2025) and HHC holiday schedule carry forward into 2027 and beyond unless explicitly updated. No year-boundary logic change is needed — the engine already generates pay periods indefinitely from the anchor date. Holiday dates are the only thing that require annual update (HHC publishes the next year's holiday schedule); when that happens, `config.toml` is the single place to add them.

---

## Out of Scope (for this phase)

- Admin dashboard for managing all users
- Email-based password reset (users contact you directly if locked out)
- Audit log of stub uploads
- Mobile-optimized layout
