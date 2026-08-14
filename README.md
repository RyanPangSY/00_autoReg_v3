# AutoReg v3 - Inno Wing Machine Room Booking Bot

Browserless booking bot for the HKU Inno Wing machine room (YouCanBook.me).
Automates the **CNC milling machine**, **Water jet cutting machine** and
**Lathe machine** booking pages via their public API - no Selenium-driven
page automation for the booking itself.

The only browser used is a short-lived Chrome window that mints the
reCAPTCHA Enterprise token (a plain HTTP token gets a too-low score and the
server rejects it with `ycbm_api_booking_captcha_failed`).

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

## Accounts

Every user has their own account with their own booking details (name,
phone, email, project, ...). The **first registered user becomes the admin**.

* **On the web**: open `http://127.0.0.1:5000/register` and fill the form.
  If the store is empty the form is pre-filled from the old
  `data/userInfo.txt` (if present).
* **From the terminal** (same thing):

  ```powershell
  .\.venv\Scripts\python.exe main.py --add-user alice   # prompts for password + details
  .\.venv\Scripts\python.exe main.py --list-users
  .\.venv\Scripts\python.exe main.py --set-admin alice
  .\.venv\Scripts\python.exe main.py --remove-user alice
  ```

`data/users.json` stores the accounts (git-ignored, passwords hashed).

## Usage

```powershell
.\.venv\Scripts\python.exe main.py
```

Opens the web UI in your browser. Log in (or register), pick the machine,
click the green days (they show the number of free slots), optionally tick
"Book ALL available slots per date", then Register. Bookings require
**at least 1 hour notice** and a **14-day window** (site configuration).

While a job runs, the status panel streams per-slot results. Every attempt
is recorded in `data/autoreg.db` (status BOOKED / FAILED / DRY_RUN), and
your **"My bookings" panel** shows the number of days and bookings you've
made. The admin can see everyone's totals at `GET /api/admin/stats` (or
`main.py --stats` in the terminal).

## Run in the background at every logon

```powershell
.\.venv\Scripts\python.exe main.py --install-startup
```

This adds a hidden `AutoReg.vbs` to the Windows Startup folder (no admin
needed) so the server starts at every logon on port 5000, logging to
`data/autoreg.log`. A desktop shortcut **AutoReg.url** opens the UI. Remove
it with `--uninstall-startup`. The pidfile `data/server.pid` prevents a
second instance from starting.

## Flags

| Flag | Meaning |
| --- | --- |
| `--cli` | Terminal booking flow instead of the web UI |
| `--dry-run` | Run everything except the final confirm (no booking) |
| `--all-slots` | Default to booking every free slot per date |
| `--token-method {browser,browserless,auto}` | Token minting method (default `browser`) |
| `--headless` | Mint tokens in headless Chrome (may be rejected) |
| `--no-fingerprint` | Do not send `data/fingerprint.json` in browserless mode |
| `--port N` | Web UI port (default 5000) |
| `--no-browser` | Don't auto-open the browser |
| `-d` | Debug logging |
| `--add-user`, `--list-users`, `--remove-user`, `--set-admin` | User management |
| `--stats` | Print per-user booking stats |
| `--install-startup` / `--uninstall-startup` | Auto-start at logon |
| `--background` | Hidden background mode (used by the auto-start entry) |

## How it works

1. `POST /v1/intents` creates a fresh booking intent (falls back to
   `/v2/intents` on `V2_REQUIRED`).
2. `GET /v1/intents/{id}/availabilitykey?startSearchAt=YYYY-MM-DD` then
   `GET /v1/availabilities/{key}` lists the free slots.
3. `PATCH /v1/intents/{id}/selections` stores the slot, timezone and form
   answers (`Q5` project is resolved against the server's option list;
   `Q8` terms checkbox is answered `"yes"`).
4. A reCAPTCHA Enterprise token with action `BOOKING_CREATE` is minted
   (visible Chrome by default) and sent with
   `PATCH /v1/intents/{id}/confirm {captcha}`.

Retries on captcha/slot/intent errors use a fresh intent each time.

## Folder structure

```text
main.py                 entry point (web UI by default, --cli for terminal)
autoreg/                application package
  booking.py            orchestration: availability, tokens, booking, retries
  youcanbook.py         YouCanBook.me API client (v1 + v2 intents)
  webui.py              Flask server: auth, job manager, stats API
  users.py              account store (data/users.json, hashed passwords)
  stats.py              SQLite booking history + per-user stats
  templates/            index.html (calendar), login.html, register.html
  static/favicon.ico
data/                   userInfo.txt, users.json, autoreg.db, secret.key,
                        server.pid, autoreg.log (all git-ignored except
                        fingerprint.json + userInfo.example.txt)
BypassV3/               vendored reCAPTCHA v3/Enterprise research client
```

## BypassV3

`BypassV3/` is vendored from
[https://github.com/EvickaStudio/BypassV3](https://github.com/EvickaStudio/BypassV3)
(AGPLv3 - see `BypassV3/LICENSE`). Its nested `.git` was removed so the
files are tracked in this repository. One local fix was applied to
`BypassV3/bypass.py`: `_encode_origin` now emits the unpadded base64 form
Google expects - padded `co` values make the anchor return
"Invalid domain for site key".

## Disclaimer

For educational / personal use. Book responsibly and follow the facility's
booking policies.
