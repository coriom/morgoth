"""Bounded, data-only documentation evidence for opt-in capability proposals."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from agents.generalist_capability import CapabilityRefusal
from agents.generalist_generic_read import _documentation_identity


@dataclass(frozen=True)
class DocumentationEvidence:
    """Operator-reviewed source assertion; not API behavior proof or authority."""

    source_identity: str
    content_digest: str
    provenance: str
    size_bytes: int


def reviewed_offline_snapshot(path: Path, source_identity: str,
                              expected_digest: str) -> DocumentationEvidence:
    """Bind a small offline snapshot to an independently reviewed source ID."""
    try:
        identity = _documentation_identity(source_identity)
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(
                Path(__file__).resolve().parents[1]):
            raise ValueError("unreviewed snapshot")
        data = path.read_bytes()
        if not 1 <= len(data) <= 16384 or sha256(data).hexdigest() != expected_digest:
            raise ValueError("snapshot digest")
        text = data.decode("utf-8")
        if any(ord(char) < 32 and char not in "\n\r\t" for char in text):
            raise ValueError("snapshot control")
        return DocumentationEvidence(identity, expected_digest,
                                     "operator_reviewed_offline_snapshot", len(data))
    except (OSError, UnicodeError, ValueError):
        raise CapabilityRefusal("DOCUMENT_SOURCE_UNVERIFIED") from None
