"""The claim packet given to every LLM agent (ADK and CrewAI), so all of them see the same original claim.

The claim form and every document line are wrapped as untrusted data; document lines are numbered
"doc_id#L<n>: text" so agents can cite them and the citations can be checked afterwards.
"""
from __future__ import annotations

import json

from claimsleuth.schemas import ClaimInput, EvidenceDocument
from claimsleuth.security import wrap_untrusted


def claim_message(claim: ClaimInput, documents: list[EvidenceDocument], run_id: str) -> str:
    form = claim.model_dump(mode="json", exclude={"documents"})
    parts = [f"Investigate warranty claim {claim.claim_id} (run {run_id}).",
             "Documents submitted: " + (", ".join(f"{d.doc_id} ({d.kind.value})" for d in documents) or "none") + ".",
             wrap_untrusted(json.dumps(form, indent=2), "claim_form")]
    for doc in documents:
        numbered = "\n".join(f"{doc.doc_id}#L{n}: {line}" for n, line in doc.numbered_lines())
        parts.append(wrap_untrusted(numbered, f"doc:{doc.doc_id}"))
    return "\n\n".join(parts)
