"""Booking orchestration: availability, reCAPTCHA token, and the full booking flow.

Depends on the vendored BypassV3 reCAPTCHA v3/Enterprise client for token
generation (action "BOOKING_CREATE", the action the site binds at generation
time) and on youcanbook.py for the API calls. No Selenium.
"""
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone

from .youcanbook import (
    CaptchaFailedError,
    IntentError,
    UnavailableSlotError,
    YCBMError,
    YouCanBookClient,
)
from BypassV3.bypass import ReCaptchaV3Bypass

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("booking")

DEFAULT_SUBDOMAIN = "innowingcncmilling"
# Each machine is its own booking page; the page's appointment types are
# disabled (configuration.appointmentTypes.active == false), so the machine
# is chosen by subdomain and no appointmentTypeIds selection is sent.
EQUIPMENT_SUBDOMAINS = (
    "innowingcncmilling",  # CNC milling machine
    "innowingwaterjet",    # Water jet cutting machine
    "innowinglathe",       # Lathe machine
)
CAPTCHA_SITE_KEY = "6LeEb08aAAAAAEr4SO4bLfUzPEG7CAUBczL80_qX"
CAPTCHA_ORIGIN = f"https://{DEFAULT_SUBDOMAIN}.youcanbook.me"
CAPTCHA_ACTION = "BOOKING_CREATE"
TIME_ZONE = "Asia/Hong_Kong"
HKT = timezone(timedelta(hours=8))
MIN_NOTICE_SECONDS = 3600  # config: times.minNotice = PT1H
AVAILABILITY_WINDOW_DAYS = 14  # config: times.maxNotice = PT336H

DEFAULT_PROJECT = "SIG - HKU Robocon (Supervisor: Dr. H.H. Cheung, IMSE)"


class NoSlotsAvailable(Exception):
    pass


class BookingError(Exception):
    pass


class TokenError(Exception):
    pass


# -- user info ---------------------------------------------------------------

def load_user_info(path):
    """Parse userInfo.txt (same format as v1): 'Key: Value' per line."""
    info = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                key, _, value = line.partition(":")
                info[key.strip()] = value.strip()
    except FileNotFoundError:
        raise BookingError(f"userInfo.txt not found at {path}")
    return info


def build_form(user_info, context):
    """Map userInfo.txt onto the live form questions.

    Q5 is a MULTI_DROPDOWN whose answer must be one of the server-provided
    option strings (confirmed against the live context); Q8 is a CHECKBOX
    whose answer is the literal string "yes".
    """
    questions = context.get("form", {}).get("questions", []) or []
    by_code = {q.get("code"): q for q in questions}
    form = []

    q5 = by_code.get("Q5")
    if q5 is not None:
        options = q5.get("options") or []
        project = user_info.get("Project") or DEFAULT_PROJECT
        value = None
        if project in options:
            value = project
        else:
            matches = [opt for opt in options if project.lower() in opt.lower()]
            if matches:
                value = matches[0]
        if value is None:
            raise BookingError(
                f"Project {project!r} is not an option of Q5. Available: {options}"
            )
        form.append({"id": "Q5", "value": value})

    for code, key in (
        ("LNAME", "Last Name"),
        ("FNAME", "First Name"),
        ("PHONE", "Phone Number"),
        ("EMAIL", "Email"),
        ("JOB", "Content"),
    ):
        question = by_code.get(code)
        if question is None:
            raise BookingError(f"Question {code} not present in booking form")
        form.append({"id": code, "value": user_info.get(key, "")})

    text_q = next((q for q in questions if q.get("type") == "TEXT"), None)
    if text_q is not None and user_info.get("Member Information"):
        form.append({"id": text_q["code"], "value": user_info["Member Information"]})

    q8 = by_code.get("Q8")
    if q8 is not None and q8.get("type") == "CHECKBOX":
        form.append({"id": "Q8", "value": "yes"})

    return form


# -- reCAPTCHA ---------------------------------------------------------------

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)


def get_token(fingerprint_path=None, max_retries=3):
    """Fetch a reCAPTCHA Enterprise v3 token for the site's key (browserless).

    The site's client calls grecaptcha.enterprise.execute(siteKey,
    {action: "BOOKING_CREATE"}); the action is embedded in the protobuf
    reload body by BypassV3 and must match or the server rejects the token.
    """
    bypass = ReCaptchaV3Bypass.from_site_key(
        CAPTCHA_SITE_KEY,
        origin=CAPTCHA_ORIGIN,
        action=CAPTCHA_ACTION,
        enterprise=True,
        fingerprint_path=fingerprint_path,
        max_retries=max_retries,
        user_agent=CHROME_UA,
    )
    token = bypass.bypass()
    if not token:
        raise TokenError("could not generate a reCAPTCHA token after retries")
    return token


def _start_chrome(visible=False):
    """Start Chrome for token minting. Selenium 4.6+ manages chromedriver."""
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service

    options = webdriver.ChromeOptions()
    if not visible:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080")
    for arg in (
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-extensions",
        "--disable-logging",
    ):
        options.add_argument(arg)
    options.add_experimental_option("excludeSwitches", ["enable-logging"])
    return webdriver.Chrome(service=Service(), options=options)


def _mint_token_in_driver(driver, site_key, action):
    """Load the real booking page and execute grecaptcha.enterprise.execute().

    A token minted by a real Chrome session carries a genuine device
    fingerprint, so its score clears the server's threshold - the browserless
    client's tokens are rejected with ycbm_api_booking_captcha_failed.
    """
    from selenium.webdriver.support.wait import WebDriverWait

    driver.get(f"https://{DEFAULT_SUBDOMAIN}.youcanbook.me/")
    WebDriverWait(driver, 30).until(
        lambda d: d.execute_script(
            "return !!(window.grecaptcha && grecaptcha.enterprise "
            "&& typeof grecaptcha.enterprise.execute === 'function');"
        )
    )
    result = driver.execute_async_script(
        """
        var done = arguments[arguments.length - 1];
        var key = arguments[0], action = arguments[1];
        var execute = window.grecaptcha && window.grecaptcha.enterprise
            && grecaptcha.enterprise.execute;
        if (!execute) { done({error: 'grecaptcha.enterprise unavailable'}); return; }
        Promise.race([
            execute(key, {action: action}),
            new Promise(function (_, reject) {
                setTimeout(function () { reject(new Error('timeout')); }, 25000);
            })
        ]).then(
            function (t) { done({token: t}); },
            function (e) { done({error: String((e && e.message) || e)}); }
        );
        """,
        site_key,
        action,
    )
    if not isinstance(result, dict) or result.get("error"):
        raise TokenError(f"browser captcha failed: {result}")
    return result["token"]


class TokenMinter:
    """Mints reCAPTCHA Enterprise tokens for the confirm step.

    method='auto':        real Chrome first (high score), fall back to the
                          browserless client if Chrome is unavailable.
    method='browser':     real Chrome only.
    method='browserless': requests-only, no Chrome.
    The Chrome driver is kept alive across calls and reused for all dates.
    """

    def __init__(self, method="auto", fingerprint_path=None, browser_visible=False):
        self.method = method
        self.fingerprint_path = fingerprint_path
        self.browser_visible = browser_visible
        self._driver = None

    def mint(self):
        if self.method in ("auto", "browser"):
            try:
                return self._mint_browser()
            except TokenError:
                if self.method == "browser":
                    raise
                log.warning("Browser captcha failed, falling back to browserless token...")
        return get_token(fingerprint_path=self.fingerprint_path)

    def _mint_browser(self):
        if self._driver is None:
            self._driver = _start_chrome(visible=self.browser_visible)
        try:
            return _mint_token_in_driver(self._driver, CAPTCHA_SITE_KEY, CAPTCHA_ACTION)
        except Exception as e:
            self.close()
            raise TokenError(f"browser captcha failed: {e}") from e

    def close(self):
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None


# -- availability ------------------------------------------------------------

def slot_epoch_ms(slot):
    starts_at = slot.get("startsAt")
    if isinstance(starts_at, str):
        if starts_at.isdigit():
            return int(starts_at)
        dt = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    return int(starts_at)


def hk_date_of(epoch_ms):
    return datetime.fromtimestamp(epoch_ms / 1000, HKT).date()


def fetch_slots(client, target_date):
    """Free time slots (epoch ms, sorted) for target_date, respecting the
    minNotice rule and the server's own startSearchAt day filtering."""
    key = client.availability_key(target_date.isoformat())
    data = client.availabilities(key)
    min_ts = int(time.time() * 1000) + MIN_NOTICE_SECONDS * 1000
    slots = []
    for slot in data.get("slots", []):
        if int(slot.get("freeUnits", 0) or 0) < 1:
            continue
        ts = slot_epoch_ms(slot)
        if ts < min_ts:
            continue
        if hk_date_of(ts) != target_date:
            continue
        slots.append(ts)
    slots.sort()
    return slots


def availability_map(subdomain, days=AVAILABILITY_WINDOW_DAYS, counts=False):
    """Availability for the next `days` days on a machine's booking page.

    With counts=False: {date_iso: bool} (a day is available when at least one
    free slot remains). With counts=True: {date_iso: {"available": bool,
    "slots": n}} where n is the number of free slots on that day.
    """
    client = YouCanBookClient(subdomain)
    client.create_intent()
    today = datetime.now(HKT).date()
    result = {}
    for i in range(days):
        day = today + timedelta(days=i)
        slots = fetch_slots(client, day)
        if counts:
            result[day.isoformat()] = {"available": bool(slots), "slots": len(slots)}
        else:
            result[day.isoformat()] = bool(slots)
    return result


def list_equipment():
    """[(subdomain, machine_name)] resolved from each booking page's title."""
    result = []
    for sub in EQUIPMENT_SUBDOMAINS:
        try:
            client = YouCanBookClient(sub)
            client.create_intent()
            title = client.get_context().get("common_texts", {}).get("title")
            result.append((sub, title or sub))
        except YCBMError as e:
            log.warning("Could not load equipment %s: %s", sub, e)
    return result


# -- booking ------------------------------------------------------------------

def book_date(subdomain, target_date, user_info,
              minter=None, dry_run=False, max_duration=180, slot_ms=None):
    """Book a slot on target_date (datetime.date).

    With slot_ms=None the first free slot of the day is booked; with an
    explicit slot_ms that exact slot is booked, and if it was taken in the
    meantime the next still-free slot is picked instead. Retries with a
    fresh intent on intent/captcha/slot errors until max_duration seconds
    elapse. With dry_run=True it stops right before the confirm call (no
    booking is created).
    """
    minter = minter or TokenMinter(method="auto")
    deadline = time.time() + max_duration
    attempts = 0
    last_error = None
    wanted = slot_ms
    while time.time() < deadline:
        attempts += 1
        client = None
        try:
            client = YouCanBookClient(subdomain)
            client.create_intent()
            context = client.get_context()
            form = build_form(user_info, context)

            if wanted is None:
                slots = fetch_slots(client, target_date)
                if not slots:
                    raise NoSlotsAvailable(
                        f"no free slot on {target_date.isoformat()} "
                        f"(>= {MIN_NOTICE_SECONDS // 60} min notice)"
                    )
                wanted = slots[0]
            slot_time = datetime.fromtimestamp(wanted / 1000, HKT)
            log.info(
                "Intent %s: slot %s chosen (attempt %d)",
                client.intent_id, slot_time.isoformat(), attempts,
            )

            client.set_selection({"startsAt": wanted, "timeZone": TIME_ZONE})
            client.set_selection({"form": form})
            log.info("Intent %s: selections saved (slot + form)", client.intent_id)

            if dry_run:
                return {"dry_run": True, "intent_id": client.intent_id,
                        "startsAt": slot_time.isoformat(), "attempts": attempts}

            token = minter.mint()
            client.confirm(token)
            booking = client.get_booking()
            log.info("Intent %s: booking confirmed: %s", client.intent_id, booking)
            return {"dry_run": False, "intent_id": client.intent_id,
                    "startsAt": slot_time.isoformat(), "booking": booking,
                    "attempts": attempts}
        except NoSlotsAvailable:
            raise
        except UnavailableSlotError as e:
            # the wanted slot got taken while booking -> re-pick the first
            # still-free slot of the day on the next attempt
            last_error = e
            wanted = None
            log.warning("Attempt %d failed (%s), re-picking a free slot...", attempts, e)
            time.sleep(1)
        except (CaptchaFailedError, IntentError, TokenError, YCBMError) as e:
            last_error = e
            log.warning("Attempt %d failed (%s), retrying with a fresh intent...",
                        attempts, e)
            time.sleep(1)
        finally:
            if client is not None:
                client.session.close()

    raise BookingError(f"booking failed after {attempts} attempts: {last_error}")


def book_all_slots(subdomain, target_date, user_info,
                   minter=None, dry_run=False, max_duration=120, max_slots=None):
    """Book every free slot on target_date - one booking per slot.

    The slot list is snapshotted first (respecting the min-notice rule),
    then each slot is booked in turn. A slot that is taken before its turn
    is skipped; a slot that was taken mid-booking falls back to the next
    still-free slot. Returns the per-slot results.
    """
    minter = minter or TokenMinter(method="auto")
    probe = YouCanBookClient(subdomain)
    try:
        probe.create_intent()
        context = probe.get_context()
        build_form(user_info, context)  # fail fast if the form is wrong
        slots = fetch_slots(probe, target_date)
    finally:
        probe.session.close()
    if not slots:
        raise NoSlotsAvailable(
            f"no free slot on {target_date.isoformat()} "
            f"(>= {MIN_NOTICE_SECONDS // 60} min notice)"
        )
    if max_slots:
        slots = slots[:max_slots]

    results = []
    for slot_ms in slots:
        slot_time = datetime.fromtimestamp(slot_ms / 1000, HKT)
        log.info("Booking slot %s on %s...", slot_time.isoformat(), target_date.isoformat())
        try:
            result = book_date(subdomain, target_date, user_info,
                               minter=minter, dry_run=dry_run,
                               max_duration=max_duration, slot_ms=slot_ms)
            results.append(result)
        except NoSlotsAvailable as e:
            log.warning("Slot %s no longer available, skipping: %s",
                        slot_time.isoformat(), e)
        except Exception as e:
            log.error("Slot %s failed: %s", slot_time.isoformat(), e)
            results.append({"error": str(e), "startsAt": slot_time.isoformat()})
    return results


def book_dates(subdomain, dates, user_info,
               token_method="auto", fingerprint_path=None,
               dry_run=False, debug=False, browser_visible=False,
               all_slots=False, max_slots=None):
    results = []
    minter = TokenMinter(method=token_method, fingerprint_path=fingerprint_path,
                         browser_visible=browser_visible)
    try:
        for target in dates:
            if debug:
                log.setLevel(logging.DEBUG)
            log.info("=== Booking %s on %s ===", target.isoformat(), subdomain)
            try:
                if all_slots:
                    detail = book_all_slots(subdomain, target, user_info,
                                            minter=minter, dry_run=dry_run,
                                            max_slots=max_slots)
                    ok = any("error" not in r for r in detail)
                    results.append((target, "OK" if ok else "FAILED", detail))
                else:
                    result = book_date(subdomain, target, user_info,
                                       minter=minter, dry_run=dry_run)
                    results.append((target, "OK", result))
            except Exception as e:
                log.error("Booking %s failed: %s", target.isoformat(), e)
                results.append((target, "FAILED", str(e)))
    finally:
        minter.close()
    return results


def format_results(results, dry_run=False):
    """One printable line per booked date / slot (shared by CLI and GUI)."""
    lines = []
    for target, status, detail in results:
        if status == "FAILED":
            if isinstance(detail, list):
                ok = [r for r in detail if "error" not in r]
                lines.append(f"{target.isoformat()}: {len(ok)} slot(s) booked")
                for r in detail:
                    if "error" in r:
                        lines.append(f"  {r.get('startsAt', '?'): <25}: {r['error']}")
            else:
                lines.append(f"{target.isoformat()}: {detail}")
            continue
        if isinstance(detail, list):
            ok = [r for r in detail if "error" not in r]
            lines.append(f"{target.isoformat()}: {len(ok)} slot(s) booked")
            for r in detail:
                if "error" in r:
                    lines.append(f"  {r.get('startsAt', '?'): <25}: {r['error']}")
                else:
                    suffix = " (dry-run, no booking created)" if dry_run else ""
                    lines.append(f"  {r.get('startsAt', '?'): <25}: BOOKED{suffix}")
        else:
            suffix = " (dry-run, no booking created)" if dry_run else ""
            lines.append(f"{target.isoformat()}: BOOKED {detail.get('startsAt', '?')}{suffix}")
    return lines


# -- CLI ----------------------------------------------------------------------

def parse_date(value):
    """Accept 'YYMMDD', 'YYYYMMDD' or 'YYYY-MM-DD' -> datetime.date.

    e.g. '260815' or '2026-08-15' -> 2026-08-15.
    """
    s = str(value).strip()
    if "-" in s:
        s = s.replace("-", "")
    if len(s) == 6 and s.isdigit():
        year, month, day = 2000 + int(s[0:2]), int(s[2:4]), int(s[4:6])
    elif len(s) == 8 and s.isdigit():
        year, month, day = int(s[0:4]), int(s[4:6]), int(s[6:8])
    else:
        raise BookingError(f"invalid date {value!r} (expected YYMMDD or YYYY-MM-DD)")
    try:
        return date(year, month, day)
    except ValueError as e:
        raise BookingError(f"invalid date {value!r}: {e}") from e


def cli_book(user_info, fingerprint_path=None, dry_run=False, debug=False,
             token_method="auto", browser_visible=False, all_slots=False):
    equipment = list_equipment()
    if not equipment:
        raise BookingError("cannot load equipment list")

    print("Available equipment:")
    for i, (_sub, name) in enumerate(equipment):
        print(f"  [{i}]: {name}")
    choice = None
    while choice is None:
        raw = input("Equipment: ").strip()
        try:
            choice = int(raw)
            equipment[choice]
        except (ValueError, IndexError):
            choice = None

    raw_dates = input("Dates (comma-separated, YYMMDD e.g. 260815,260816): ")
    dates = [parse_date(d) for d in raw_dates.split(",") if d.strip()]

    results = book_dates(
        equipment[choice][0], dates, user_info,
        token_method=token_method, fingerprint_path=fingerprint_path,
        dry_run=dry_run, debug=debug, browser_visible=browser_visible,
        all_slots=all_slots,
    )
    print("\n=== Results ===")
    for line in format_results(results, dry_run=dry_run):
        print(f"  {line}")
    return results
