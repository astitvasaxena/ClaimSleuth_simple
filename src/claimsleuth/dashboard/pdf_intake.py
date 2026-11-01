"""Uploaded warranty-claim PDF -> claim form + evidence documents, with PDF page/line provenance.

pypdf reads the PDF's text layer page by page. Lines are grouped into sections by their upper-case headings
(WARRANTY CLAIM FORM, ... INVOICE, SERVICE CENTER / REPAIR / DIAGNOSTIC REPORT). The claim form fills a
`ClaimInput`; the invoice and the repair report become the same line-numbered text documents every other
ClaimSleuth component reads, and every document line keeps the PDF page and line it came from.

Nothing is inferred or filled in: a missing section or field is reported as missing. All text is untrusted
customer data: it is screened with security.screen_text and never interpreted as an instruction.
"""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from pydantic import ValidationError
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from claimsleuth.schemas import ClaimInput, DocumentKind, DocumentRef, EvidenceDocument, GuardrailEvent
from claimsleuth.security import screen_text

MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_PAGES = 20
MAX_LINE_CHARS = 500

_HEADINGS = (("claim_form", re.compile(r"\bCLAIM FORM\b")),
             ("invoice", re.compile(r"\bINVOICE\b")),
             ("repair_report", re.compile(r"\b(SERVICE (CENTER|CENTRE) REPORT|REPAIR REPORT|DIAGNOSTIC REPORT)\b")))
_FOOTER = re.compile(r"(^|\s)Page \d+( of \d+)?\s*$", re.I)
_LABEL = re.compile(r"^\s*([A-Za-z][A-Za-z ]{1,40}?)\s*:\s*(.*?)\s*$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

DOCUMENTS = {"invoice": (DocumentKind.INVOICE, "invoice.txt"),
             "repair_report": (DocumentKind.DIAGNOSTIC, "repair_report.txt"),
             "other_text": (DocumentKind.OTHER, "other_text.txt")}

FORM_LABELS = {
    "claim id": "claim_id", "claim number": "claim_id",
    "claimant name": "claimant_name", "claimant": "claimant_name", "customer name": "claimant_name",
    "claimant id": "claimant_id", "customer id": "claimant_id",
    "policy number": "policy_id", "policy id": "policy_id",
    "product": "device_model", "product name": "device_model", "device": "device_model",
    "serial number": "serial_number", "serial no": "serial_number",
    "purchase date": "purchase_date",
    "incident date": "incident_date", "date of incident": "incident_date",
    "claimed repair amount": "claimed_amount", "claimed amount": "claimed_amount",
    "problem description": "reported_issue", "reported issue": "reported_issue",
    "customer statement": "claimant_statement", "claimant statement": "claimant_statement",
}
FIELD_LABELS = {"claim_id": "Claim ID", "claimant_name": "Claimant Name", "claimant_id": "Claimant ID",
                "policy_id": "Policy Number", "device_model": "Product", "serial_number": "Serial Number",
                "purchase_date": "Purchase Date", "incident_date": "Incident Date",
                "claimed_amount": "Claimed Repair Amount", "reported_issue": "Problem Description",
                "claimant_statement": "Customer Statement"}
REQUIRED = ("claim_id", "claimant_name", "claimant_id", "policy_id", "device_model", "incident_date", "reported_issue")
MULTILINE = ("reported_issue", "claimant_statement")
_DATE_FORMATS = ("%Y-%m-%d", "%d %B %Y", "%B %d, %Y", "%d %b %Y", "%b %d, %Y")


class PdfIntakeError(ValueError):
    """The upload is not a usable PDF (wrong type, too large, unreadable, encrypted, no text layer)."""


@dataclass
class PdfLine:
    page: int
    line: int
    text: str


@dataclass
class FieldValue:
    field: str
    label: str
    value: str
    page: int
    line: int


@dataclass
class ExtractedDocument:
    doc_id: str
    kind: DocumentKind
    filename: str
    lines: list[PdfLine] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(line.text + "\n" for line in self.lines)

    def evidence(self) -> EvidenceDocument:
        raw = self.text.encode("utf-8")
        return EvidenceDocument(doc_id=self.doc_id, kind=self.kind, filename=self.filename,
                                sha256=hashlib.sha256(raw).hexdigest(), text=self.text)


@dataclass
class ClaimDraft:
    upload_id: str
    filename: str
    sha256: str
    size_bytes: int
    pages: int
    fields: dict[str, FieldValue]
    documents: list[ExtractedDocument]
    footer_lines: int = 0
    claim: ClaimInput | None = None
    errors: list[str] = field(default_factory=list)      # block submission
    missing: list[str] = field(default_factory=list)     # missing evidence: reported, never invented
    warnings: list[str] = field(default_factory=list)
    guardrail_events: list[GuardrailEvent] = field(default_factory=list)

    @property
    def can_submit(self) -> bool:
        return self.claim is not None and not self.errors

    def evidence_documents(self) -> list[EvidenceDocument]:
        return [d.evidence() for d in self.documents]

    def line_map(self) -> dict[str, list[list[int]]]:
        """doc_id -> [[pdf page, pdf line], ...] indexed by document line - 1."""
        return {d.doc_id: [[ln.page, ln.line] for ln in d.lines] for d in self.documents}

    def to_dict(self) -> dict[str, Any]:
        return {
            "upload_id": self.upload_id, "filename": self.filename, "sha256": self.sha256,
            "size_bytes": self.size_bytes, "pages": self.pages, "footer_lines_ignored": self.footer_lines,
            "fields": [{"field": f, "label": FIELD_LABELS[f], **({"value": v.value, "page": v.page, "line": v.line}
                                                              if (v := self.fields.get(f)) else {"value": None}),
                        "required": f in REQUIRED} for f in FIELD_LABELS],
            "documents": [{"doc_id": d.doc_id, "kind": d.kind.value, "filename": d.filename,
                           "sha256": d.evidence().sha256,
                           "lines": [{"n": i, "text": ln.text, "page": ln.page, "pdf_line": ln.line}
                                     for i, ln in enumerate(d.lines, start=1)]} for d in self.documents],
            "claim": self.claim.model_dump(mode="json") if self.claim else None,
            "errors": self.errors, "missing": self.missing, "warnings": self.warnings,
            "guardrail_events": [e.model_dump(mode="json") for e in self.guardrail_events],
            "can_submit": self.can_submit,
        }


def safe_filename(name: str | None) -> str:
    base = re.split(r"[\\/]", name or "")[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip("._")[:80]
    return cleaned or "upload.pdf"


def read_pdf_lines(data: bytes) -> tuple[list[PdfLine], int]:
    """Validate the bytes as a PDF and return its text lines with page/line numbers (1-based)."""
    if len(data) > MAX_PDF_BYTES:
        raise PdfIntakeError(f"file is {len(data)} bytes; the limit is {MAX_PDF_BYTES} bytes (5 MB)")
    if not data.startswith(b"%PDF-"):
        raise PdfIntakeError("file is not a PDF (it does not start with the %PDF- signature)")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise PdfIntakeError("the PDF is password protected")
        pages = reader.pages
        if len(pages) > MAX_PAGES:
            raise PdfIntakeError(f"the PDF has {len(pages)} pages; the limit is {MAX_PAGES}")
        lines = []
        for page_no, page in enumerate(pages, start=1):
            text = page.extract_text() or ""
            for line_no, raw in enumerate(text.splitlines(), start=1):
                clean = " ".join(_CONTROL.sub(" ", raw).split())[:MAX_LINE_CHARS]
                if clean:
                    lines.append(PdfLine(page_no, line_no, clean))
    except PdfIntakeError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, OSError) as exc:
        raise PdfIntakeError(f"the PDF could not be read: {type(exc).__name__}: {str(exc)[:200]}") from exc
    if not lines:
        raise PdfIntakeError("the PDF has no text layer (a scanned image?); OCR is not supported")
    return lines, len(pages)


def _heading(text: str) -> str | None:
    if ":" in text or text != text.upper() or not re.search(r"[A-Z]", text):
        return None
    return next((name for name, pattern in _HEADINGS if pattern.search(text)), None)


def _parse_date(value: str) -> date | None:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_claim_pdf(data: bytes, filename: str | None = None, known_policies: set[str] | None = None) -> ClaimDraft:
    lines, page_count = read_pdf_lines(data)
    sha = hashlib.sha256(data).hexdigest()
    sections: dict[str, list[PdfLine]] = {}
    current, footers = "other_text", 0
    for ln in lines:
        if _FOOTER.search(ln.text):
            footers += 1
            continue
        current = _heading(ln.text) or current
        sections.setdefault(current, []).append(ln)

    fields: dict[str, FieldValue] = {}
    last: str | None = None
    previous_page = None
    for ln in sections.get("claim_form", []):
        match = _LABEL.match(ln.text)
        name = FORM_LABELS.get(match.group(1).strip().lower()) if match else None
        if name is not None:
            if name not in fields and match.group(2):
                fields[name] = FieldValue(name, FIELD_LABELS[name], match.group(2), ln.page, ln.line)
            last = name if name in MULTILINE else None
        elif last is not None and ln.page == previous_page and _heading(ln.text) is None:
            fields[last].value += " " + ln.text           # wrapped continuation of a free-text field
        else:
            last = None
        previous_page = ln.page

    documents = [ExtractedDocument(doc_id, *DOCUMENTS[doc_id], lines=sections[doc_id])
                 for doc_id in ("invoice", "repair_report", "other_text") if sections.get(doc_id)]
    draft = ClaimDraft(upload_id=sha[:16], filename=safe_filename(filename), sha256=sha, size_bytes=len(data),
                       pages=page_count, fields=fields, documents=documents, footer_lines=footers)

    if "claim_form" not in sections:
        draft.errors.append("no WARRANTY CLAIM FORM section was found in the PDF")
    for name in REQUIRED:
        if name not in fields:
            draft.errors.append(f"claim form field missing: {FIELD_LABELS[name]}")
    if "invoice" not in sections:
        draft.missing.append("purchase invoice (no INVOICE section in the PDF)")
    if "repair_report" not in sections:
        draft.missing.append("diagnostic / repair report (no SERVICE CENTER / REPAIR REPORT section in the PDF)")
    for name in ("serial_number", "purchase_date", "claimant_statement"):
        if name not in fields:
            draft.warnings.append(f"claim form field not found: {FIELD_LABELS[name]}")
    if "other_text" in sections:
        draft.warnings.append(f"{len(sections['other_text'])} line(s) outside the recognised sections were kept as "
                              "an OTHER document so that nothing is silently dropped")

    incident = None
    if "incident_date" in fields:
        incident = _parse_date(fields["incident_date"].value)
        if incident is None:
            draft.errors.append(f"Incident Date '{fields['incident_date'].value}' is not a recognised date "
                                "(use YYYY-MM-DD)")
    if not draft.errors:
        try:
            draft.claim = ClaimInput(
                claim_id=fields["claim_id"].value.strip().upper(), policy_id=fields["policy_id"].value.strip().upper(),
                claimant_id=fields["claimant_id"].value.strip().upper(), claimant_name=fields["claimant_name"].value,
                device_model=fields["device_model"].value,
                serial_number=fields["serial_number"].value if "serial_number" in fields else None,
                incident_date=incident, reported_issue=fields["reported_issue"].value,
                claimant_statement=fields["claimant_statement"].value if "claimant_statement" in fields else "",
                documents=[DocumentRef(doc_id=d.doc_id, kind=d.kind, filename=d.filename) for d in documents])
            draft.evidence_documents()          # enforces the EvidenceDocument size limit
        except ValidationError as exc:
            draft.claim = None
            draft.errors += [f"{'.'.join(map(str, e['loc'])) or 'claim'}: {e['msg']}" for e in exc.errors()]
    if draft.claim is not None and known_policies is not None and draft.claim.policy_id not in known_policies:
        draft.warnings.append(f"policy {draft.claim.policy_id} is not in the policy register; the deterministic "
                              "rules will escalate the claim")

    for name in ("claimant_statement", "reported_issue"):
        if name in fields:
            v = fields[name]
            draft.guardrail_events += screen_text(v.value, f"claim.{name} (PDF p.{v.page} L{v.line})")
    for doc in documents:
        for n, ln in enumerate(doc.lines, start=1):
            draft.guardrail_events += screen_text(ln.text, f"doc:{doc.doc_id}#L{n} (PDF p.{ln.page} L{ln.line})")
    return draft
