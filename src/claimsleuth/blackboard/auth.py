"""Local writer authentication for the shared coordination blackboard (standard library only).

Three writer principals, each with its own secret key in <database>.keys/<principal>.key:

    orchestrator  may write as: controller, adjudicator   (the dashboard / integration process)
    adk           may write as: evidence_agent, investigator_agent
    crewai        may write as: forensic_agent              (the CrewAI A2A service process)

The orchestrator provisions the keys when it registers a run. A writer must present its principal's key (checked
against the key file on every write) and may only write as the authors bound to that principal. Every entry is
signed with HMAC-SHA256 over its content, so a reader detects entries that were inserted without a key (for example
by raw SQL), tampered with, or written under the wrong identity.

Limitation: the key files are protected only by the operating-system account (read access for the current user).
This authenticates and separates the ClaimSleuth writers and makes tampering evident; it does not stop a malicious
program running as the same OS user, which could read the key files.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PRINCIPALS: dict[str, frozenset[str]] = {
    "orchestrator": frozenset({"controller", "adjudicator"}),
    "adk": frozenset({"evidence_agent", "investigator_agent"}),
    "crewai": frozenset({"forensic_agent"}),
}
AGENT_PRINCIPALS = frozenset({"adk", "crewai"})
SIGNED_FIELDS = ("run_id", "run_seq", "claim_id", "ts", "kind", "section", "author", "framework", "process",
                 "fingerprint", "payload_sha256", "ref_seq", "principal")


class BlackboardAuthError(Exception):
    """A writer could not be authenticated or is not allowed to write as the given author."""


@dataclass(frozen=True)
class Writer:
    principal: str
    key: bytes = field(repr=False)


def key_dir(db: Path) -> Path:
    return Path(f"{db}.keys")


def _key_file(db: Path, principal: str) -> Path:
    if principal not in PRINCIPALS:
        raise BlackboardAuthError(f"unknown writer principal {principal!r}")
    return key_dir(db) / f"{principal}.key"


def provision_keys(db: Path) -> None:
    """Create any missing key (exclusive create, so concurrent provisioning cannot overwrite a key)."""
    folder = key_dir(db)
    folder.mkdir(parents=True, exist_ok=True)
    for principal in PRINCIPALS:
        path = _key_file(db, principal)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(secrets.token_hex(32))


def _read_key(db: Path, principal: str) -> bytes | None:
    path = _key_file(db, principal)
    try:
        return bytes.fromhex(path.read_text(encoding="ascii").strip())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise BlackboardAuthError(f"key for {principal} unreadable: {type(exc).__name__}") from exc


def load_writer(db: Path, principal: str) -> Writer:
    key = _read_key(db, principal)
    if key is None:
        raise BlackboardAuthError(f"no credentials provisioned for writer {principal!r} ({key_dir(db)})")
    return Writer(principal, key)


def authenticate(db: Path, writer: Writer, author: str) -> None:
    """The writer's key must match its principal's key file, and the author must belong to that principal."""
    if not isinstance(writer, Writer):
        raise BlackboardAuthError("an authenticated writer is required")
    expected = _read_key(db, writer.principal)
    if expected is None or not hmac.compare_digest(expected, writer.key):
        raise BlackboardAuthError(f"authentication failed for writer {writer.principal!r}")
    if author not in PRINCIPALS[writer.principal]:
        raise BlackboardAuthError(f"writer {writer.principal!r} may not write as {author!r} "
                                  f"(allowed: {', '.join(sorted(PRINCIPALS[writer.principal]))})")


def load_keyring(db: Path) -> dict[str, bytes]:
    return {p: k for p in PRINCIPALS if (k := _read_key(db, p)) is not None}


def _canonical(fields: dict[str, Any]) -> bytes:
    return json.dumps({f: fields.get(f) for f in SIGNED_FIELDS}, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign(writer: Writer, fields: dict[str, Any]) -> str:
    return hmac.new(writer.key, _canonical(fields), hashlib.sha256).hexdigest()


def verify(fields: dict[str, Any], payload_text: str, signature: str | None, keyring: dict[str, bytes]) -> str:
    """'valid', 'unsigned' (no signature, e.g. raw SQL or written before authentication existed), 'invalid'
    (content or signature altered), 'identity' (author not allowed for the principal) or 'unknown-key'."""
    principal = fields.get("principal")
    if not signature or not principal:
        return "unsigned"
    if principal not in PRINCIPALS or fields.get("author") not in PRINCIPALS[principal]:
        return "identity"
    key = keyring.get(principal)
    if key is None:
        return "unknown-key"
    if hashlib.sha256(payload_text.encode("utf-8")).hexdigest() != fields.get("payload_sha256"):
        return "invalid"
    expected = hmac.new(key, _canonical(fields), hashlib.sha256).hexdigest()
    return "valid" if hmac.compare_digest(expected, signature) else "invalid"
