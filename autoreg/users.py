"""Per-user account store for AutoReg v3.

Users are stored in data/users.json with hashed passwords
(werkzeug.security / PBKDF2). Each user carries the booking details that
get submitted with their bookings. The first registered user is the admin.
"""
import json
import os
import threading

from werkzeug.security import check_password_hash, generate_password_hash

USER_INFO_KEYS = {
    "Last Name": "last_name",
    "First Name": "first_name",
    "Phone Number": "phone",
    "Email": "email",
    "Content": "content",
    "Project": "project",
    "Member Information": "member_info",
}


class UserExistsError(Exception):
    pass


class UserStore:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._users = {}
        self._mtime = None
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                self._users = json.load(f)
            self._mtime = os.path.getmtime(self.path)
        else:
            self._users = {}

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self._users, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, self.path)
        self._mtime = os.path.getmtime(self.path)

    def _maybe_reload(self):
        """Pick up changes written by another process (e.g. CLI user commands
        run while the web server is up). On a read error, keep the current
        snapshot and leave the mtime stale so the next query retries."""
        try:
            if os.path.exists(self.path) and os.path.getmtime(self.path) != self._mtime:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._users = json.load(f)
                self._mtime = os.path.getmtime(self.path)
        except (OSError, ValueError):
            pass

    # -- queries --------------------------------------------------------------

    def user_exists(self, username):
        with self._lock:
            self._maybe_reload()
            return username in self._users

    def list_users(self):
        with self._lock:
            self._maybe_reload()
            return sorted(
                (u, d.get("is_admin", False)) for u, d in self._users.items()
            )

    def is_admin(self, username):
        with self._lock:
            self._maybe_reload()
            user = self._users.get(username)
            return bool(user and user.get("is_admin"))

    def count(self):
        with self._lock:
            self._maybe_reload()
            return len(self._users)

    def verify(self, username, password):
        with self._lock:
            self._maybe_reload()
            user = self._users.get(username)
            if not user:
                return False
            return check_password_hash(user.get("password_hash", ""), password)

    def to_userinfo(self, username):
        """Booking details dict in the format booking.build_form expects."""
        with self._lock:
            self._maybe_reload()
            user = self._users.get(username)
            if not user:
                return None
        info = {}
        for userinfo_key, store_key in USER_INFO_KEYS.items():
            value = user.get(store_key)
            if value:
                info[userinfo_key] = value
        return info

    # -- mutations -------------------------------------------------------------

    def add_user(self, username, password, *, last_name="", first_name="", phone="",
                 email="", content="", project=None, member_info=None,
                 is_admin=False, required_keys=None):
        """Create a user. Raises UserExistsError if the name is taken.

        required_keys: list of USER_INFO_KEYS that must be non-empty
        (e.g. the ones the booking form marks required).
        """
        with self._lock:
            if username in self._users:
                raise UserExistsError(f"username {username!r} already exists")
            for key in required_keys or ():
                if not locals().get(USER_INFO_KEYS[key]):
                    raise ValueError(f"{key} is required")
            self._users[username] = {
                "password_hash": generate_password_hash(password),
                "last_name": last_name,
                "first_name": first_name,
                "phone": phone,
                "email": email,
                "content": content,
                "project": project,
                "member_info": member_info,
                "is_admin": is_admin,
            }
            self._save()

    def remove_user(self, username):
        with self._lock:
            removed = self._users.pop(username, None) is not None
            if removed:
                self._save()
            return removed

    def set_admin(self, username, is_admin):
        with self._lock:
            user = self._users.get(username)
            if not user:
                return False
            user["is_admin"] = bool(is_admin)
            self._save()
            return True

    def update_info(self, username, **fields):
        """Update booking-detail fields of an existing user."""
        with self._lock:
            user = self._users.get(username)
            if not user:
                return False
            for store_key, value in fields.items():
                if store_key in USER_INFO_KEYS.values() or store_key == "password_hash":
                    user[store_key] = value
            self._save()
            return True
