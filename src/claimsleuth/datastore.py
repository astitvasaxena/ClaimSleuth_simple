"""Loading claims, their documents, policies and ground-truth labels from the data folder."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from claimsleuth import config
from claimsleuth.schemas import ClaimInput, EvidenceDocument, PolicyTerms


def list_claim_ids(data_dir: Path = config.DATA_DIR) -> list[str]:
    root = config.claims_dir(data_dir)
    return sorted(p.name for p in root.iterdir() if (p / "claim.json").exists()) if root.exists() else []


def load_claim(claim_id: str, data_dir: Path = config.DATA_DIR) -> tuple[ClaimInput, list[EvidenceDocument]]:
    folder = config.claims_dir(data_dir) / claim_id
    if not (folder / "claim.json").exists():
        raise FileNotFoundError(f"unknown claim: {claim_id}")
    claim = ClaimInput.model_validate_json((folder / "claim.json").read_text(encoding="utf-8"))
    docs = []
    for ref in claim.documents:
        path = folder / ref.filename
        if not path.exists():
            # A document listed but not supplied is missing evidence, never filled in.
            continue
        raw = path.read_bytes()
        docs.append(EvidenceDocument(doc_id=ref.doc_id, kind=ref.kind, filename=ref.filename,
                                     sha256=hashlib.sha256(raw).hexdigest(), text=raw.decode("utf-8")))
    return claim, docs


def load_policies(data_dir: Path = config.DATA_DIR) -> dict[str, PolicyTerms]:
    data = json.loads(config.policies_file(data_dir).read_text(encoding="utf-8"))
    return {pid: PolicyTerms.model_validate(terms) for pid, terms in data.items()}


def load_ground_truth(data_dir: Path = config.DATA_DIR) -> dict[str, dict]:
    """Evaluation labels. Only the evaluation harness and tests may call this."""
    return json.loads(config.ground_truth_file(data_dir).read_text(encoding="utf-8"))
