"""Map Prime Video profile names to picker indices for lg-tv-connect.py."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_PROFILE_CONFIG = Path.home() / ".lg-tv-prime-profiles.json"
# "none" = single-screen picker (all profiles on one list, no Adult/Kids step).
PROFILE_TYPES = ("adult", "kid", "none")


@dataclass
class PrimeProfileEntry:
    name: str
    index: int
    row: int = 0
    pin: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _empty_config() -> dict:
    return {"profiles": {key: [] for key in PROFILE_TYPES}}


def load_profile_config(path: Path | None = None) -> dict:
    path = path or DEFAULT_PROFILE_CONFIG
    if not path.exists():
        return _empty_config()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"profile config must be a JSON object: {path}")
    profiles = data.setdefault("profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError(f'"profiles" must be an object in {path}')
    for key in PROFILE_TYPES:
        profiles.setdefault(key, [])
    return data


def save_profile_config(data: dict, path: Path | None = None) -> Path:
    path = path or DEFAULT_PROFILE_CONFIG
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _entries_for_type(config: dict, profile_type: str) -> list[PrimeProfileEntry]:
    if profile_type not in PROFILE_TYPES:
        raise ValueError(f"profile-type must be one of {PROFILE_TYPES}, not {profile_type!r}")
    raw = config.get("profiles", {}).get(profile_type, [])
    entries: list[PrimeProfileEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        index = item.get("index")
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(index, int) or index < 0:
            continue
        row = item.get("row", 0)
        pin = item.get("pin")
        entries.append(
            PrimeProfileEntry(
                name=name.strip(),
                index=index,
                row=int(row) if isinstance(row, int) and row >= 0 else 0,
                pin=str(pin).strip() if isinstance(pin, str) and pin.strip() else None,
            )
        )
    return entries


def list_profiles(config: dict | None = None) -> list[tuple[str, PrimeProfileEntry]]:
    config = config or load_profile_config()
    rows: list[tuple[str, PrimeProfileEntry]] = []
    for profile_type in PROFILE_TYPES:
        for entry in _entries_for_type(config, profile_type):
            rows.append((profile_type, entry))
    return rows


def _match_profile_name(
    name: str,
    entries: list[PrimeProfileEntry],
) -> PrimeProfileEntry:
    exact = [entry for entry in entries if entry.name.lower() == name.lower()]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ValueError(f"multiple profiles named {name!r}")

    partial = [entry for entry in entries if name.lower() in entry.name.lower()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        matches = ", ".join(entry.name for entry in partial)
        raise ValueError(f'profile name {name!r} is ambiguous; matches: {matches}')

    known = ", ".join(entry.name for entry in entries) or "(none)"
    raise ValueError(f'unknown profile name {name!r}; configured: {known}')


def format_profile_usage(profile_type: str, entry: PrimeProfileEntry, *, show_type: bool) -> str:
    parts = [f'--profile-name "{entry.name}"']
    if show_type:
        parts.append(f"--profile-type {profile_type}")
    if entry.pin:
        parts.append(f'--profile-pin "{entry.pin}"')
    return " ".join(parts)


def format_profiles_table(config: dict | None = None) -> str:
    rows = list_profiles(config)
    if not rows:
        return (
            f"No profiles configured in {DEFAULT_PROFILE_CONFIG}.\n"
            "Add one with:\n"
            '  lg-tv-connect.py --profile-save "Your Name" --profile 0 --profile-type adult'
        )

    headers = ["picker", "name", "index", "row", "pin"]
    table_rows: list[list[str]] = []
    for profile_type, entry in rows:
        table_rows.append(
            [
                profile_type,
                entry.name,
                str(entry.index),
                str(entry.row),
                "yes" if entry.pin else "-",
            ]
        )

    widths = [len(h) for h in headers]
    for row in table_rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))

    def fmt(cells: list[str]) -> str:
        return "  ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(cells))

    names_seen: dict[str, int] = {}
    for _profile_type, entry in rows:
        key = entry.name.lower()
        names_seen[key] = names_seen.get(key, 0) + 1
    show_type_in_usage = len({ptype for ptype, _ in rows}) > 1 or any(
        count > 1 for count in names_seen.values()
    )

    lines = [fmt(headers), fmt(["-" * w for w in widths])]
    lines.extend(fmt(row) for row in table_rows)
    lines.append(f"\nConfig: {DEFAULT_PROFILE_CONFIG}")
    lines.append("picker = Adult/Kids screen; name = label on the profile-name screen")
    if any(entry.name.lower() in {"adult", "kids", "kid"} for _, entry in rows):
        lines.append(
            'warning: profile name looks like a picker label — save the label from the '
            "name screen (e.g. your account name), not Adult/Kids"
        )
    lines.append("Use:")
    for profile_type, entry in rows:
        usage = format_profile_usage(profile_type, entry, show_type=show_type_in_usage)
        lines.append(f"  lg-tv-connect.py --launch amazon --content-id <ID> {usage}")
    return "\n".join(lines)


def resolve_profile_name(
    name: str,
    *,
    profile_type: str | None = None,
    config: dict | None = None,
) -> tuple[str, PrimeProfileEntry]:
    """Return (picker_type, entry) for a configured profile name."""
    name = name.strip()
    if not name:
        raise ValueError("profile name must not be empty")

    config = config or load_profile_config()
    if profile_type is not None:
        if profile_type not in PROFILE_TYPES:
            raise ValueError(f"profile-type must be one of {PROFILE_TYPES}, not {profile_type!r}")
        entries = _entries_for_type(config, profile_type)
        if not entries:
            raise ValueError(
                f'No {profile_type} profiles configured in {DEFAULT_PROFILE_CONFIG}. '
                f'Add one with --profile-save "{name}" --profile 0 --profile-type {profile_type}'
            )
        try:
            return profile_type, _match_profile_name(name, entries)
        except ValueError as exc:
            known = ", ".join(entry.name for entry in entries)
            raise ValueError(
                f"{exc} for type {profile_type!r}; configured: {known}"
            ) from exc

    matches: list[tuple[str, PrimeProfileEntry]] = []
    for ptype in PROFILE_TYPES:
        for entry in _entries_for_type(config, ptype):
            if entry.name.lower() == name.lower() or name.lower() in entry.name.lower():
                matches.append((ptype, entry))

    exact = [(ptype, entry) for ptype, entry in matches if entry.name.lower() == name.lower()]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        options = ", ".join(f'{entry.name!r} ({ptype})' for ptype, entry in exact)
        raise ValueError(f"profile name {name!r} is ambiguous; matches: {options}")

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        options = ", ".join(f'{entry.name!r} ({ptype})' for ptype, entry in matches)
        raise ValueError(f'profile name {name!r} is ambiguous; matches: {options}')

    all_rows = list_profiles(config)
    if not all_rows:
        raise ValueError(
            f"No profiles configured in {DEFAULT_PROFILE_CONFIG}. "
            f'Add one with --profile-save "{name}" --profile 0 --profile-type adult'
        )
    known = ", ".join(f'{entry.name!r} ({ptype})' for ptype, entry in all_rows)
    raise ValueError(f'unknown profile name {name!r}; configured: {known}')


def upsert_profile(
    name: str,
    *,
    index: int,
    profile_type: str,
    row: int = 0,
    pin: str | None = None,
    config: dict | None = None,
    path: Path | None = None,
) -> PrimeProfileEntry:
    name = name.strip()
    if not name:
        raise ValueError("profile name must not be empty")
    if index < 0:
        raise ValueError("profile index must be >= 0")
    if row < 0:
        raise ValueError("profile-row must be >= 0")
    if profile_type not in PROFILE_TYPES:
        raise ValueError(f"profile-type must be one of {PROFILE_TYPES}")

    config = config or load_profile_config(path)
    entry = PrimeProfileEntry(name=name, index=index, row=row, pin=pin)
    profiles = config.setdefault("profiles", {})
    for ptype in PROFILE_TYPES:
        profiles.setdefault(ptype, [])
    # A profile name is unique across pickers: drop any stale entry (possibly
    # under a different picker type) before adding the fresh one. This avoids
    # duplicate/ambiguous names when re-saving, e.g. moving a profile from the
    # two-step "adult" picker to a single-screen "none" picker.
    for ptype in PROFILE_TYPES:
        profiles[ptype] = [
            item.to_dict()
            for item in _entries_for_type(config, ptype)
            if item.name.lower() != name.lower()
        ]
    profiles[profile_type].append(entry.to_dict())
    save_profile_config(config, path)
    return entry