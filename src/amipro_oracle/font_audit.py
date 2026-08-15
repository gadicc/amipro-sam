from __future__ import annotations

import configparser
import hashlib
import re
import stat
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from .constants import EXIT_INTEGRITY
from .errors import OracleError
from .io import digest_json

FONT_ENVIRONMENT_SCHEMA = "amipro-oracle-font-environment-v1"
FONT_RESOLUTION_SCHEMA = "amipro-oracle-font-resolution-v1"

MAX_WIN_INI_BYTES = 1024 * 1024
MAX_FONT_OCCURRENCES = 4_096
MAX_FONT_FAMILIES = 512
MAX_FONT_NAME_BYTES = 256
MAX_RUNTIME_FONT_FILES = 2_048
MAX_RUNTIME_FONT_BYTES = 32 * 1024 * 1024

_STYLE_FONT_HEADER = re.compile(rb"(?m)^[ \t]+\[fnt\][ \t]*\r?\n")
_FONT_FILE_SUFFIXES = frozenset({".fon", ".ttf"})
_FONT_WRAPPER_SUFFIXES = frozenset({".fot"})
_FACE_SUFFIX = re.compile(r"\s+(?:bold italic|bold oblique|bold|italic|oblique|regular)\Z", re.I)
_SIZE_SUFFIX = re.compile(r"\s+\d+(?:\s*,\s*\d+)+\Z")
_DESCRIPTION_SUFFIX = re.compile(r"\s+\([^()]{0,80}\)\Z")


def _font_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _unescape_literal(value: str) -> str:
    return (
        value.replace("@@", "@")
        .replace("<<", "<")
        .replace("<;>", ">")
        .replace("<[>", "[")
        .replace("</R>", "'")
    )


def _safe_font_name(raw: bytes, *, inline: bool) -> str | None:
    value = raw.strip(b" \t\r")
    if not value or len(value) > MAX_FONT_NAME_BYTES:
        return None
    name = _unescape_literal(value.decode("latin-1", errors="strict")).strip()
    if inline:
        name = re.sub(r"^\d", "", name).strip()
    if (
        not name
        or len(name) > MAX_FONT_NAME_BYTES
        or any(unicodedata.category(character) == "Cc" for character in name)
        or not _font_key(name)
    ):
        return None
    return name


def _next_line(payload: bytes, start: int) -> bytes:
    if start >= len(payload):
        return b""
    end = payload.find(b"\n", start)
    if end < 0:
        end = len(payload)
    return payload[start:end].rstrip(b"\r")


def _requested_fonts(payload: bytes) -> tuple[list[tuple[str, str]], dict[str, object]]:
    requests: list[tuple[str, str]] = []
    malformed_count = 0
    truncated = False
    style_commands = 0
    inline_commands = 0

    for match in _STYLE_FONT_HEADER.finditer(payload):
        style_commands += 1
        if style_commands > MAX_FONT_OCCURRENCES:
            truncated = True
            break
        line = _next_line(payload, match.end())
        family = _safe_font_name(line, inline=False)
        if family is None:
            malformed_count += 1
            continue
        if len(requests) >= MAX_FONT_OCCURRENCES:
            truncated = True
            break
        requests.append((family, "style"))

    if not truncated:
        cursor = 0
        while True:
            start = payload.find(b"<:f", cursor)
            if start < 0:
                break
            cursor = start + 3
            if start > 0 and payload[start - 1 : start] == b"<":
                continue
            inline_commands += 1
            if inline_commands > MAX_FONT_OCCURRENCES:
                truncated = True
                break
            end = payload.find(b">", cursor)
            newline = payload.find(b"\n", cursor)
            if end < 0 or (newline >= 0 and newline < end) or end - cursor > 1024:
                malformed_count += 1
                continue
            descriptor = payload[cursor:end]
            cursor = end + 1
            if not descriptor:
                continue
            fields = descriptor.split(b",")
            compact = len(fields) == 3 and fields[2] == b""
            if len(fields) not in {1, 2, 5} and not compact:
                malformed_count += 1
                continue
            if len(fields) < 2 or not fields[1]:
                continue
            family = _safe_font_name(fields[1], inline=True)
            if family is None:
                malformed_count += 1
                continue
            if len(requests) >= MAX_FONT_OCCURRENCES:
                truncated = True
                break
            requests.append((family, "inline"))

    return requests, {
        "occurrence_limit": MAX_FONT_OCCURRENCES,
        "family_limit": MAX_FONT_FAMILIES,
        "malformed_count": malformed_count,
        "style_command_count": style_commands,
        "inline_command_count": inline_commands,
        "truncated": truncated,
    }


def _read_win_ini(path: Path) -> tuple[configparser.RawConfigParser, bytes]:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise OracleError("font inventory WIN.INI is missing", exit_code=EXIT_INTEGRITY) from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or not 1 <= info.st_size <= MAX_WIN_INI_BYTES
    ):
        raise OracleError("font inventory WIN.INI is unsafe", exit_code=EXIT_INTEGRITY)
    try:
        payload = path.read_bytes()
        parser = configparser.RawConfigParser(interpolation=None, strict=True)
        parser.optionxform = str
        parser.read_string(payload.decode("latin-1", errors="strict"))
    except (OSError, UnicodeError, configparser.Error) as exc:
        raise OracleError("font inventory WIN.INI is invalid", exit_code=EXIT_INTEGRITY) from exc
    after = path.lstat()
    before_identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if after_identity != before_identity:
        raise OracleError("font inventory WIN.INI changed while reading", exit_code=EXIT_INTEGRITY)
    return parser, payload


def _section_name(parser: configparser.RawConfigParser, requested: str) -> str | None:
    folded = requested.casefold()
    return next((name for name in parser.sections() if name.casefold() == folded), None)


def _family_variants(label: str) -> set[str]:
    value = _DESCRIPTION_SUFFIX.sub("", label.strip())
    value = _SIZE_SUFFIX.sub("", value).strip()
    variants = {value}
    base = _FACE_SUFFIX.sub("", value).strip()
    if base:
        variants.add(base)
    return {variant for variant in variants if _font_key(variant)}


def _runtime_font_file_counts(runtime: Path) -> dict[str, int]:
    counts = {"font_binary_count": 0, "registration_wrapper_count": 0}
    seen = 0
    for root in (runtime / "WINDOWS" / "SYSTEM", runtime / "AMIPRO"):
        if root.is_symlink() or not root.is_dir():
            raise OracleError(
                "font inventory runtime topology is incomplete",
                exit_code=EXIT_INTEGRITY,
            )
        for path in root.iterdir():
            if path.is_symlink():
                raise OracleError("font inventory contains a symlink", exit_code=EXIT_INTEGRITY)
            if not path.is_file():
                continue
            suffix = path.suffix.casefold()
            if suffix not in _FONT_FILE_SUFFIXES | _FONT_WRAPPER_SUFFIXES:
                continue
            seen += 1
            if seen > MAX_RUNTIME_FONT_FILES:
                raise OracleError(
                    "runtime font inventory exceeds its bound",
                    exit_code=EXIT_INTEGRITY,
                )
            if suffix in _FONT_FILE_SUFFIXES:
                counts["font_binary_count"] += 1
            else:
                counts["registration_wrapper_count"] += 1
    return counts


def _validate_registered_font_file(system: Path, filename: str) -> None:
    value = filename.strip()
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value) is None:
        raise OracleError("WIN.INI font registration path is unsafe", exit_code=EXIT_INTEGRITY)
    targets = [system / value]
    if Path(value).suffix.casefold() == ".fot":
        targets.append(system / (Path(value).stem + ".TTF"))
    for target in targets:
        try:
            info = target.lstat()
        except FileNotFoundError as exc:
            raise OracleError(
                "WIN.INI font registration target is missing",
                exit_code=EXIT_INTEGRITY,
            ) from exc
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or not 1 <= info.st_size <= MAX_RUNTIME_FONT_BYTES
        ):
            raise OracleError(
                "WIN.INI font registration target is unsafe",
                exit_code=EXIT_INTEGRITY,
            )


def font_environment_from_runtime(
    runtime: Path,
    *,
    runtime_key: str,
    sealed_tree_digest: str,
    printer_profile: str,
    printer_model: str,
    printer_identity_digest: str,
    printer_device_families: tuple[str, ...] = (),
) -> dict[str, object]:
    """Inventory deterministic Windows registrations without loading font payloads."""

    if (
        not isinstance(runtime_key, str)
        or re.fullmatch(r"[0-9a-f]{64}", runtime_key) is None
        or not isinstance(sealed_tree_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", sealed_tree_digest) is None
        or not isinstance(printer_profile, str)
        or not printer_profile
        or not isinstance(printer_model, str)
        or not printer_model
        or not isinstance(printer_identity_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", printer_identity_digest) is None
    ):
        raise OracleError("invalid printer identity for font inventory", exit_code=EXIT_INTEGRITY)

    parser, win_ini = _read_win_ini(runtime / "WINDOWS" / "WIN.INI")
    fonts_section = _section_name(parser, "fonts")
    substitutes_section = _section_name(parser, "FontSubstitutes")
    if fonts_section is None:
        raise OracleError("WIN.INI lacks the registered font inventory", exit_code=EXIT_INTEGRITY)

    installed: set[str] = set()
    registrations = list(parser.items(fonts_section, raw=True))
    if not registrations or len(registrations) > MAX_RUNTIME_FONT_FILES:
        raise OracleError(
            "WIN.INI font registrations are outside their bound",
            exit_code=EXIT_INTEGRITY,
        )
    system = runtime / "WINDOWS" / "SYSTEM"
    for label, filename in registrations:
        _validate_registered_font_file(system, filename)
        installed.update(_family_variants(label))

    aliases: list[dict[str, str]] = []
    if substitutes_section is not None:
        items = list(parser.items(substitutes_section, raw=True))
        if len(items) > MAX_FONT_FAMILIES:
            raise OracleError(
                "WIN.INI font substitutes exceed their bound",
                exit_code=EXIT_INTEGRITY,
            )
        for source, target in items:
            source = source.strip()
            target = target.strip()
            if _font_key(source) and _font_key(target):
                aliases.append({"source": source, "target": target})

    device = sorted({name.strip() for name in printer_device_families if _font_key(name)})
    identity: dict[str, object] = {
        "schema": FONT_ENVIRONMENT_SCHEMA,
        "runtime_key": runtime_key,
        "sealed_tree_digest": sealed_tree_digest,
        "printer_profile": printer_profile,
        "printer_model": printer_model,
        "printer_identity_digest": printer_identity_digest,
        "win_ini_sha256": hashlib.sha256(win_ini).hexdigest(),
        "registered_face_count": len(registrations),
        "installed_families": sorted(installed, key=lambda value: (value.casefold(), value)),
        "explicit_substitutes": sorted(
            aliases,
            key=lambda item: (item["source"].casefold(), item["source"]),
        ),
        "printer_device_families": device,
        "printer_device_inventory": "enumerated" if device else "not-enumerated",
        **_runtime_font_file_counts(runtime),
    }
    return {**identity, "environment_digest": digest_json(identity)}


def classify_document_fonts(
    payload: bytes,
    environment: dict[str, object] | None,
) -> dict[str, object]:
    requests, scan = _requested_fonts(payload)
    installed_by_key: dict[str, str] = {}
    aliases_by_key: dict[str, tuple[str, str]] = {}
    devices_by_key: dict[str, str] = {}
    environment_digest: str | None = None

    if environment is not None:
        environment_identity = dict(environment)
        recorded_digest = environment_identity.pop("environment_digest", None)
        if (
            environment.get("schema") != FONT_ENVIRONMENT_SCHEMA
            or not isinstance(recorded_digest, str)
            or digest_json(environment_identity) != recorded_digest
        ):
            raise OracleError("invalid font environment", exit_code=EXIT_INTEGRITY)
        environment_digest = recorded_digest
        for family in environment.get("installed_families", []):
            if isinstance(family, str):
                installed_by_key.setdefault(_font_key(family), family)
        for item in environment.get("explicit_substitutes", []):
            if isinstance(item, dict):
                source = item.get("source")
                target = item.get("target")
                if isinstance(source, str) and isinstance(target, str):
                    aliases_by_key.setdefault(_font_key(source), (source, target))
        for family in environment.get("printer_device_families", []):
            if isinstance(family, str):
                devices_by_key.setdefault(_font_key(family), family)

    occurrences: Counter[str] = Counter()
    display: dict[str, str] = {}
    sources: dict[str, set[str]] = defaultdict(set)
    family_limit_exceeded = False
    for family, source in requests:
        key = _font_key(family)
        if key not in display and len(display) >= MAX_FONT_FAMILIES:
            family_limit_exceeded = True
            continue
        display.setdefault(key, family)
        occurrences[key] += 1
        sources[key].add(source)
    scan["truncated"] = bool(scan["truncated"] or family_limit_exceeded)

    classifications: list[dict[str, object]] = []
    status_counts: Counter[str] = Counter()
    unresolved: list[str] = []
    for key in sorted(display, key=lambda item: (display[item].casefold(), display[item])):
        family = display[key]
        record: dict[str, object] = {
            "family": family,
            "occurrences": occurrences[key],
            "sources": sorted(sources[key]),
        }
        if key in installed_by_key:
            record.update({"status": "installed", "matched_family": installed_by_key[key]})
        elif key in aliases_by_key and (
            _font_key(aliases_by_key[key][1]) in installed_by_key
            or _font_key(aliases_by_key[key][1]) in devices_by_key
        ):
            source, target = aliases_by_key[key]
            record.update(
                {
                    "status": "explicit-alias",
                    "matched_family": source,
                    "substitute_family": target,
                }
            )
        elif key in devices_by_key:
            record.update(
                {"status": "printer-device", "matched_family": devices_by_key[key]}
            )
        else:
            record["status"] = "native-substitution-unresolved"
            unresolved.append(family)
        status_counts[str(record["status"])] += 1
        classifications.append(record)

    incomplete = bool(scan["truncated"] or scan["malformed_count"])
    if environment is None and classifications:
        fidelity = "font-environment-unavailable"
    elif unresolved or incomplete:
        fidelity = "degraded-or-unknown"
    elif classifications:
        fidelity = "resolved-by-runtime-inventory"
    else:
        fidelity = "no-explicit-font-families"
    return {
        "schema": FONT_RESOLUTION_SCHEMA,
        "environment_digest": environment_digest,
        "fidelity": fidelity,
        "requested_family_count": len(classifications),
        "requested_occurrence_count": sum(occurrences.values()),
        "status_counts": dict(sorted(status_counts.items())),
        "families": classifications,
        "unresolved_families": unresolved,
        "strict_blocker_count": len(unresolved) + int(incomplete),
        "scan": scan,
        "diagnostics": [
            "printer-device fonts are classified only when the runtime inventory enumerates them",
            "unresolved names may be silently mapped by Ami Pro, Windows GDI, or PSCRIPT",
            "the derived PDF font inventory cannot reliably map output fonts back to source names",
        ],
    }


def font_policy(*, require_installed_fonts: bool) -> dict[str, object]:
    if type(require_installed_fonts) is not bool:
        raise OracleError("invalid strict font policy", exit_code=EXIT_INTEGRITY)
    return {
        "require_installed_fonts": require_installed_fonts,
        "accepted_statuses": ["installed", "explicit-alias", "printer-device"],
        "unresolved_default": "allow-with-degraded-fidelity",
        "unresolved_strict": "block-before-native-execution",
    }


def font_policy_blocks(resolution: object, *, require_installed_fonts: bool) -> bool:
    if not require_installed_fonts:
        return False
    return (
        not isinstance(resolution, dict)
        or resolution.get("schema") != FONT_RESOLUTION_SCHEMA
        or type(resolution.get("strict_blocker_count")) is not int
        or resolution["strict_blocker_count"] > 0
    )


def font_fidelity_degraded(resolution: object) -> bool:
    return isinstance(resolution, dict) and resolution.get("fidelity") in {
        "degraded-or-unknown",
        "font-environment-unavailable",
    }
