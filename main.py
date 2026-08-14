import argparse
import atexit
import logging
import os
import subprocess
import sys

from autoreg.booking import BookingError, cli_book
from autoreg.stats import StatsDB
from autoreg.users import UserExistsError, UserStore

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("AUTOREG_DATA_DIR", os.path.join(ROOT_DIR, "data"))
USERS_PATH = os.path.join(DATA_DIR, "users.json")
STATS_PATH = os.path.join(DATA_DIR, "autoreg.db")
SECRET_PATH = os.path.join(DATA_DIR, "secret.key")
PID_PATH = os.path.join(DATA_DIR, "server.pid")
LOG_PATH = os.path.join(DATA_DIR, "autoreg.log")
USERINFO_LEGACY = os.path.join(DATA_DIR, "userInfo.txt")
DEFAULT_PORT = 5000

REQUIRED_INFO_FIELDS = ("Last Name", "First Name", "Phone Number", "Email", "Content")


def load_or_create_secret():
    if os.path.exists(SECRET_PATH):
        with open(SECRET_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    os.makedirs(DATA_DIR, exist_ok=True)
    secret = os.urandom(32).hex()
    with open(SECRET_PATH, "w", encoding="utf-8") as f:
        f.write(secret)
    return secret


def _process_alive(pid):
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True, text=True, timeout=15,
            )
            return f"{pid}" in out.stdout
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pidfile_guard():
    """Exit silently if another AutoReg server is already running."""
    if os.path.exists(PID_PATH):
        try:
            with open(PID_PATH, "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            pid = -1
        if _process_alive(pid):
            print(f"AutoReg is already running (pid {pid}). "
                  f"Open http://127.0.0.1:{DEFAULT_PORT}/")
            sys.exit(0)
        try:
            os.remove(PID_PATH)
        except OSError:
            pass
    with open(PID_PATH, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    atexit.register(lambda: os.path.exists(PID_PATH) and os.remove(PID_PATH))


# -- user management CLI -------------------------------------------------------

def _read_password(prompt="Password: "):
    import getpass
    try:
        if sys.stdin and not sys.stdin.isatty():
            return input(prompt).strip()
    except (AttributeError, OSError):
        pass
    return getpass.getpass(prompt)


def _add_user_cli(users, username):
    if users.user_exists(username):
        print(f"ERROR: user {username!r} already exists")
        return 1
    password = _read_password()
    if len(password) < 6:
        print("ERROR: password must be at least 6 characters")
        return 1
    print("Booking details (used on every booking):")
    details = {}
    for field in REQUIRED_INFO_FIELDS:
        value = input(f"  {field}: ").strip()
        if not value:
            print(f"ERROR: {field} is required")
            return 1
        details[field] = value
    project = input("  Project [optional, default Robocon SIG]: ").strip()
    member_info = input("  Member Information [optional]: ").strip()
    kwargs = {
        "last_name": details["Last Name"], "first_name": details["First Name"],
        "phone": details["Phone Number"], "email": details["Email"],
        "content": details["Content"],
        "project": project or None, "member_info": member_info or None,
        "is_admin": users.count() == 0,
    }
    users.add_user(username, password, **kwargs)
    print(f"User {username!r} created" + (" (admin)" if kwargs["is_admin"] else ""))
    return 0


def _list_users_cli(users):
    for username, is_admin in users.list_users():
        print(f"  {username}{' (admin)' if is_admin else ''}")
    return 0


def _remove_user_cli(users, username):
    if users.remove_user(username):
        print(f"User {username!r} removed")
        return 0
    print(f"ERROR: user {username!r} not found")
    return 1


def _set_admin_cli(users, username):
    if users.set_admin(username, True):
        print(f"User {username!r} is now admin")
        return 0
    print(f"ERROR: user {username!r} not found")
    return 1


def _stats_cli(stats):
    rows = stats.all_stats()
    if not rows:
        print("No bookings recorded yet.")
        return 0
    for row in sorted(rows, key=lambda r: -r["distinct_days"]):
        print(f"  {row['username']}: {row['distinct_days']} day(s), "
              f"{row['bookings']} booking(s)")
    return 0


# -- auto-start ---------------------------------------------------------------

def _startup_folder():
    return os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")),
        "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
    )


def _install_startup(port):
    pythonw = os.path.join(ROOT_DIR, ".venv", "Scripts", "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.exists(pythonw):
        print("ERROR: pythonw.exe not found")
        return 1

    folder = _startup_folder()
    os.makedirs(folder, exist_ok=True)
    vbs_path = os.path.join(folder, "AutoReg.vbs")
    vbs = (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.CurrentDirectory = "{ROOT_DIR}"\r\n'
        f'sh.Run """"{pythonw}"" main.py --no-browser --port {port} '
        '--background"", 0, False\r\n'
    )
    with open(vbs_path, "w", encoding="utf-8") as f:
        f.write(vbs)

    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    if os.path.isdir(desktop):
        shortcut = os.path.join(desktop, "AutoReg.url")
        with open(shortcut, "w", encoding="utf-8") as f:
            f.write("[InternetShortcut]\nURL=http://127.0.0.1:%d/\n" % port)
        print(f"Desktop shortcut created: {shortcut}")
    print(f"Auto-start installed: {vbs_path}")
    print("The server will start hidden at every Windows logon on port %d." % port)
    print(f"Open the UI at http://127.0.0.1:{port}/")
    return 0


def _uninstall_startup():
    removed = []
    for name in ("AutoReg.vbs", "AutoReg.url"):
        for folder in (_startup_folder(), os.path.join(os.path.expanduser("~"), "Desktop")):
            path = os.path.join(folder, name)
            if os.path.exists(path):
                os.remove(path)
                removed.append(path)
    if removed:
        for path in removed:
            print(f"Removed: {path}")
        return 0
    print("No auto-start files found.")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="AutoReg v3 - booking bot for Inno Wing machine room "
                    "(YouCanBook.me API + reCAPTCHA v3 token; local web UI with accounts)"
    )
    parser.add_argument("--cli", action="store_true",
                        help="Run the terminal booking flow instead of the web UI")
    parser.add_argument("-d", "--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--dry-run", action="store_true",
                        help="Stop before the final confirm call (no booking created)")
    parser.add_argument("--no-fingerprint", action="store_true",
                        help="Do not send the captured fingerprint.json with reCAPTCHA requests")
    parser.add_argument("--token-method", choices=["auto", "browser", "browserless"],
                        default="browser",
                        help="How to mint reCAPTCHA tokens (default: browser)")
    parser.add_argument("--browser-visible", dest="browser_visible",
                        action="store_true", default=True,
                        help="Mint tokens in a visible Chrome window (default)")
    parser.add_argument("--headless", dest="browser_visible", action="store_false",
                        help="Mint tokens in headless Chrome instead (may be rejected)")
    parser.add_argument("--all-slots", action="store_true",
                        help="Default to booking every free slot per selected date")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Web UI port")
    parser.add_argument("--no-browser", action="store_true",
                        help="Do not auto-open the web UI in the browser")
    parser.add_argument("--background", action="store_true",
                        help="Run hidden in the background (log to data/autoreg.log; "
                             "used by the startup task)")

    user_group = parser.add_argument_group("user management")
    user_group.add_argument("--add-user", metavar="USERNAME",
                            help="Create a user interactively")
    user_group.add_argument("--list-users", action="store_true", help="List users")
    user_group.add_argument("--remove-user", metavar="USERNAME", help="Remove a user")
    user_group.add_argument("--set-admin", metavar="USERNAME",
                            help="Grant admin to a user")
    user_group.add_argument("--stats", action="store_true",
                            help="Print per-user booking stats")

    startup_group = parser.add_argument_group("auto-start")
    startup_group.add_argument("--install-startup", action="store_true",
                               help="Add a hidden AutoReg.vbs to the Windows Startup "
                                    "folder so the server starts at logon")
    startup_group.add_argument("--uninstall-startup", action="store_true",
                               help="Remove the auto-start entry and desktop shortcut")
    args = parser.parse_args()

    level = logging.DEBUG if args.debug else logging.INFO
    if args.background:
        logging.basicConfig(filename=LOG_PATH, level=level,
                            format="%(asctime)s - %(levelname)s - %(message)s")
    else:
        logging.basicConfig(level=level,
                            format="%(asctime)s - %(levelname)s - %(message)s")

    if args.install_startup:
        return _install_startup(args.port)
    if args.uninstall_startup:
        return _uninstall_startup()

    users = UserStore(USERS_PATH)
    stats = StatsDB(STATS_PATH)

    if args.add_user:
        return _add_user_cli(users, args.add_user)
    if args.list_users:
        return _list_users_cli(users)
    if args.remove_user:
        return _remove_user_cli(users, args.remove_user)
    if args.set_admin:
        return _set_admin_cli(users, args.set_admin)
    if args.stats:
        return _stats_cli(stats)

    fingerprint_path = None if args.no_fingerprint else os.path.join(DATA_DIR, "fingerprint.json")

    if args.cli:
        return _run_cli(users, args, fingerprint_path)

    _pidfile_guard()
    from autoreg.webui import run_web
    run_web(
        users=users,
        stats_db=stats,
        secret_key=load_or_create_secret(),
        fingerprint_path=fingerprint_path,
        token_method=args.token_method,
        browser_visible=args.browser_visible,
        dry_run=args.dry_run,
        port=args.port,
        open_browser=not args.no_browser,
        userinfo_legacy_path=USERINFO_LEGACY,
    )
    return 0


def _run_cli(users, args, fingerprint_path):
    if users.count() == 0:
        print("No users yet. Create one first with:  python main.py --add-user <name>")
        return 1
    print("Users:")
    for username, is_admin in users.list_users():
        print(f"  {username}{' (admin)' if is_admin else ''}")
    chosen = None
    while chosen is None:
        raw = input("Booking as: ").strip()
        if users.user_exists(raw):
            chosen = raw
    user_info = users.to_userinfo(chosen)
    try:
        cli_book(user_info, fingerprint_path=fingerprint_path,
                 dry_run=args.dry_run, debug=args.debug,
                 token_method=args.token_method,
                 browser_visible=args.browser_visible,
                 all_slots=args.all_slots)
    except BookingError as e:
        print(f"ERROR: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
