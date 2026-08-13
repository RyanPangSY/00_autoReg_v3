"""Local Flask web UI for AutoReg v3.

Serves a calendar UI at http://127.0.0.1:<port>/ and exposes a small JSON
API on top of the booking engine (booking.py). Registrations run in a
background thread with live per-date progress.
"""
import logging
import threading
import time
import uuid

from flask import Flask, jsonify, render_template, request

from .booking import (
    TokenMinter,
    availability_map,
    book_all_slots,
    book_date,
    format_results,
    list_equipment,
    parse_date,
)

log = logging.getLogger("webui")

app = Flask(__name__)


class JobManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._jobs = {}

    def start(self, subdomain, dates, user_info, token_method, fingerprint_path,
              browser_visible, dry_run, all_slots):
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


jobs = JobManager()


# -- UI page -----------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# -- API ---------------------------------------------------------------------

@app.route("/api/equipment")
def api_equipment():
    try:
        items = list_equipment()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify([{"subdomain": sub, "name": name} for sub, name in items])


@app.route("/api/availability")
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


@app.route("/api/register", methods=["POST"])
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

    job_id, err = jobs.start(
        subdomain=subdomain,
        dates=dates,
        user_info=app.config["USER_INFO"],
        token_method=app.config["TOKEN_METHOD"],
        fingerprint_path=app.config["FINGERPRINT_PATH"],
        browser_visible=app.config["BROWSER_VISIBLE"],
        dry_run=bool(body.get("dry_run") or app.config["DRY_RUN"]),
        all_slots=bool(body.get("all_slots")),
    )
    if err:
        return jsonify({"error": err}), 409
    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def api_status(job_id):
    status = jobs.status(job_id)
    if status is None:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(status)


def create_app(user_info, fingerprint_path, token_method, browser_visible, dry_run):
    app.config["USER_INFO"] = user_info
    app.config["FINGERPRINT_PATH"] = fingerprint_path
    app.config["TOKEN_METHOD"] = token_method
    app.config["BROWSER_VISIBLE"] = browser_visible
    app.config["DRY_RUN"] = dry_run
    return app


def run_web(user_info, fingerprint_path, token_method, browser_visible, dry_run,
            port=5000, open_browser=True):
    create_app(user_info, fingerprint_path, token_method, browser_visible, dry_run)
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
