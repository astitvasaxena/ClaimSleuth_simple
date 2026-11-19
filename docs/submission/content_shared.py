"""Single source of truth for the literature table, requirements R1..R11 and references.
Imported by build_report.py and build_ppt.py so both documents say the same thing.
Every reference below was checked against the publisher's or vendor's own page (see report Section 3)."""

REQ = [
    ("R1", "Read the claim PDFs and keep page and line provenance for every fact; report missing fields, never invent them."),
    ("R2", "Use several specialist agents with separate jobs, built on two different frameworks (Google ADK and CrewAI), so that their opinions are independent."),
    ("R3", "Share intermediate context through a blackboard that checks who may read what and detects tampering."),
    ("R4", "Give the agents their tools through MCP: at least two tools, read-only, scoped to the claim under investigation, with every call logged."),
    ("R5", "Let agents in different environments talk through the A2A protocol with a validated message contract."),
    ("R6", "Keep the decision and every rupee in deterministic code; agents only recommend and cannot approve a payout."),
    ("R7", "Resist prompt injection and unsafe output: untrusted-input handling, guardrails, callbacks, output checks."),
    ("R8", "Fail closed: a missing or failed agent, tool or record sends the claim to a human, never to a silent approval."),
    ("R9", "Make every run observable and auditable: one correlated trace, provenance, an append-only audit trail."),
    ("R10", "Be evaluated on a reproducible benchmark with time and cost reported, and be honest about what is and is not measured."),
    ("R11", "Run on a cloud platform from a single machine image so that nothing has to run on a laptop during the demonstration."),
]

REF_LIST = [   # (key, entry) in the order the report first cites them; the report cites by number via cite()
    ("tractable", "Tractable. tractable.ai (accessed November 2026)."),
    ("shift", "Shift Technology. shift-technology.com (accessed November 2026)."),
    ("verisk", "Verisk. ClaimSearch. verisk.com/insurance/products/claimsearch (accessed November 2026)."),
    ("extend", "Extend. extend.com (accessed November 2026)."),
    ("adk", "Google. Agent Development Kit (ADK) documentation. google.github.io/adk-docs (accessed November 2026)."),
    ("crewai", "CrewAI documentation. docs.crewai.com (accessed November 2026)."),
    ("autogen", "Wu, Q., Bansal, G., Zhang, J., et al. (2023). AutoGen: Enabling Next-Gen LLM Applications via Multi-Agent Conversation. arXiv:2308.08155."),
    ("mcp", "Model Context Protocol. What is the Model Context Protocol (MCP)? modelcontextprotocol.io (accessed November 2026)."),
    ("a2a", "A2A Protocol. Agent2Agent Protocol, an open standard for AI agent interoperability. a2a-protocol.org (accessed November 2026)."),
    ("han", "Han, B. and Zhang, S. (2025). Exploring Advanced LLM Multi-Agent Systems Based on Blackboard Architecture. arXiv:2507.01701."),
    ("salemi", "Salemi, A., Parmar, M., Goyal, P., et al. (2025). LLM-Based Multi-Agent Blackboard System for Information Discovery in Data Science. arXiv:2510.01285."),
    ("terrarium", "Nakamura, M., Kumar, A., Mahmud, S., et al. (2025). Terrarium: Revisiting the Blackboard for Multi-Agent Safety, Privacy, and Security Studies. arXiv:2510.14312."),
    # added 2026-11-20; each checked on arxiv.org or the DOI record (Semantic Scholar, Crossref), no venue claimed beyond what those show
    ("viaene", "Viaene, S., Derrig, R., Baesens, B. and Dedene, G. (2002). A Comparison of State-of-the-Art Classification Techniques for Expert Automobile Insurance Claim Fraud Detection. Journal of Risk and Insurance, 69(3), 373-421. doi:10.1111/1539-6975.00023."),
    ("guo", "Guo, T., Chen, X., Wang, Y., et al. (2024). Large Language Model based Multi-Agents: A Survey of Progress and Challenges. arXiv:2402.01680."),
    ("greshake", "Greshake, K., Abdelnabi, S., Mishra, S., Endres, C., Holz, T. and Fritz, M. (2023). Not what you've signed up for: Compromising Real-World LLM-Integrated Applications with Indirect Prompt Injection. arXiv:2302.12173."),
    ("nii", "Nii, H. P. (1994). Blackboard systems at the architecture level. Expert Systems with Applications, 7(1), 43-54. doi:10.1016/0957-4174(94)90025-6."),
]
REFS = [entry for _, entry in REF_LIST]
_REF_NO = {key: i for i, (key, _) in enumerate(REF_LIST, 1)}


def cite(*keys):
    """Numbered citation, e.g. cite("han", "salemi") -> "[10, 11]"."""
    return "[" + ", ".join(str(_REF_NO[k]) for k in keys) + "]"


LIT_ROWS = [
    ("Tractable " + cite("tractable") + "\n(tractable.ai)",
     "AI damage detection and assessment, mainly for vehicle appraisal; its page says it streamlines claims workflows for insurers, repairers and others.",
     "Works from photographs of damage. We found no description of cross-checking claim form, invoice and repair report, or of citing document lines.",
     "R1, R2"),
    ("Shift Technology " + cite("shift") + "\n(shift-technology.com)",
     "\"AI agents\" for insurers, with separate agent solutions for coverage and liability, fraud and risk, subrogation and claims. Its page also lists a data network of third-party sources.",
     "Commercial and closed. Public material does not say how agent output is verified before a payout, how context is shared between agents, or what stops an agent from approving a claim.",
     "R3, R6, R7"),
    ("Verisk ClaimSearch " + cite("verisk") + "\n(verisk.com)",
     "A large property and casualty claims database for claim analysis and fraud detection.",
     "A data service, not an investigation workflow. It is the kind of source our `claim_history_lookup` tool stands in for, but over synthetic data and with a scope rule per claim.",
     "R4, R8"),
    ("Extend " + cite("extend") + "\n(extend.com)",
     "Product protection and warranty platform for retailers, with claim automation, self-service claim resolution and fraud management.",
     "Aimed at brands that sell protection plans. Nothing we read describes multi-agent review, per-fact provenance, or a deterministic adjudicator.",
     "R1, R2, R6"),
    ("Agent frameworks:\nGoogle ADK " + cite("adk") + ", CrewAI " + cite("crewai") + ", AutoGen " + cite("autogen"),
     "Libraries for building agents and multi-agent systems. ADK describes itself as a way to build multi-agent systems; CrewAI as a way to build collaborative agents, crews and flows; AutoGen as agents that converse with each other.",
     "They supply building blocks. They do not decide who may approve money, how two agents in two different Python environments share verified context, or how to defend against hidden instructions in documents.",
     "R3, R5, R6, R7"),
    ("Protocols:\nMCP " + cite("mcp") + ", A2A " + cite("a2a"),
     "MCP is an open standard for connecting models to tools and data. A2A is an open standard, now hosted by the Linux Foundation, for agents built on different frameworks to discover and talk to each other.",
     "Protocols only. A design still has to decide what each tool may return (scope), what a message must contain (contract) and how a result is verified.",
     "R4, R5, R8"),
    ("Blackboard for LLM agents " + cite("han", "salemi", "terrarium"),
     "Research prototypes that bring the classic blackboard architecture to language-model agents: general reasoning tasks, information discovery in data lakes, and a testbed for safety, privacy and security studies.",
     "Not applied to claims. None of the abstracts we read describes two framework processes sharing one signed store, read permissions per agent, or a deterministic decision on top.",
     "R3, R7, R9"),
]


# Slide-sized wording of the same seven rows, in the same order, with the same requirement IDs (LIT_ROWS[i][3]).
LIT_SHORT = [
    ("Tractable", "AI damage assessment from photos", "No cross-check of form, invoice and report; no line citations"),
    ("Shift Technology", "Closed AI agents for insurer claims and fraud", "No public account of output checks, shared context or payout control"),
    ("Verisk ClaimSearch", "Large claims database for fraud analysis", "A data source, not an investigation workflow"),
    ("Extend", "Warranty and protection-plan claim automation", "No multi-agent review, provenance or deterministic adjudicator described"),
    ("ADK, CrewAI, AutoGen", "Frameworks for multi-agent systems", "Building blocks only; no payout control or injection defence"),
    ("MCP, A2A", "Open protocols for tools and agent messaging", "Protocols only; scope, contract and verification left to the design"),
    ("Han 2025; Salemi 2025; Nakamura 2025", "Blackboard architecture for LLM agents", "Not applied to claims; no signed store or deterministic decision"),
]

# Slide-sized wording of R1..R11 (same IDs and meaning as REQ).
REQ_SHORT = {
    "R1": "Facts with page and line provenance; missing fields reported, never invented",
    "R2": "Several specialist agents on two frameworks (ADK, CrewAI)",
    "R3": "Blackboard with read permissions and tamper detection",
    "R4": "At least two read-only, claim-scoped MCP tools, every call logged",
    "R5": "A2A between environments with a validated message contract",
    "R6": "Decision and every rupee in deterministic code; agents only recommend",
    "R7": "Resist prompt injection and unsafe output (guardrails, callbacks)",
    "R8": "Fail closed: a failed agent, tool or record goes to a human",
    "R9": "Observable and auditable: one trace, provenance, audit trail",
    "R10": "Reproducible benchmark with time and cost; honest about gaps",
    "R11": "Runs on a cloud VM so nothing runs on a laptop in the demo",
}
SHORT_REFS = [
    "Han and Zhang (2025). Blackboard architecture for LLM multi-agent systems. arXiv:2507.01701.",
    "Salemi et al. (2025). LLM-based multi-agent blackboard system for data science. arXiv:2510.01285.",
    "Nakamura et al. (2025). Terrarium: the blackboard for multi-agent safety and security. arXiv:2510.14312.",
    "Wu et al. (2023). AutoGen: multi-agent conversation. arXiv:2308.08155.",
    "Nii (1994). Blackboard systems at the architecture level. Expert Systems with Applications 7(1).",
    "Viaene et al. (2002). Classification techniques for automobile insurance claim fraud detection. J. Risk and Insurance 69(3).",
    "Guo et al. (2024). Large language model based multi-agents: a survey. arXiv:2402.01680.",
    "Greshake et al. (2023). Indirect prompt injection in LLM-integrated applications. arXiv:2302.12173.",
    "Model Context Protocol, modelcontextprotocol.io; A2A Protocol, a2a-protocol.org.",
    "Google ADK docs, google.github.io/adk-docs; CrewAI docs, docs.crewai.com.",
    "Tractable, Shift Technology, Verisk ClaimSearch, Extend: vendor pages, accessed November 2026.",
]
assert len(LIT_SHORT) == 7 and set(REQ_SHORT) == {r for r, _ in REQ}
