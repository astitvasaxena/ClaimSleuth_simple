"""The dashboard's claim workspace: the data directory that the MCP server, the CrewAI A2A service and the
in-process ADK/baseline code all read when the dashboard runs an investigation.

    runtime/dashboard/data/policies.json            copy of data/policies.json
    runtime/dashboard/data/claims/<claim id>/       built-in sample claims (copied from data/claims),
                                                    uploaded PDF claims and security-test variants
    runtime/dashboard/uploads/<upload id>.pdf       uploaded PDFs awaiting submission

The repository's data/ folder is never written to, and data/ground_truth.json is never copied here.
Each claim folder has an origin.json (built-in, uploaded PDF or security test) next to the usual
claim.json and .txt documents; the services ignore it.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from claimsleuth import config
from claimsleuth.dashboard.pdf_intake import ClaimDraft, PdfIntakeError, parse_claim_pdf
from claimsleuth.datastore import list_claim_ids, load_claim, load_policies
from claimsleuth.schemas import ClaimInput, EvidenceDocument, utc_now

DEFAULT_ROOT = config.RUNTIME_DIR / "dashboard"
_UPLOAD_ID = re.compile(r"^[0-9a-f]{16}$")


class WorkspaceError(ValueError):
    pass


class Workspace:
    def __init__(self, root: Path = DEFAULT_ROOT, source_data: Path = config.DATA_DIR) -> None:
        self.root, self.source_data = root, source_data
        self.data_dir = root / "data"
        self.claims_dir = config.claims_dir(self.data_dir)
        self.uploads = root / "uploads"

    # ------------------------------------------------------------------ setup
    def prepare(self) -> None:
        """Copy the policy register and the built-in sample claims (refreshed on every start)."""
        self.claims_dir.mkdir(parents=True, exist_ok=True)
        self.uploads.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(config.policies_file(self.source_data), config.policies_file(self.data_dir))
        for claim_id in list_claim_ids(self.source_data):
            target = self.claims_dir / claim_id
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(config.claims_dir(self.source_data) / claim_id, target)
            self._origin(claim_id, {"origin": "built-in sample", "source": f"data/claims/{claim_id}"})

    def _origin(self, claim_id: str, info: dict[str, Any]) -> None:
        (self.claims_dir / claim_id / "origin.json").write_text(json.dumps(info, indent=2), encoding="utf-8")

    def origin(self, claim_id: str) -> dict[str, Any] | None:
        path = self.claims_dir / claim_id / "origin.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def policy_ids(self) -> set[str]:
        return set(load_policies(self.data_dir))

    # ------------------------------------------------------------------ listing and loading
    def claims(self) -> list[dict[str, Any]]:
        out = []
        for claim_id in list_claim_ids(self.data_dir):
            try:
                claim, docs = load_claim(claim_id, self.data_dir)
            except Exception as exc:      # a broken folder is listed with its error, not hidden
                out.append({"claim_id": claim_id, "error": f"{type(exc).__name__}: {exc}"})
                continue
            origin = self.origin(claim_id) or {}
            out.append({"claim_id": claim_id, "origin": origin.get("origin", "unknown"),
                        "pdf": origin.get("filename"), "claimant_name": claim.claimant_name,
                        "device_model": claim.device_model, "policy_id": claim.policy_id,
                        "documents": [f"{d.doc_id} ({d.kind.value})" for d in docs]})
        return out

    def load(self, claim_id: str) -> tuple[ClaimInput, list[EvidenceDocument]]:
        """A claim from the workspace, else from the repository data folder (older runs)."""
        try:
            return load_claim(claim_id, self.data_dir)
        except FileNotFoundError:
            return load_claim(claim_id, self.source_data)

    # ------------------------------------------------------------------ uploads
    def store_upload(self, data: bytes, filename: str | None) -> ClaimDraft:
        draft = parse_claim_pdf(data, filename, self.policy_ids())
        self.uploads.mkdir(parents=True, exist_ok=True)
        (self.uploads / f"{draft.upload_id}.pdf").write_bytes(data)
        (self.uploads / f"{draft.upload_id}.json").write_text(json.dumps({"filename": draft.filename}), encoding="utf-8")
        return draft

    def load_draft(self, upload_id: str) -> tuple[ClaimDraft, bytes]:
        if not _UPLOAD_ID.fullmatch(upload_id or ""):
            raise WorkspaceError("invalid upload id")
        pdf = self.uploads / f"{upload_id}.pdf"
        if not pdf.exists():
            raise WorkspaceError("unknown upload id; upload the PDF again")
        meta = json.loads((self.uploads / f"{upload_id}.json").read_text(encoding="utf-8"))
        data = pdf.read_bytes()
        return parse_claim_pdf(data, meta.get("filename"), self.policy_ids()), data

    def materialise_upload(self, upload_id: str) -> str:
        """Write the extracted claim as a claim folder the services can read; returns its claim id."""
        try:
            draft, data = self.load_draft(upload_id)
        except PdfIntakeError as exc:
            raise WorkspaceError(str(exc)) from exc
        if not draft.can_submit:
            raise WorkspaceError("the PDF cannot be submitted: " + "; ".join(draft.errors))
        claim = draft.claim
        folder = self.claims_dir / claim.claim_id
        existing = self.origin(claim.claim_id) if folder.exists() else None
        if existing is not None:
            if existing.get("origin") != "uploaded PDF":
                raise WorkspaceError(f"claim id {claim.claim_id} belongs to a {existing.get('origin')} claim; "
                                     "an uploaded claim must use a new claim id")
            if existing.get("sha256") != draft.sha256:
                raise WorkspaceError(f"claim id {claim.claim_id} was already submitted from a different PDF "
                                     f"(sha256 {existing.get('sha256', '')[:12]}...)")
            return claim.claim_id
        folder.mkdir(parents=True)
        (folder / "claim.json").write_text(claim.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
        for doc in draft.documents:
            (folder / doc.filename).write_text(doc.text, encoding="utf-8", newline="\n")
        (folder / "source.pdf").write_bytes(data)
        fields = {f: {"value": v.value, "page": v.page, "line": v.line} for f, v in draft.fields.items()}
        self._origin(claim.claim_id, {"origin": "uploaded PDF", "filename": draft.filename, "sha256": draft.sha256,
                                      "pages": draft.pages, "submitted_at": utc_now().isoformat(),
                                      "fields": fields, "line_map": draft.line_map()})
        return claim.claim_id

    # ------------------------------------------------------------------ security-test variants
    def write_variant(self, base_claim_id: str, claim_id: str, statement: str, scenario: str) -> str:
        """Same documents as a built-in claim, with the claimant statement replaced (security scenarios)."""
        source = self.claims_dir / base_claim_id
        target = self.claims_dir / claim_id
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
        claim = ClaimInput.model_validate_json((target / "claim.json").read_text(encoding="utf-8"))
        claim = claim.model_copy(update={"claim_id": claim_id, "claimant_statement": statement})
        ClaimInput.model_validate(claim.model_dump())
        (target / "claim.json").write_text(claim.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
        self._origin(claim_id, {"origin": "security test", "scenario": scenario, "base_claim": base_claim_id})
        return claim_id
