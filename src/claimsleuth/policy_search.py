"""Keyword search over the synthetic policy register (data/policies.json).

Backs the MCP tool warranty_policy_search. Results quote the stored policy terms only; every matched
term carries a JSON-pointer source into policies.json, so nothing returned is generated text.
"""
from __future__ import annotations

import re
from pathlib import Path

from claimsleuth import config
from claimsleuth.datastore import load_policies
from claimsleuth.schemas import PolicyMatch, PolicyMatchedTerm, PolicySearchInput, PolicySearchOutput, PolicyTerms

STOPWORDS = {"a", "an", "the", "of", "for", "and", "or", "is", "are", "what", "which", "does", "do", "to", "in",
             "on", "by", "with", "my", "it", "this", "that", "policy", "policies", "warranty", "covered", "cover"}

# Query words that point at a numeric/boolean policy term.
TERM_KEYWORDS: dict[str, set[str]] = {
    "duration_months": {"duration", "period", "month", "expiry", "expire", "window", "length", "term"},
    "deductible": {"deductible", "excess"},
    "max_coverage_limit": {"limit", "maximum", "max", "cap", "capped", "coverage"},
    "depreciation_rate": {"depreciation", "depreciate"},
    "requires_original_receipt": {"receipt", "invoice", "proof", "purchase"},
    "requires_diagnostic_report": {"diagnostic", "report", "technician", "repair"},
}


def _words(text: str) -> set[str]:
    # Trailing "s" is dropped so "laptops" matches "laptop".
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in re.findall(r"[a-z0-9]+", text.lower())}


def _searchable(policy: PolicyTerms) -> list[tuple[str, str]]:
    """(json-pointer field, text) pairs that a query can match."""
    pairs = [("policy_id", policy.policy_id), ("name", policy.name), ("product_category", policy.product_category)]
    pairs += [(f"exclusions/{i}", text) for i, text in enumerate(policy.exclusions)]
    return pairs


def _match(policy: PolicyTerms, query_words: set[str], source_file: str) -> tuple[float, list[PolicyMatchedTerm]]:
    matched_words: set[str] = set()
    terms: list[PolicyMatchedTerm] = []
    for field, text in _searchable(policy):
        hit = query_words & _words(text)
        if hit:
            matched_words |= hit
            terms.append(PolicyMatchedTerm(field=field, value=text,
                                           source=f"{source_file}#/{policy.policy_id}/{field}"))
    for field, keywords in TERM_KEYWORDS.items():
        hit = query_words & keywords
        if hit:
            matched_words |= hit
            terms.append(PolicyMatchedTerm(field=field, value=str(getattr(policy, field)),
                                           source=f"{source_file}#/{policy.policy_id}/{field}"))
    return len(matched_words) / len(query_words), terms


def search_policies(request: PolicySearchInput, data_dir: Path = config.DATA_DIR) -> PolicySearchOutput:
    policies = load_policies(data_dir)
    source_file = config.policies_file(data_dir).name
    if request.policy_id is not None:
        candidates = [policies[request.policy_id]] if request.policy_id in policies else []
    else:
        candidates = list(policies.values())

    query_words = _words(request.search_query or "") - STOPWORDS
    if request.search_query is not None and not query_words:
        raise ValueError("search_query contains no searchable words")

    scored: list[PolicyMatch] = []
    for policy in candidates:
        if query_words:
            score, terms = _match(policy, query_words, source_file)
            if score == 0:
                continue
        else:
            score, terms = None, []
        scored.append(PolicyMatch(policy_id=policy.policy_id, name=policy.name,
                                  product_category=policy.product_category, terms=policy, matched_terms=terms,
                                  score=None if score is None else round(score, 3),
                                  source=f"{source_file}#/{policy.policy_id}"))
    scored.sort(key=lambda m: (-(m.score or 0), m.policy_id))
    return PolicySearchOutput(policy_id=request.policy_id, search_query=request.search_query,
                              matches=scored[:request.limit], total_matches=len(scored),
                              sources=sorted({m.source for m in scored[:request.limit]}))
