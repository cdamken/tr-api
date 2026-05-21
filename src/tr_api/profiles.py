"""Multi-account profile management.

Layout on disk:

    ~/.tr-api/
    ├── active            ← text file with the active profile name (a phone number)
    └── profiles/
        ├── +4912345678/
        │   ├── meta.json   ← phone, jurisdiction, name (no secrets)
        │   └── cookies.txt ← Mozilla cookie jar imported from Chrome
        ├── +4998765432/
        │   ├── meta.json
        │   └── cookies.txt
        └── ...

A "profile" is identified by phone number (E.164 format, e.g. "+4912345678").
We never store PIN — authentication is entirely cookie-based.
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .exceptions import NoActiveProfile, ProfileNotFound

# E.164 phone format: +[country code][digits], 8-15 digits total
PHONE_RE = re.compile(r"^\+\d{8,15}$")

BASE_DIR = Path.home() / ".tr-api"
PROFILES_DIR = BASE_DIR / "profiles"
ACTIVE_FILE = BASE_DIR / "active"


@dataclass
class Profile:
    """Metadata for one TR account."""
    phone: str                       # E.164, e.g. "+4912345678"
    jurisdiction: str = "DE"         # ISO country code (DE, AT, FR, IT, etc.)
    name: str | None = None          # Friendly display name (e.g. "Personal")
    created_at: str | None = None    # ISO 8601 timestamp

    @property
    def dir(self) -> Path:
        return PROFILES_DIR / self.phone

    @property
    def meta_file(self) -> Path:
        return self.dir / "meta.json"

    @property
    def cookies_file(self) -> Path:
        return self.dir / "cookies.txt"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _validate_phone(phone: str) -> str:
    """Normalize and validate an E.164 phone number."""
    phone = phone.strip().replace(" ", "")
    if not PHONE_RE.fullmatch(phone):
        raise ValueError(
            f"Invalid phone number: {phone!r}. "
            "Use E.164 format: +<country code><digits>, e.g. +4912345678"
        )
    return phone


def _ensure_dirs() -> None:
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def create(phone: str, jurisdiction: str = "DE", name: str | None = None) -> Profile:
    """Create a new profile directory. Returns the Profile.

    Existing profile is overwritten (you'd be calling this from `refresh`).
    """
    phone = _validate_phone(phone)
    _ensure_dirs()

    prof = Profile(
        phone=phone,
        jurisdiction=jurisdiction,
        name=name,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    prof.dir.mkdir(parents=True, exist_ok=True)
    prof.meta_file.write_text(
        json.dumps(asdict(prof), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return prof


def load(phone: str) -> Profile:
    """Load a profile by phone number. Raises ProfileNotFound if missing."""
    phone = _validate_phone(phone)
    meta = PROFILES_DIR / phone / "meta.json"
    if not meta.is_file():
        raise ProfileNotFound(f"No profile for {phone} (expected at {meta})")
    data = json.loads(meta.read_text(encoding="utf-8"))
    return Profile(**data)


def list_all() -> list[Profile]:
    """Return every profile on disk, sorted by phone."""
    if not PROFILES_DIR.is_dir():
        return []
    out: list[Profile] = []
    for d in sorted(PROFILES_DIR.iterdir()):
        if not d.is_dir():
            continue
        meta = d / "meta.json"
        if not meta.is_file():
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            out.append(Profile(**data))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def remove(phone: str) -> None:
    """Delete a profile directory entirely. Also clears `active` if it pointed here."""
    phone = _validate_phone(phone)
    target = PROFILES_DIR / phone
    if target.is_dir():
        shutil.rmtree(target)
    if get_active_phone() == phone:
        ACTIVE_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Active profile pointer
# ---------------------------------------------------------------------------
def set_active(phone: str) -> Profile:
    """Mark a profile as active. Returns the profile (also verifies it exists)."""
    prof = load(phone)  # raises if missing
    _ensure_dirs()
    ACTIVE_FILE.write_text(prof.phone + "\n", encoding="utf-8")
    return prof


def get_active_phone() -> str | None:
    """Phone number of the active profile, or None if none set."""
    if not ACTIVE_FILE.is_file():
        return None
    txt = ACTIVE_FILE.read_text(encoding="utf-8").strip()
    return txt or None


def get_active() -> Profile:
    """Return the active Profile. Raises NoActiveProfile / ProfileNotFound."""
    phone = get_active_phone()
    if not phone:
        raise NoActiveProfile(
            "No active profile set. Run `tr-api profiles use <phone>` "
            "or `tr-api refresh` to create one."
        )
    return load(phone)
