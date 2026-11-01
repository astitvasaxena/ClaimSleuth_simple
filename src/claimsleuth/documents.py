"""Line-numbered "Label: value" extraction from submitted text documents.

Every extracted value keeps its document id, line number and verbatim line, so any later
statement about it can be checked against the source.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from claimsleuth.schemas import DocumentKind, EvidenceDocument, EvidenceRef, make_evidence_id

_LABEL_LINE = re.compile(r"^\s*([A-Za-z][A-Za-z ]{1,40}?)\s*:\s*(.+?)\s*$")

# Field name -> accepted labels (lower case), per document kind.
FIELD_LABELS: dict[DocumentKind, dict[str, tuple[str, ...]]] = {
    DocumentKind.INVOICE: {
        "purchase_date": ("purchase date",),
        "serial_number": ("serial number", "serial no"),
        "customer_name": ("customer",),
        "policy_id": ("policy number", "policy id", "policy"),
    },
    DocumentKind.DIAGNOSTIC: {
        "serial_number": ("serial number", "serial no"),
        "inspection_date": ("date of inspection", "date of test", "inspection date", "date"),
        "diagnosis": ("diagnosis", "technician findings"),
        "repair_cost": ("estimated cost", "repair cost"),
        "customer_name": ("client", "customer"),
    },
}

AUTHORITY = {DocumentKind.INVOICE: "PURCHASE_RECORD", DocumentKind.DIAGNOSTIC: "TECH_REPORT",
             DocumentKind.OTHER: "CUSTOMER_TEXT"}


@dataclass
class ExtractedFields:
    doc: EvidenceDocument
    values: dict[str, EvidenceRef] = field(default_factory=dict)
    conflicts: list[tuple[EvidenceRef, EvidenceRef]] = field(default_factory=list)

    def value(self, name: str) -> str | None:
        ref = self.values.get(name)
        return ref.value if ref else None


def extract_fields(doc: EvidenceDocument) -> ExtractedFields:
    """First occurrence of a field wins; a later line with a different value is a conflict."""
    labels = FIELD_LABELS.get(doc.kind, {})
    label_to_field = {label: name for name, options in labels.items() for label in options}
    out = ExtractedFields(doc=doc)
    for line_no, line in doc.numbered_lines():
        match = _LABEL_LINE.match(line)
        if not match:
            continue
        name = label_to_field.get(match.group(1).strip().lower())
        if name is None:
            continue
        ref = EvidenceRef(
            evidence_id=make_evidence_id(doc.doc_id, line_no, name), doc_id=doc.doc_id, line=line_no,
            field=name, value=match.group(2), quote=line.strip()[:500], authority=AUTHORITY[doc.kind],
        )
        existing = out.values.get(name)
        if existing is None:
            out.values[name] = ref
        elif existing.value.strip().lower() != ref.value.strip().lower():
            out.conflicts.append((existing, ref))
    return out


def quote_appears(doc: EvidenceDocument, line: int, quote: str) -> bool:
    """True when the quote appears on that line of the document (whitespace and case normalised)."""
    lines = doc.text.splitlines()
    if not 1 <= line <= len(lines):
        return False
    norm = lambda s: " ".join(s.lower().split())
    return norm(quote) in norm(lines[line - 1])
