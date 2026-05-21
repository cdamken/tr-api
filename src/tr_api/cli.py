"""tr-api command-line interface.

Implements the contract described in docs/cli-contract.md:

  - Exit codes are stable (0=OK, 10=NO_ACTIVE_PROFILE, 20=MISSING_COOKIES,
    30=SESSION_EXPIRED, etc. — see EXIT_CODES below).
  - `--json` switches stdout to a stable `{ok, data}` /
    `{ok:false, error, exit_code, message, hint}` envelope.
  - Default (no --json) is human-readable text. The dashboard always
    uses --json.

The CLI is a thin shell over the library — every subcommand calls
into tr_api.* functions and prints the result. The handlers themselves
don't deal with exit codes; the wrapping `_dispatch` does.

Entry point is `main()`, registered as `tr-api` in pyproject.toml.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import account, cookies, portfolio, profiles, transactions
from .client import TrClient
from .exceptions import (
    ApiError,
    ChromeNotFound,
    KeychainAccessDenied,
    MissingSessionCookies,
    NoActiveProfile,
    ProfileNotFound,
    SessionExpired,
    TrApiError,
)
from .profiles import Profile

# Exit-code contract. See docs/cli-contract.md.
EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_USAGE = 2
EXIT_NO_ACTIVE_PROFILE = 10
EXIT_PROFILE_NOT_FOUND = 11
EXIT_MISSING_COOKIES = 20
EXIT_CHROME_NOT_FOUND = 21
EXIT_KEYCHAIN_DENIED = 22
EXIT_SESSION_EXPIRED = 30
EXIT_API_ERROR = 31

# Map exception classes → (exit_code, hint). Order matters: more specific
# classes first.
_ERR_TABLE: list[tuple[type[BaseException], int, str | None]] = [
    (NoActiveProfile,       EXIT_NO_ACTIVE_PROFILE,
        "Run `tr-api profiles use <phone>` or `tr-api auth import --phone <phone>`."),
    (ProfileNotFound,       EXIT_PROFILE_NOT_FOUND,
        "List existing profiles with `tr-api profiles list`."),
    (MissingSessionCookies, EXIT_MISSING_COOKIES,
        "Run `tr-api auth import` to (re-)read cookies from Chrome."),
    (KeychainAccessDenied,  EXIT_KEYCHAIN_DENIED,
        "Look for a Keychain dialog on screen and click 'Always Allow'."),
    (ChromeNotFound,        EXIT_CHROME_NOT_FOUND,
        "Make sure Chrome is installed and you've used it at least once."),
    (SessionExpired,        EXIT_SESSION_EXPIRED,
        "Open https://app.traderepublic.com in Chrome, log in, then "
        "`tr-api auth import`."),
    (ApiError,              EXIT_API_ERROR, None),
    (TrApiError,            EXIT_GENERIC, None),
]


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
def _json_default(o: Any) -> Any:
    """JSON fallback for things json.dumps doesn't know: Path, datetime, …"""
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"Cannot serialize {type(o).__name__}")


def _emit_ok(data: Any, *, json_mode: bool) -> None:
    if json_mode:
        json.dump({"ok": True, "data": data}, sys.stdout, default=_json_default)
        sys.stdout.write("\n")
    else:
        # Human mode: if data is already a printable string, print it;
        # otherwise pretty-print JSON. Subcommands that want richer
        # human output pass a string here.
        if isinstance(data, str):
            print(data)
        else:
            json.dump(data, sys.stdout, indent=2, default=_json_default)
            sys.stdout.write("\n")


def _emit_err(exc: BaseException, *, json_mode: bool) -> int:
    exit_code = EXIT_GENERIC
    hint: str | None = None
    for cls, code, h in _ERR_TABLE:
        if isinstance(exc, cls):
            exit_code = code
            hint = h
            break

    payload: dict[str, Any] = {
        "ok": False,
        "error": type(exc).__name__,
        "exit_code": exit_code,
        "message": str(exc),
    }
    if hint:
        payload["hint"] = hint

    if json_mode:
        json.dump(payload, sys.stderr, default=_json_default)
        sys.stderr.write("\n")
    else:
        sys.stderr.write(f"error: {payload['error']}: {payload['message']}\n")
        if hint:
            sys.stderr.write(f"hint:  {hint}\n")
    return exit_code


# ---------------------------------------------------------------------------
# Profile resolution helper
# ---------------------------------------------------------------------------
def _resolve_profile(phone: str | None) -> Profile:
    """Load the requested profile, or the active one if phone is None."""
    if phone:
        return profiles.load(phone)
    return profiles.get_active()


# ---------------------------------------------------------------------------
# Subcommand handlers — each returns the `data` for the envelope
# ---------------------------------------------------------------------------
def cmd_version(args: argparse.Namespace) -> Any:
    from . import __version__
    return {"version": __version__}


# ----- profiles -----------------------------------------------------------
def cmd_profiles_list(args: argparse.Namespace) -> Any:
    active = profiles.get_active_phone()
    out = []
    for p in profiles.list_all():
        d = asdict(p)
        d["has_cookies"] = p.cookies_file.is_file()
        d["is_active"] = (p.phone == active)
        out.append(d)
    return {"active": active, "profiles": out}


def cmd_profiles_add(args: argparse.Namespace) -> Any:
    p = profiles.create(args.phone, jurisdiction=args.jurisdiction, name=args.name)
    return asdict(p)


def cmd_profiles_use(args: argparse.Namespace) -> Any:
    p = profiles.set_active(args.phone)
    return asdict(p)


def cmd_profiles_remove(args: argparse.Namespace) -> Any:
    if not args.yes:
        raise SystemExit(
            "Refusing to remove profile without --yes. "
            f"Re-run with: tr-api profiles remove {args.phone} --yes"
        )
    profiles.remove(args.phone)
    return {"removed": args.phone}


def cmd_profiles_show(args: argparse.Namespace) -> Any:
    p = _resolve_profile(args.phone)
    d = asdict(p)
    d["has_cookies"] = p.cookies_file.is_file()
    d["cookies_file"] = p.cookies_file
    return d


# ----- auth ---------------------------------------------------------------
def cmd_auth_import(args: argparse.Namespace) -> Any:
    p = _resolve_profile(args.phone) if not args.phone else None
    if args.phone:
        # If the profile doesn't exist, create an empty one first.
        try:
            p = profiles.load(args.phone)
        except ProfileNotFound:
            p = profiles.create(args.phone, jurisdiction=args.jurisdiction or "DE")
    assert p is not None

    got = cookies.import_from_chrome(browser=args.browser)
    n = cookies.save_to_file(got, p.cookies_file)
    summary = cookies.summarize(got)
    summary["saved_to"] = p.cookies_file
    summary["count"] = n
    summary["phone"] = p.phone
    # If no active profile is set yet, set this one.
    if profiles.get_active_phone() is None:
        profiles.set_active(p.phone)
        summary["set_active"] = True
    return summary


def cmd_auth_status(args: argparse.Namespace) -> Any:
    p = _resolve_profile(args.phone)
    out: dict[str, Any] = {
        "phone": p.phone,
        "cookies_file": p.cookies_file,
        "has_cookies": p.cookies_file.is_file(),
    }
    if p.cookies_file.is_file():
        jar = cookies.load_from_file(p.cookies_file)
        names = {c.name for c in jar}
        present = {c.name: c.value for c in jar}
        summary = cookies.summarize(present)
        summary.pop("required_missing")  # rename below for clarity
        summary["required_missing"] = sorted(cookies.REQUIRED_AUTH_COOKIES - names)
        out["summary"] = summary
        st = p.cookies_file.stat()
        out["cookies_file_mtime"] = datetime.fromtimestamp(
            st.st_mtime, tz=timezone.utc
        ).isoformat()
    return out


# ----- account / ping ----------------------------------------------------
def cmd_account(args: argparse.Namespace) -> Any:
    p = _resolve_profile(args.phone)
    with TrClient(p) as c:
        if args.summary:
            return asdict(account.summary(c))
        return account.fetch(c)


def cmd_ping(args: argparse.Namespace) -> Any:
    p = _resolve_profile(args.phone)
    with TrClient(p) as c:
        alive = account.ping(c)
    return {"phone": p.phone, "alive": alive}


# ----- portfolio ---------------------------------------------------------
def cmd_portfolio(args: argparse.Namespace) -> Any:
    p = _resolve_profile(args.phone)
    with TrClient(p) as c:
        if args.snapshot:
            return portfolio.snapshot(
                c,
                include_history=not args.no_history,
                history_range=args.history_range,
            )
        return portfolio.portfolio(c)


def cmd_cash(args: argparse.Namespace) -> Any:
    p = _resolve_profile(args.phone)
    with TrClient(p) as c:
        return portfolio.cash(c)


def cmd_history(args: argparse.Namespace) -> Any:
    p = _resolve_profile(args.phone)
    with TrClient(p) as c:
        return portfolio.history(c, timeframe=args.range)


# ----- transactions ------------------------------------------------------
def cmd_transactions(args: argparse.Namespace) -> Any:
    p = _resolve_profile(args.phone)
    with TrClient(p) as c:
        if args.since:
            cutoff = _parse_date(args.since)
            items = transactions.fetch_since(c, cutoff, max_pages=args.max_pages)
        elif args.since_id:
            items = transactions.fetch_until_id(
                c, args.since_id, max_pages=args.max_pages
            )
        else:
            items = transactions.fetch_all(c, max_pages=args.max_pages)
    return {"count": len(items), "items": items}


def _parse_date(s: str) -> datetime:
    """Accept YYYY-MM-DD or full ISO-8601."""
    try:
        if "T" in s:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s)
        # Plain date — treat as UTC midnight.
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as e:
        raise SystemExit(f"Bad --since date {s!r}: {e}")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tr-api",
        description="Minimal Python client for Trade Republic (DE).",
    )
    p.add_argument(
        "--json",
        dest="json_mode",
        action="store_true",
        help="Emit JSON envelope on stdout/stderr (the dashboard contract).",
    )
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<command>")

    # version
    sp = sub.add_parser("version", help="Print library version")
    sp.set_defaults(func=cmd_version)

    # profiles
    pp = sub.add_parser("profiles", help="Manage account profiles")
    pp_sub = pp.add_subparsers(dest="profiles_cmd", required=True)

    sp = pp_sub.add_parser("list", help="List all profiles")
    sp.set_defaults(func=cmd_profiles_list)

    sp = pp_sub.add_parser("add", help="Create an empty profile")
    sp.add_argument("phone", help="E.164 phone number, e.g. +4912345678")
    sp.add_argument("--name", default=None)
    sp.add_argument("--jurisdiction", default="DE")
    sp.set_defaults(func=cmd_profiles_add)

    sp = pp_sub.add_parser("use", help="Set the active profile")
    sp.add_argument("phone")
    sp.set_defaults(func=cmd_profiles_use)

    sp = pp_sub.add_parser("remove", help="Delete a profile")
    sp.add_argument("phone")
    sp.add_argument("--yes", action="store_true", help="Confirm deletion")
    sp.set_defaults(func=cmd_profiles_remove)

    sp = pp_sub.add_parser("show", help="Show one profile (default: active)")
    sp.add_argument("phone", nargs="?", default=None)
    sp.set_defaults(func=cmd_profiles_show)

    # auth
    ap = sub.add_parser("auth", help="Manage session cookies")
    ap_sub = ap.add_subparsers(dest="auth_cmd", required=True)

    sp = ap_sub.add_parser("import", help="Read cookies from Chrome and save")
    sp.add_argument("--phone", default=None,
                    help="Profile to save into (default: active)")
    sp.add_argument("--browser", default="chrome",
                    help="Browser to read from (chrome, chromium, brave, …)")
    sp.add_argument("--jurisdiction", default=None,
                    help="Used only when creating a new profile (default: DE)")
    sp.set_defaults(func=cmd_auth_import)

    sp = ap_sub.add_parser("status", help="Show cookie validity (no network)")
    sp.add_argument("--phone", default=None)
    sp.set_defaults(func=cmd_auth_status)

    # account / ping
    sp = sub.add_parser("account", help="Fetch /api/v2/auth/account")
    sp.add_argument("--phone", default=None)
    sp.add_argument("--summary", action="store_true",
                    help="Return only the curated summary fields")
    sp.set_defaults(func=cmd_account)

    sp = sub.add_parser("ping", help="Check if cookies still authenticate")
    sp.add_argument("--phone", default=None)
    sp.set_defaults(func=cmd_ping)

    # portfolio
    sp = sub.add_parser("portfolio", help="Fetch portfolio via WebSocket")
    sp.add_argument("--phone", default=None)
    sp.add_argument("--snapshot", action="store_true",
                    help="Also include cash and history (one WS connection)")
    sp.add_argument("--no-history", action="store_true",
                    help="With --snapshot: skip the history fetch")
    sp.add_argument("--history-range", default="1y",
                    choices=sorted(portfolio.HISTORY_RANGES))
    sp.set_defaults(func=cmd_portfolio)

    sp = sub.add_parser("cash", help="Fetch cash balances")
    sp.add_argument("--phone", default=None)
    sp.set_defaults(func=cmd_cash)

    sp = sub.add_parser("history", help="Fetch portfolio value history")
    sp.add_argument("--phone", default=None)
    sp.add_argument("--range", default="1y",
                    choices=sorted(portfolio.HISTORY_RANGES))
    sp.set_defaults(func=cmd_history)

    # transactions
    sp = sub.add_parser("transactions", help="Fetch timeline transactions")
    sp.add_argument("--phone", default=None)
    sp.add_argument("--since", default=None,
                    help="YYYY-MM-DD or full ISO-8601 cutoff (exclusive)")
    sp.add_argument("--since-id", action="append", default=None,
                    help="Stop when this ID appears (repeatable)")
    sp.add_argument("--max-pages", type=int, default=transactions.MAX_PAGES_DEFAULT)
    sp.set_defaults(func=cmd_transactions)

    return p


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _dispatch(args: argparse.Namespace) -> int:
    func: Callable[[argparse.Namespace], Any] = args.func
    try:
        data = func(args)
    except SystemExit as e:
        # SystemExit with a string message is our way to signal usage errors
        # inside handlers (e.g. cmd_profiles_remove without --yes).
        if isinstance(e.code, str):
            sys.stderr.write(e.code + "\n")
            return EXIT_USAGE
        return e.code if isinstance(e.code, int) else EXIT_GENERIC
    except TrApiError as e:
        return _emit_err(e, json_mode=args.json_mode)
    except Exception as e:  # pragma: no cover — defensive
        return _emit_err(e, json_mode=args.json_mode)

    _emit_ok(data, json_mode=args.json_mode)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return _dispatch(args)


if __name__ == "__main__":
    raise SystemExit(main())
