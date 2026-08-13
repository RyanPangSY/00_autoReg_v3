# AutoReg v3 - Inno Wing Machine Room Booking Bot

Browserless booking bot for the HKU Inno Wing machine room
(YouCanBook.me). Automates the **CNC milling machine**, **Water jet cutting
machine** and **Lathe machine** booking pages via their public API - no
Selenium-driven page automation for the booking itself.

The only browser used is a short-lived Chrome window that mints the
reCAPTCHA Enterprise token (a plain HTTP token gets a too-low score and the
server rejects it with `ycbm_api_booking_captcha_failed`).

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

Then copy the user details template and fill in your own:

```powershell
Copy-Item data\userInfo.example.txt data\userInfo.txt
```

`userInfo.txt` is git-ignored (it contains personal data). Format:

```text
Last Name: Pang
First Name: Ryan
Phone Number: 61389189
Email: roboconwaterjet@gmail.com
Content: Robocon
```

Optional keys: `Project` (must match an option of the Q5 dropdown, defaults
to the Robocon SIG entry), `Member Information` (optional text field).

## Usage

```powershell
.\.venv\Scripts\python.exe main.py
```

Opens the web UI in your browser: pick the machine, click the green days
(they show the number of free slots), optionally tick "Book ALL available
slots per date", then Register. Bookings require **at least 1 hour notice**
and are limited to a **14-day window** (site configuration).

### Flags

| Flag | Meaning |
| --- | --- |
| `--cli` | Terminal flow instead of the web UI |
| `--dry-run` | Run everything except the final confirm (no booking) |
| `--all-slots` | Default to booking every free slot per date |
| `--token-method {browser,browserless,auto}` | Token minting method (default `browser`) |
| `--headless` | Mint tokens in headless Chrome (may be rejected) |
| `--no-fingerprint` | Do not send `data/fingerprint.json` in browserless mode |
| `--port N` | Web UI port (default 5000) |
| `--no-browser` | Don't auto-open the browser |
| `-d` | Debug logging |

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
  webui.py              Flask server + job manager
  templates/index.html  calendar UI (single file, no build step)
  static/favicon.ico
data/                   userInfo.txt (git-ignored) + fingerprint.json
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
