"""Local Flask web UI for AutoReg v3.

Serves a calendar UI at http://127.0.0.1:<port>/ with user registration /
login. Each booking job runs with the logged-in user's details and every
per-slot attempt is recorded in the stats database.
"""
import logging
import threading
import time
import uuid
from functools import wraps

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from .booking import (
    TokenMinter,
    availability_map,
    book_all_slots,
    book_date,
    format_results,
    list_equipment,
    parse_date,
)
from .users import UserExistsError, UserStore

log = logging.getLogger("webui")

app = Flask(__name__)

VALIDATION = {
    "username": lambda v: 3 <= len(v) <= 32 and v.replace("_", "").replace("-", "").isalnum(),
    "password": lambda v: len(v) >= 6,
    "email": lambda v: "@" in v and "." in v,
    "phone": lambda v: v.replace("+", "").replace(" ", "").isdigit() and 7 <= len(v.replace("+", "").replace(" ", "")) <= 15,
    "content": lambda v: len(v.strip()) > 0,
}
VALIDATION_MESSAGES = {
    "username": "Username must be 3-32 characters (letters, digits, _ or -).",
    "password": "Password must be at least 6 characters.",
    "email": "Enter a valid email address.",
    "phone": "Enter a valid phone number (digits, optional + prefix).",
    "content": "Content / job description is required.",
}


class JobManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._jobs = {}

    def start(self, subdomain, dates, user_info, token_method, fingerprint_path,
              browser_visible, dry_run, all_slots, username, stats_db):
        with self._lock:
            active = [j for j in self._jobs.values() if j["state"] == "running"]
            if active:
                return None, f"A registration is already running (job {active[0]['id']})"
            job_id = uuid.uuid4().hex[:8]
        job = {
            "id": job_id,
            "state": "running",
            "done": 0,
            "total": len(dates),
            "lines": [],
            "results": [],
        }
        with self._lock:
            self._jobs[job_id] = job

        def worker():
            minter = TokenMinter(method=token_method, fingerprint_path=fingerprint_path,
                                 browser_visible=browser_visible)
            try:
                for i, target in enumerate(dates):
                    log.info("=== Booking %s on %s ===", target.isoformat(), subdomain)
                    try:
                        if all_slots:
                            detail = book_all_slots(subdomain, target, user_info,
                                                    minter=minter, dry_run=dry_run)
                            status = "OK" if any("error" not in r for r in detail) else "FAILED"
                        else:
                            detail = book_date(subdomain, target, user_info,
                                               minter=minter, dry_run=dry_run)
                            status = "OK"
                        job["results"].append((target, status, detail))
                        _record_stats(stats_db, username, subdomain, dry_run, detail)
                    except Exception as e:
                        log.error("Booking %s failed: %s", target.isoformat(), e)
                        job["results"].append((target, "FAILED", str(e)))
                    job["done"] = i + 1
                    job["lines"] = format_results(job["results"], dry_run=dry_run)
            finally:
                minter.close()
                job["state"] = "done"

        threading.Thread(target=worker, daemon=True).start()
        return job_id, None

    def status(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return None
        return {
            "state": job["state"],
            "done": job["done"],
            "total": job["total"],
            "lines": job["lines"],
        }


def _record_stats(stats_db, username, machine, dry_run, detail):
    """Write one row per slot attempt into the stats database."""
    records = detail if isinstance(detail, list) else [detail]
    for r in records:
        if not isinstance(r, dict):
            continue
        if "error" in r:
            status = "FAILED"
        elif dry_run:
            status = "DRY_RUN"
        else:
            status = "BOOKED"
        stats_db.record(username, machine, r.get("startsAt"), status,
                        r.get("intent_id"))


jobs = JobManager()


# -- auth helpers -------------------------------------------------------------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "username" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "not logged in"}), 401
            return redirect(url_for("login_page"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not app.config["USERS"].is_admin(session["username"]):
            return jsonify({"error": "admin only"}), 403
        return view(*args, **kwargs)
    return wrapped


def _validate_registration(form):
    """Returns (user_fields, error_message)."""
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    fields = {}
    for label, key in (
        ("last_name", "Last Name"), ("first_name", "First Name"),
        ("phone", "Phone Number"), ("email", "Email"),
        ("content", "Content"), ("project", "Project"),
        ("member_info", "Member Information"),
    ):
        value = (form.get(key) or form.get(label) or "").strip()
        if key in ("Last Name", "First Name", "Project", "Member Information") and not value:
            value = None
        fields[USERINFO_TO_STORE[key]] = value or None

    if not VALIDATION["username"](username):
        return None, VALIDATION_MESSAGES["username"]
    if not VALIDATION["password"](password):
        return None, VALIDATION_MESSAGES["password"]
    if not VALIDATION["email"](fields["email"] or ""):
        return None, VALIDATION_MESSAGES["email"]
    if not VALIDATION["phone"](fields["phone"] or ""):
        return None, VALIDATION_MESSAGES["phone"]
    if not VALIDATION["content"](fields["content"] or ""):
        return None, VALIDATION_MESSAGES["content"]
    return fields, None


USERINFO_TO_STORE = {
    "Last Name": "last_name", "First Name": "first_name",
    "Phone Number": "phone", "Email": "email",
    "Content": "content", "Project": "project",
    "Member Information": "member_info",
}


# -- pages --------------------------------------------------------------------

@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if "username" in session:
        if not app.config["USERS"].user_exists(session["username"]):
            session.clear()
        else:
            return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if app.config["USERS"].verify(username, password):
            session["username"] = username
            return redirect(url_for("index"))
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/register", methods=["GET", "POST"])
def register_page():
    if "username" in session:
        return redirect(url_for("index"))
    error = None
    prefill = {}
    users = app.config["USERS"]
    if request.method == "POST":
        fields, error = _validate_registration(request.form)
        if fields is None:
            pass
        else:
            try:
                first_user = users.count() == 0
                users.add_user(
                    (request.form.get("username") or "").strip(),
                    request.form.get("password") or "",
                    **fields,
                    is_admin=first_user,
                )
                session["username"] = (request.form.get("username") or "").strip()
                return redirect(url_for("index"))
            except UserExistsError as e:
                error = str(e)
    elif users.count() == 0:
        prefill = _prefill_from_userinfo(app.config.get("USERINFO_LEGACY_PATH"))

    return render_template("register.html", error=error,
                           is_first_user=users.count() == 0, prefill=prefill)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


def _prefill_from_userinfo(path):
    """Seed the registration form from the retired data/userInfo.txt."""
    import os
    if not path or not os.path.exists(path):
        return {}
    prefill = {}
    mapping = {
        "Last Name": "last_name", "First Name": "first_name",
        "Phone Number": "phone", "Email": "email", "Content": "content",
        "Project": "project", "Member Information": "member_info",
    }
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if ":" in line and not line.startswith("#"):
                    key, _, value = line.partition(":")
                    store_key = mapping.get(key.strip())
                    if store_key:
                        prefill[store_key] = value.strip()
    except OSError:
        pass
    return prefill


# -- API ----------------------------------------------------------------------

@app.route("/api/equipment")
@login_required
def api_equipment():
    try:
        items = list_equipment()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify([{"subdomain": sub, "name": name} for sub, name in items])


@app.route("/api/availability")
@login_required
def api_availability():
    subdomain = request.args.get("equipment", "")
    days = int(request.args.get("days", 14))
    if not subdomain:
        return jsonify({"error": "missing equipment"}), 400
    try:
        data = availability_map(subdomain, days=days, counts=True)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(data)


@app.route("/api/me")
@login_required
def api_me():
    username = session["username"]
    return jsonify({
        "username": username,
        "is_admin": app.config["USERS"].is_admin(username),
        "stats": app.config["STATS"].user_stats(username),
    })


@app.route("/api/register", methods=["POST"])
@login_required
def api_register():
    body = request.get_json(silent=True) or {}
    subdomain = body.get("equipment")
    raw_dates = body.get("dates") or []
    if not subdomain:
        return jsonify({"error": "missing equipment"}), 400
    try:
        dates = [parse_date(d) for d in raw_dates]
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    if not dates:
        return jsonify({"error": "no dates selected"}), 400
    if len(dates) > 31:
        return jsonify({"error": "too many dates (max 31)"}), 400

    username = session["username"]
    user_info = app.config["USERS"].to_userinfo(username)
    job_id, err = jobs.start(
        subdomain=subdomain,
        dates=dates,
        user_info=user_info,
        token_method=app.config["TOKEN_METHOD"],
        fingerprint_path=app.config["FINGERPRINT_PATH"],
        browser_visible=app.config["BROWSER_VISIBLE"],
        dry_run=bool(body.get("dry_run") or app.config["DRY_RUN"]),
        all_slots=bool(body.get("all_slots")),
        username=username,
        stats_db=app.config["STATS"],
    )
    if err:
        return jsonify({"error": err}), 409
    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
@login_required
def api_status(job_id):
    status = jobs.status(job_id)
    if status is None:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(status)


@app.route("/api/stats")
@login_required
def api_stats():
    return jsonify(app.config["STATS"].user_stats(session["username"]))


@app.route("/api/admin/stats")
@admin_required
def api_admin_stats():
    return jsonify({
        "users": app.config["STATS"].all_stats(),
        "accounts": [
            {"username": u, "is_admin": a}
            for u, a in app.config["USERS"].list_users()
        ],
    })


# -- app factory --------------------------------------------------------------

def create_app(users, stats_db, secret_key, fingerprint_path, token_method,
               browser_visible, dry_run, userinfo_legacy_path=None):
    app.secret_key = secret_key
    app.config["USERS"] = users
    app.config["STATS"] = stats_db
    app.config["FINGERPRINT_PATH"] = fingerprint_path
    app.config["TOKEN_METHOD"] = token_method
    app.config["BROWSER_VISIBLE"] = browser_visible
    app.config["DRY_RUN"] = dry_run
    app.config["USERINFO_LEGACY_PATH"] = userinfo_legacy_path
    return app


def run_web(users, stats_db, secret_key, fingerprint_path, token_method,
            browser_visible, dry_run, port=5000, open_browser=True,
            userinfo_legacy_path=None):
    create_app(users, stats_db, secret_key, fingerprint_path, token_method,
               browser_visible, dry_run, userinfo_legacy_path)
    if open_browser:
        threading.Thread(
            target=lambda: (time.sleep(0.8), _open_browser(port)),
            daemon=True,
        ).start()
    log.info("AutoReg web UI on http://127.0.0.1:%d", port)
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)


def _open_browser(port):
    import webbrowser
    try:
        webbrowser.open(f"http://127.0.0.1:{port}/")
    except Exception as e:
        log.warning("Could not open browser: %s", e)
