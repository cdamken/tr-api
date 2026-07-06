#!/usr/bin/env python3
"""Standalone probe for Trade Republic's v2 web-login (push-approval) flow.

WHY: TR deprecated /api/v1/auth/web/login (426 CLIENT_VERSION_OUTDATED) and
moved web login to /api/v2/auth/web/login with a push-APPROVAL model — no
4-digit code anymore; you approve the login from the TR mobile app. This
script proves the v2 flow end-to-end BEFORE we migrate tr-api + the
downstreams. It touches nothing in production: separate file, its own device
id, its own cookie jar.

Ref: pytr PR #355 (migrate to /api/v2/auth/web/login + push approval).

RUN IT (on the server, where Playwright + the tr-venv live):

    export PLAYWRIGHT_BROWSERS_PATH=/var/cache/tr-playwright   # shared cache
    export TR_PHONE=+49...            # your TR phone, intl format
    /opt/tr-venv/bin/python /tmp/v2_login_probe.py
    # It will PROMPT for your PIN (hidden). Then APPROVE the login in your
    # Trade Republic app when the push arrives.

Optional: set TR_PIN in the env to skip the prompt (less safe — shell history).
"""
from __future__ import annotations

import base64
import getpass
import json
import os
import sys
import time
import uuid
from pathlib import Path

import requests

# Reuse ONLY the hard part (the AWS WAF token via Playwright) from tr-api.
from tr_api.waf import get_waf_token

HOST = "https://api.traderepublic.com"
V2_LOGIN = "/api/v2/auth/web/login"

# Values captured from app.traderepublic.com (pytr PR #355, 2026-05-29).
# Overridable via env in case TR bumps them.
APP_VERSION = os.environ.get("TR_WEB_APP_VERSION", "15.7.0")
USER_AGENT = os.environ.get(
    "TR_WEB_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
)
DEVICE_ID_FILE = Path("/tmp/tr_v2_probe_device_id")


def log(msg: str) -> None:
    print(msg, flush=True)


def get_device_id() -> str:
    try:
        return DEVICE_ID_FILE.read_text().strip()
    except FileNotFoundError:
        did = uuid.uuid4().hex + uuid.uuid4().hex  # 64 hex chars, like the web app
        DEVICE_ID_FILE.write_text(did)
        return did


def device_info_header() -> str:
    payload = {
        "stableDeviceId": get_device_id(),
        "model": "Apple Macintosh",
        "browser": "Chrome",
        "browserVersion": "148.0.0.0",
        "os": "Mac OS",
        "osVersion": "10.15.7",
        "timezone": "Europe/Berlin",
        "timezoneOffset": -120,
        "screen": "1800x1169x30",
        "preferredLanguages": ["en", "en-US"],
        "numberOfCores": 12,
        "deviceMemory": 16,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def auth_headers(waf_token: str) -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": "https://app.traderepublic.com",
        "Referer": "https://app.traderepublic.com/",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "x-tr-platform": "web",
        "x-tr-app-version": APP_VERSION,
        "x-tr-device-info": device_info_header(),
        "x-aws-waf-token": waf_token,
    }


def main() -> int:
    phone = (os.environ.get("TR_PHONE") or "").strip()
    if not phone:
        phone = input("TR phone (intl, e.g. +49...): ").strip()
    pin = (os.environ.get("TR_PIN") or "").strip()
    if not pin:
        pin = getpass.getpass("TR PIN (hidden): ").strip()
    if not phone.startswith("+") or not pin.isdigit():
        log("ERROR: phone must start with + and PIN must be digits.")
        return 2

    log("→ Getting AWS WAF token via Playwright (a few seconds)…")
    try:
        waf = get_waf_token().value
    except Exception as e:
        log(f"ERROR getting WAF token: {type(e).__name__}: {e}")
        return 3
    log(f"  WAF token acquired ({len(waf)} chars).")

    sess = requests.Session()
    # v2 wants the token BOTH as a cookie and as the x-aws-waf-token header.
    sess.cookies.set("aws-waf-token", waf, domain=".traderepublic.com")
    headers = auth_headers(waf)

    log(f"→ POST {V2_LOGIN}")
    r = sess.post(HOST + V2_LOGIN, json={"phoneNumber": phone, "pin": pin},
                  headers=headers, timeout=25)
    log(f"  status={r.status_code}")
    log(f"  body={r.text[:400]}")
    if r.status_code != 200:
        log("✗ Initiate failed. If 426=CLIENT_VERSION_OUTDATED, bump x-tr-app-version.")
        return 4
    try:
        j = r.json()
    except ValueError:
        log("✗ 200 but body wasn't JSON."); return 4
    process_id = j.get("processId")
    if not process_id:
        log(f"✗ No processId in response: {j}"); return 4
    log(f"  processId={process_id}")
    log(f"  countdownInSeconds={j.get('countdownInSeconds')}")
    log(f"  2FA hint={j.get('twoFactorMethod') or j.get('twoFactor')}")

    poll_url = f"{HOST}{V2_LOGIN}/processes/{process_id}"
    log("")
    log("📱  APPROVE THE LOGIN IN YOUR TRADE REPUBLIC APP NOW.")
    log(f"    Polling {poll_url} every 2s (up to 180s)…")
    deadline = time.time() + 180
    approved = False
    while time.time() < deadline:
        pr = sess.get(poll_url, headers=headers, timeout=15)
        body = pr.text[:200]
        if pr.status_code == 200:
            try:
                pj = pr.json()
            except ValueError:
                pj = {}
            state = str(pj.get("state") or pj.get("status") or "").upper()
            log(f"  poll 200 state={state!r} body={body}")
            if state in ("APPROVED", "COMPLETED", "SUCCESS", "OK", "DONE"):
                approved = True
                break
            if state in ("REJECTED", "DECLINED", "FAILED", "EXPIRED"):
                log(f"✗ Login {state}."); return 5
            # unknown state but a session cookie appeared → treat as done
            if any(c.name == "tr_session" and c.value for c in sess.cookies):
                approved = True
                break
        elif pr.status_code in (401, 403, 404, 410):
            log(f"  poll {pr.status_code} body={body}")
            log("✗ Process gone/expired (approve faster next time).")
            return 5
        else:
            log(f"  poll {pr.status_code} body={body}")
        time.sleep(2)

    if not approved:
        log("✗ No approval received within timeout.")
        return 6

    got = {c.name: (c.value[:12] + "…") for c in sess.cookies
           if c.domain.endswith("traderepublic.com")}
    log("")
    log("✅  LOGIN APPROVED. Session cookies received:")
    log(f"    {json.dumps(got, indent=2)}")

    # Prove the session works with a lightweight authenticated REST call.
    try:
        acct = sess.get(HOST + "/api/v2/auth/account",
                        headers={"User-Agent": USER_AGENT}, timeout=15)
        log(f"→ GET /api/v2/auth/account status={acct.status_code} "
            f"body={acct.text[:200]}")
    except Exception as e:
        log(f"  (account check errored: {type(e).__name__}: {e})")

    log("")
    log("🎉  v2 push-approval flow WORKS. We can migrate tr-api to this.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
