"""Browserless client for the YouCanBook.me booking API (v1 + v2 intents).

Implements the same flow as the site's JS bundle (assets/index-*.js):

    POST  /v1/intents                         create intent (V2_REQUIRED -> /v2/intents)
    GET   /v1/intents/{id}/context            page config: appointment types, form, captcha
    GET   /v1/intents/{id}/availabilitykey    -> {key}   (v2: /availability_key?startSearchOn=)
    GET   /v1/availabilities/{key}            -> {slots} (v2: /v2/availabilities/{key})
    PATCH /v1/intents/{id}/selections         set type / slot / form answers
    PATCH /v1/intents/{id}/confirm {captcha}  create the booking (v2: POST .../confirm {captchaResponse})

No cookies / auth are required: requests only carry the same pseudo-random
session headers the site's own client generates.
"""
import random
import string
from datetime import datetime, timezone

import requests

API_BASE = "https://api.youcanbook.me"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

CAPTCHA_FAILED_CODES = ("CAPTCHA_FAILED", "CAPTCHA_MISSING", "CAPTCHA_SCORE_TOO_LOW")
SLOT_ERROR_CODES = ("UNAVAILABLE_TIME_SLOT", "NOT_ENOUGH_UNITS", "MEMBER_NOT_AVAILABLE")
INTENT_ERROR_CODES = ("INTENT_NOT_FOUND", "INTENT_EXPIRED", "INTENT_ALREADY_CONFIRMED")


def _rand7():
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(7))


class YCBMError(Exception):
    def __init__(self, code, message, http_status=None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.http_status = http_status


class CaptchaFailedError(YCBMError):
    pass


class UnavailableSlotError(YCBMError):
    pass


class IntentError(YCBMError):
    pass


class YouCanBookClient:
    """A single intent session bound to one YouCanBook.me subdomain."""

    def __init__(self, subdomain, session=None):
        self.subdomain = subdomain
        self.v2 = False
        self.intent_id = None
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "content-type": "application/json",
                "User-Agent": USER_AGENT,
                "X-Browser-Id": _rand7(),
                "X-Session-Id": _rand7(),
            }
        )

    @property
    def _v(self):
        return 2 if self.v2 else 1

    # -- low level -----------------------------------------------------------

    def _request(self, method, path, payload=None):
        headers = {"X-Request-Id": "BA-" + _rand7()}
        try:
            resp = self.session.request(
                method,
                API_BASE + path,
                json=payload,
                headers=headers,
                timeout=30,
            )
        except requests.exceptions.RequestException as e:
            raise YCBMError("REQUEST_ERROR", str(e)) from e
        if resp.status_code >= 400:
            raise self._error_from(resp)
        if not resp.content:
            return {}
        return resp.json()

    @staticmethod
    def _error_from(resp):
        code = f"http_{resp.status_code}"
        message = resp.text[:500]
        try:
            body = resp.json()
            code = body.get("code") or code
            message = body.get("message") or message
        except ValueError:
            pass
        if code in CAPTCHA_FAILED_CODES:
            return CaptchaFailedError(code, message, resp.status_code)
        if code in SLOT_ERROR_CODES:
            return UnavailableSlotError(code, message, resp.status_code)
        if code in INTENT_ERROR_CODES or resp.status_code == 404:
            return IntentError(code, message, resp.status_code)
        return YCBMError(code, message, resp.status_code)

    # -- intent lifecycle ----------------------------------------------------

    def create_intent(self):
        """Create a fresh booking intent; follows V2_REQUIRED to /v2/intents."""
        path = "/v2/intents" if self.v2 else "/v1/intents"
        body = {"slug": self.subdomain} if self.v2 else {"subdomain": self.subdomain}
        try:
            data = self._request("POST", path, body)
        except YCBMError as e:
            if not self.v2 and e.code == "V2_REQUIRED":
                self.v2 = True
                return self.create_intent()
            raise
        if isinstance(data, dict) and data.get("data") and isinstance(data["data"], dict):
            data = data["data"]
        self.intent_id = data.get("id")
        if not self.intent_id:
            raise YCBMError("NO_INTENT_ID", f"intent creation returned no id: {data}")
        return data

    def get_intent(self):
        return self._request("GET", f"/v{self._v}/intents/{self.intent_id}")

    def get_context(self):
        return self._request("GET", f"/v{self._v}/intents/{self.intent_id}/context")

    def get_booking(self):
        return self._request("GET", f"/v{self._v}/intents/{self.intent_id}/booking")

    # -- availability ---------------------------------------------------------

    def availability_key(self, date_iso):
        """date_iso: 'YYYY-MM-DD' (UTC day string, same as the site's client)."""
        param = "startSearchAt" if not self.v2 else "startSearchOn"
        path = f"/v{self._v}/intents/{self.intent_id}/availability{'_key' if self.v2 else 'key'}?{param}={date_iso}"
        data = self._request("GET", path)
        return data.get("key")

    def availabilities(self, key):
        suffix = "?includeBusySlots=false" if self.v2 else ""
        return self._request("GET", f"/v{self._v}/availabilities/{key}{suffix}")

    # -- selections ------------------------------------------------------------

    def set_selection(self, payload):
        """PATCH intent selections. Payload uses the v1 shape:
        {appointmentTypeIds, startsAt(ms), form:[{id,value}], timeZone, units, ...}
        Converted to the v2 shape automatically when the intent is v2.
        """
        if self.v2:
            payload = self._to_v2(payload)
        return self._request("PATCH", f"/v{self._v}/intents/{self.intent_id}/selections", payload)

    @staticmethod
    def _to_v2(payload):
        out = {}
        for key, value in payload.items():
            if key == "teamMemberId" and value is not None:
                out["userProfileIds"] = [value]
            elif key == "startsAt" and isinstance(value, (int, float)):
                out["startsAt"] = datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()
            elif key == "form":
                out["form"] = [
                    {"questionCode": item["id"], "value": item.get("value")} for item in value
                ]
            else:
                out[key] = value
        return out

    # -- booking ---------------------------------------------------------------

    def confirm(self, captcha_token):
        """Finalize the booking. Requires a reCAPTCHA Enterprise token."""
        if self.v2:
            return self._request(
                "POST",
                f"/v2/intents/{self.intent_id}/confirm",
                {"captchaResponse": captcha_token},
            )
        return self._request(
            "PATCH",
            f"/v1/intents/{self.intent_id}/confirm",
            {"captcha": captcha_token},
        )
