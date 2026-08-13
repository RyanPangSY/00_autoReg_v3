import argparse
import logging
import os

from autoreg.booking import BookingError, cli_book, load_user_info


def main():
    parser = argparse.ArgumentParser(
        description="AutoReg v3 - booking bot for Inno Wing machine room "
                    "(YouCanBook.me API + reCAPTCHA v3 token; local web UI)"
    )
    parser.add_argument("--cli", action="store_true",
                        help="Run the terminal flow instead of the web UI")
    parser.add_argument("-d", "--debug", action="store_true",
                        help="Enable debug logging")
    parser.add_argument("--dry-run", action="store_true",
                        help="Stop before the final confirm call (no booking created)")
    parser.add_argument("--no-fingerprint", action="store_true",
                        help="Do not send the captured fingerprint.json with reCAPTCHA requests")
    parser.add_argument("--token-method", choices=["auto", "browser", "browserless"],
                        default="browser",
                        help="How to mint reCAPTCHA tokens: real Chrome (best score, "
                             "default), browserless (no Chrome), or auto (Chrome with "
                             "browserless fallback)")
    parser.add_argument("--browser-visible", dest="browser_visible",
                        action="store_true", default=True,
                        help="Mint tokens in a visible Chrome window (default; proven "
                             "to pass the server's score threshold)")
    parser.add_argument("--headless", dest="browser_visible", action="store_false",
                        help="Mint tokens in headless Chrome instead (may be rejected)")
    parser.add_argument("--all-slots", action="store_true",
                        help="Default the web UI / CLI to booking every available "
                             "time slot on each selected date (one booking per slot)")
    parser.add_argument("--port", type=int, default=5000,
                        help="Port for the web UI (default: 5000)")
    parser.add_argument("--no-browser", action="store_true",
                        help="Do not auto-open the web UI in the browser")
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG,
                            format="%(asctime)s - %(levelname)s - %(message)s")
    else:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s - %(levelname)s - %(message)s")

    root_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(root_dir, "data")
    try:
        user_info = load_user_info(os.path.join(data_dir, "userInfo.txt"))
    except BookingError as e:
        print(f"ERROR: {e}")
        return 1

    fingerprint_path = None if args.no_fingerprint else os.path.join(data_dir, "fingerprint.json")

    if args.cli:
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

    from autoreg.webui import run_web
    run_web(
        user_info,
        fingerprint_path=fingerprint_path,
        token_method=args.token_method,
        browser_visible=args.browser_visible,
        dry_run=args.dry_run,
        port=args.port,
        open_browser=not args.no_browser,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
