#!/usr/bin/env python3
"""Builds docs/submission/ClaimSleuth_Final_Presentation.pptx (15 slides, numbered 0 to 14).

Re-runnable. Reads (if present): TEAM.json, results/summary.json, results/architecture_comparison.csv,
results/messaging_comparison.csv. Anything missing is rendered as "pending"; no number is invented here.

Run:  <tools-venv>/bin/python docs/submission/build_ppt.py
"""
import csv
import json
import math
import shutil
import sys
from datetime import date
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent))
from content_shared import LIT_ROWS, LIT_SHORT, REQ, REQ_SHORT, SHORT_REFS  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
RES = HERE / "results"
OUT = HERE / "ClaimSleuth_Final_Presentation.pptx"
SHOTS = ROOT / "docs" / "screenshots"

NAVY = RGBColor(0x0F, 0x24, 0x47)
BLUE = RGBColor(0x1D, 0x4E, 0xD8)
LBLUE = RGBColor(0xDB, 0xEA, 0xFE)
GREEN = RGBColor(0xDC, 0xFC, 0xE7)
DGREEN = RGBColor(0x15, 0x80, 0x3D)
AMBER = RGBColor(0xFE, 0xF3, 0xC7)
DAMBER = RGBColor(0xB4, 0x53, 0x09)
RED = RGBColor(0xFE, 0xE2, 0xE2)
GREY = RGBColor(0xF1, 0xF5, 0xF9)
DGREY = RGBColor(0x47, 0x55, 0x69)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
INK = RGBColor(0x0F, 0x17, 0x2A)
PURPLE = RGBColor(0xED, 0xE9, 0xFE)

SW, SH = 13.333, 7.5
BOTTOM = 7.05  # content must end above this (inches)
WARNINGS: list[str] = []


# ---------------------------------------------------------------- data
def load_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_csv(p: Path):
    try:
        with p.open(newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        return rows if len(rows) > 1 else None
    except Exception:
        return None


TEAM = load_json(HERE / "TEAM.json") or {"members": []}
SUMMARY = load_json(RES / "summary.json")
ARCH = load_csv(RES / "architecture_comparison.csv")
MSG = load_csv(RES / "messaging_comparison.csv")


def flatten(d, prefix=""):
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(flatten(v, f"{prefix}{k}."))
    elif isinstance(d, (int, float, str, bool)):
        out[prefix[:-1]] = d
    return out


FLAT = flatten(SUMMARY) if SUMMARY else {}


def sget(*needles, default="pending"):
    """First flattened summary value whose key contains all needles (case-insensitive)."""
    for k, v in FLAT.items():
        kl = k.lower()
        if all(n in kl for n in needles):
            return f"{v:.2f}" if isinstance(v, float) else str(v)
    return default


# ---------------------------------------------------------------- helpers
prs = Presentation()
prs.slide_width, prs.slide_height = Inches(SW), Inches(SH)
BLANK = prs.slide_layouts[6]
FONT = "Calibri"


def est_lines(text: str, pt: float, width_in: float) -> int:
    cpl = max(1, int(width_in * 72 / (pt * 0.52)))
    return sum(max(1, math.ceil(len(par) / cpl)) for par in text.split("\n"))


def est_height(text: str, pt: float, width_in: float, pad=0.12) -> float:
    return est_lines(text, pt, width_in - 0.2) * pt * 1.22 / 72 + pad


def _run(p, text, pt, bold=False, color=INK, italic=False):
    r = p.add_run()
    r.text = text
    r.font.size, r.font.bold, r.font.italic = Pt(pt), bold, italic
    r.font.name = FONT
    r.font.color.rgb = color
    return r


def text(slide, x, y, w, h, content, pt=16, bold=False, color=INK, align=PP_ALIGN.LEFT,
         anchor=MSO_ANCHOR.TOP, name=None):
    """content: str or list of str (paragraphs). A paragraph starting '- ' becomes a bullet."""
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = Inches(0.08)
    tf.margin_top = tf.margin_bottom = Inches(0.04)
    paras = content if isinstance(content, list) else [content]
    need = 0.0
    for i, para in enumerate(paras):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.space_after = Pt(4)
        bullet = para.startswith("- ")
        s = ("• " + para[2:]) if bullet else para
        _run(p, s, pt, bold, color)
        need += est_height(s, pt, w, 0.02) + 4 / 72
    if need > h + 0.05:
        WARNINGS.append(f"text overflow ~{need:.2f}in > {h:.2f}in: {str(paras[0])[:50]}")
    return tb


def box(slide, x, y, w, h, label, fill=LBLUE, line=BLUE, pt=14, bold=False, color=INK,
        shape=MSO_SHAPE.ROUNDED_RECTANGLE, dash=False, align=PP_ALIGN.CENTER):
    s = slide.shapes.add_shape(shape, Inches(x), Inches(y), Inches(w), Inches(h))
    s.fill.solid()
    s.fill.fore_color.rgb = fill
    s.line.color.rgb = line
    s.line.width = Pt(1.5)
    if dash:
        s.line.dash_style = 4  # dash
    s.shadow.inherit = False
    tf = s.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    tf.margin_left = tf.margin_right = Inches(0.05)
    tf.margin_top = tf.margin_bottom = Inches(0.03)
    lines = label.split("\n")
    need = 0.0
    for i, ln in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        _run(p, ln, pt, bold if i == 0 else False, color)
        need += est_lines(ln, pt, w - 0.1) * pt * 1.2 / 72
    if need > h:
        WARNINGS.append(f"box overflow ~{need:.2f}in > {h:.2f}in: {lines[0][:40]}")
    return s


def arrow(slide, x1, y1, x2, y2, color=DGREY, dashed=False, both=False, width=2.0):
    c = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2))
    c.line.color.rgb = color
    c.line.width = Pt(width)
    if dashed:
        c.line.dash_style = 4
    ln = c.line._get_or_add_ln()
    for tag, on in (("a:headEnd", both), ("a:tailEnd", True)):
        if on:
            e = etree.SubElement(ln, qn(tag))
            e.set("type", "triangle")
    return c


def label(slide, x, y, w, content, pt=14, color=DGREY, bold=False, align=PP_ALIGN.CENTER):
    return text(slide, x, y, w, 0.34 * (1 + content.count("\n")), content, pt, bold, color, align)


def table(slide, rows, widths, x=0.5, y=1.45, pt=14, header_fill=NAVY, first_col_bold=False,
          pending_fill=AMBER):
    """rows[0] is the header. Row heights estimated so overflow can be detected."""
    nr, nc = len(rows), len(rows[0])
    heights = []
    for ri, r in enumerate(rows):
        h = max(est_height(str(c), pt, widths[ci], 0.1) for ci, c in enumerate(r))
        heights.append(max(h, 0.36))
    total = sum(heights)
    if y + total > BOTTOM:
        WARNINGS.append(f"table overflow: ends at {y + total:.2f}in (limit {BOTTOM}) first cell {rows[0][0]}")
    gs = slide.shapes.add_table(nr, nc, Inches(x), Inches(y), Inches(sum(widths)), Inches(total))
    t = gs.table
    for ci, w in enumerate(widths):
        t.columns[ci].width = Inches(w)
    for ri, r in enumerate(rows):
        t.rows[ri].height = Inches(heights[ri])
        for ci, val in enumerate(r):
            cell = t.cell(ri, ci)
            cell.margin_left = cell.margin_right = Inches(0.08)
            cell.margin_top = cell.margin_bottom = Inches(0.03)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            tf = cell.text_frame
            tf.word_wrap = True
            p = tf.paragraphs[0]
            sval = str(val)
            if ri == 0:
                _run(p, sval, pt, True, WHITE)
                cell.fill.solid()
                cell.fill.fore_color.rgb = header_fill
            else:
                _run(p, sval, pt, first_col_bold and ci == 0, INK)
                cell.fill.solid()
                if sval.strip().lower() == "pending":
                    cell.fill.fore_color.rgb = pending_fill
                else:
                    cell.fill.fore_color.rgb = GREY if ri % 2 == 0 else WHITE
    return total


def new_slide(n: int, title: str, notes: str, subtitle: str | None = None):
    s = prs.slides.add_slide(BLANK)
    bar = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, prs.slide_width, Inches(1.0))
    bar.fill.solid()
    bar.fill.fore_color.rgb = NAVY
    bar.line.fill.background()
    text(s, 0.5, 0.04 if subtitle else 0.12, 11.8, 0.6, title, 28, True, WHITE, anchor=MSO_ANCHOR.MIDDLE)
    if subtitle:
        text(s, 0.5, 0.62, 11.8, 0.34, subtitle, 14, False, RGBColor(0xBF, 0xDB, 0xFE))
    text(s, 12.2, 6.98, 0.9, 0.4, str(n), 14, False, DGREY, PP_ALIGN.RIGHT)
    text(s, 0.5, 6.98, 9, 0.4, "ClaimSleuth | Agentic AI (CSE3101) | BML Munjal University", 12, False, DGREY)
    s.notes_slide.notes_text_frame.text = notes
    return s


def pending_note(s, y=6.5, what="results/ files"):
    if SUMMARY is None or (ARCH is None and MSG is None):
        text(s, 0.5, y, 12.3, 0.4, f"Cells marked 'pending' are filled from {what} when the experiment run lands "
             "(re-run build_ppt.py).", 14, False, DAMBER)


# ================================================================ slide 0
members = TEAM.get("members", [])
s = prs.slides.add_slide(BLANK)
bg = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, prs.slide_width, prs.slide_height)
bg.fill.solid()
bg.fill.fore_color.rgb = NAVY
bg.line.fill.background()
text(s, 0.8, 1.0, 11.7, 1.4, TEAM.get("title", "ClaimSleuth: Multi-Agent Warranty Claim Investigation"),
     40, True, WHITE)
text(s, 0.8, 2.5, 11.7, 0.9,
     "Hierarchical manager over ADK and CrewAI agents, with a blackboard, MCP tools, A2A and a deterministic adjudicator",
     20, False, RGBColor(0xBF, 0xDB, 0xFE))
mrows = [["Name", "Enrollment"]] + [[m.get("name", ""), m.get("enrollment", "")] for m in members]
table(s, mrows, [5.0, 4.0], x=0.8, y=3.7, pt=16)
text(s, 0.8, 6.3, 11.7, 0.7,
     f"{TEAM.get('course', 'Agentic AI (CSE3101)')} | Instructor: {TEAM.get('instructor', '')} | Final evaluation, November 2026",
     16, False, WHITE)
s.notes_slide.notes_text_frame.text = (
    "Introduce the team and the one-line pitch: three agents on two frameworks investigate a warranty claim; "
    "the decision and every rupee come from deterministic code, agents only recommend. "
    "Likely question: is it real AI? Answer: default mode is offline test doubles for the models, live mode uses Gemini 3.5 Flash.")

# ================================================================ slide 1
s = new_slide(1, "Objectives and problem statement",
              "Problem: manual warranty checking is slow, inconsistent and open to manipulation. Objective: automate the checking "
              "with agents but keep money decisions deterministic. Question to expect: why not one big LLM? Answer: narrow "
              "agents are simpler to prompt, fail independently, and can be cross-checked.")
text(s, 0.5, 1.2, 6.0, 0.4, "Problem", 20, True, BLUE)
text(s, 0.5, 1.65, 6.0, 4.9, [
    "- A claims officer reads a claim form, invoice and repair report, compares serials, dates and names, looks up the policy and claim history, "
    "watches for fraud, and calculates a payout.",
    "- Slow and error-prone by hand.",
    "- Uploaded documents are untrusted: they can hide instructions aimed at an AI (prompt injection).",
    "- A wrong automated payout is costly, so AI output cannot be trusted with money.",
], 16)
text(s, 6.9, 1.2, 6.0, 0.4, "Objectives", 20, True, BLUE)
text(s, 6.9, 1.65, 6.0, 4.9, [
    "- O1: Investigate a claim PDF with three specialised agents on two frameworks (Google ADK and CrewAI).",
    "- O2: Share context through a blackboard and give agents two MCP tools.",
    "- O3: The deterministic adjudicator alone decides (approve, reject, escalate, request evidence) and computes the payout in INR.",
    "- O4: Detect and never obey prompt injection; send risky cases to a human.",
    "- O5: Make every step observable and auditable with page and line citations.",
    "- O6: Evaluate the design against alternatives with measured runs and deploy to the cloud.",
], 16)

# ================================================================ slide 2
s = new_slide(2, "Literature review and gap",
              "Same table as report Section 3 in short form (content_shared.py). Every source was checked against the vendor's or "
              "publisher's own page in November 2026; for the commercial products a listed gap means we did not find the feature "
              "described, not that it is certainly absent. Gap: the works describe frameworks or protocols in isolation; few combine "
              "a blackboard, MCP, cross-framework A2A and deterministic adjudication with prompt-injection safeguards in one audited pipeline.")
lit = [["Source", "What it does", "Gap for our problem", "Req."]] + [
    [src, does, gap, LIT_ROWS[i][3]] for i, (src, does, gap) in enumerate(LIT_SHORT)]
h = table(s, lit, [2.6, 3.6, 4.9, 1.2], y=1.15, pt=14, first_col_bold=True)
box(s, 0.5, 1.2 + h + 0.15, 12.3, 0.9,
    "Gap: no single audited pipeline combines blackboard context, MCP tools, ADK-to-CrewAI A2A, injection-safe handling and a "
    "deterministic adjudicator, with measured comparisons.", AMBER, DAMBER, 15, True)

# ================================================================ slide 3
s = new_slide(3, "Agent requirements mapped to gaps and problem statement",
              "R1 to R11 are the same requirements as report Section 4 (content_shared.py). The last column lists the literature rows "
              "(slide 2) whose gap each requirement fills. Problem statement: manual claim review is slow, error-prone, exposed to "
              "untrusted documents, and a wrong payout is costly. Likely question: how is R7 verified? Via the citation checks, the "
              "150 prompt-injection tests and the evidence-integrity lab.")
_src_for = {r: [LIT_SHORT[i][0].split(";")[0] for i in range(7) if r in LIT_ROWS[i][3].split(", ")] for r, _ in REQ}
req = [["#", "Requirement", "Fills gap in"]] + [
    [r, REQ_SHORT[r], " / ".join(_src_for[r]) or "course brief"] for r, _ in REQ]
table(s, req, [0.8, 8.0, 3.5], y=1.1, pt=14)

# ================================================================ slide 4
s = new_slide(4, "MAS architecture: hierarchical manager",
              "Walk left to right. The manager plans, runs Evidence first (sequential), then releases Investigator and dispatches Forensic at "
              "the same time (parallel), monitors, and passes only verified reports to the adjudicator. Name the architecture: hierarchical "
              "manager with sequential and parallel stages. Evidence and Investigator are ADK agents in one process; Forensic is CrewAI in a "
              "second Python environment because the two dependency sets cannot merge.",
              "Hierarchical manager with sequential and parallel stages")
box(s, 0.4, 1.3, 1.9, 1.0, "Claim PDF\nupload", GREY, DGREY)
box(s, 2.7, 1.3, 2.5, 1.0, "Investigation Manager\nplan, dispatch, monitor", NAVY, NAVY, 14, True, WHITE)
box(s, 5.6, 1.3, 2.6, 1.0, "Deterministic baseline\nscreen + rules", GREY, DGREY)
arrow(s, 2.3, 1.8, 2.7, 1.8)
arrow(s, 5.2, 1.8, 5.6, 1.8)
# stage 1
box(s, 2.7, 2.85, 2.5, 1.0, "Stage 1 (sequential)\nEvidence Agent (ADK)", LBLUE, BLUE, 14, True)
arrow(s, 3.95, 2.3, 3.95, 2.85)
# blackboard
box(s, 2.7, 4.35, 2.5, 0.8, "Blackboard (SQLite)\nverified, receipted", PURPLE, RGBColor(0x6D, 0x28, 0xD9), 14, True)
arrow(s, 3.95, 3.85, 3.95, 4.35, both=True)
# stage 2 parallel
box(s, 5.9, 2.65, 2.6, 0.95, "Stage 2 (parallel)\nInvestigator (ADK)", LBLUE, BLUE, 14, True)
box(s, 5.9, 3.95, 2.6, 0.95, "Stage 2 (parallel)\nForensic (CrewAI, A2A)", GREEN, DGREEN, 14, True)
arrow(s, 5.2, 4.75, 5.9, 4.4, both=True, color=RGBColor(0x6D, 0x28, 0xD9))
arrow(s, 5.2, 4.55, 5.9, 3.2, both=True, color=RGBColor(0x6D, 0x28, 0xD9))
arrow(s, 3.95, 2.3, 5.9, 3.1, dashed=True)
arrow(s, 3.95, 2.3, 5.9, 4.1, dashed=True)
# MCP
box(s, 9.2, 3.15, 2.0, 1.1, "MCP server\n2 tools", AMBER, DAMBER, 14, True)
arrow(s, 8.5, 3.15, 9.2, 3.5, both=True)
arrow(s, 8.5, 4.4, 9.2, 3.95, both=True)
box(s, 11.4, 2.85, 1.6, 0.8, "Policies", GREY, DGREY)
box(s, 11.4, 3.85, 1.6, 0.8, "Claim history", GREY, DGREY)
arrow(s, 11.2, 3.5, 11.4, 3.25)
arrow(s, 11.2, 3.9, 11.4, 4.25)
# adjudicator
box(s, 5.9, 5.45, 2.6, 0.9, "Adjudicator\nonly decision-maker", RED, RGBColor(0xB9, 0x1C, 0x1C), 14, True)
arrow(s, 7.2, 4.9, 7.2, 5.45)
box(s, 9.2, 5.45, 2.2, 0.9, "Human review\nand report", GREY, DGREY)
arrow(s, 8.5, 5.9, 9.2, 5.9)
box(s, 0.4, 5.45, 4.8, 0.9, "Callbacks and trace: every agent, model and tool call\nrecorded; guardrails on inputs and outputs", GREEN, DGREEN, 14)
arrow(s, 5.2, 5.9, 5.9, 5.9, dashed=True)
text(s, 8.7, 1.25, 4.3, 1.4, "Dashed = dispatch by manager. Purple = blackboard read/write. "
     "Agents recommend; the adjudicator decides.", 14, False, DGREY)

# ================================================================ slide 5
def _s(ms):
    return f"{ms / 1000:.1f}"


def _cfg(key, grp="e3_architecture"):
    return SUMMARY[grp][key] if SUMMARY else None


s = new_slide(5, "Architecture evaluation: which orchestration is best",
              "Three orchestrations were run on the same 40 cases, offline, on a macOS laptop (results/architecture_comparison.csv). "
              "Accuracy is 40 of 40 for all three BY CONSTRUCTION: the offline test doubles ignore other agents' reports, so accuracy cannot "
              "separate the designs. Say this openly. The choice rests on measured time, calls and context shared, plus design heuristics: "
              "independence of agent opinions, fault isolation, auditability and parallel speedup for the Investigator and Forensic stages. "
              "Sequential-only is as slow as the manager here because the manager also runs Evidence first; parallel-only is about twice as "
              "fast but nobody verifies the reports and the Investigator never sees the Evidence report. Laptop timings are not the "
              "deployment numbers: on the AWS VM in offline mode (2026-11-19) a claim took about 3 s.")
if SUMMARY:
    rows5 = [["Architecture (40 cases each)", "Correct", "Median (s)", "Mean / stdev (s)", "Model calls", "Context shared (chars)", "Evidence report seen by"]]
    names5 = {"seq_direct": ("Sequential only", "Investigator"),
              "par_direct": ("Parallel only, no manager", "nobody"),
              "hier_blackboard": ("Hierarchical manager + blackboard", "Investigator and Forensic")}
    for k, (nm, seen) in names5.items():
        c = _cfg(k)
        w = c["wall_ms"]
        rows5.append([nm, f"{c['correct']}/{c['runs']}", _s(w["median"]), f"{_s(w['mean'])} / {_s(w['stdev'])}",
                      f"{c['mean_llm_calls']:.0f}", f"{c['mean_context_chars_shared']:.0f}", seen])
    table(s, rows5, [3.3, 1.0, 1.1, 1.6, 1.1, 1.8, 2.4], y=1.2, pt=13, first_col_bold=True)
else:
    text(s, 0.5, 1.2, 12.3, 0.5, "results/summary.json missing: numbers pending.", 15, False, DAMBER)
text(s, 0.5, 3.55, 12.3, 3.4, [
    "- Accuracy is identical by construction (offline doubles ignore other agents' reports and prompt wording). It does not choose the design.",
    "- Measured: parallel-only halves wall time (12.2 s vs about 24.3 s) but shares no context and has no verification gate. Calls are equal (9 model calls, 6 MCP calls, 2 A2A messages).",
    "- Why the manager: Investigator and Forensic run in parallel after Evidence; the manager verifies each report, isolates a failing agent and keeps an audit trail.",
    "- Offline laptop run (macOS), 3 x 40 runs. Not deployment numbers: the AWS VM in offline mode took about 3 s per claim.",
], 14)


# ================================================================ slide 6
s = new_slide(6, "Agent-tool communication via MCP",
              "Deployment: the MCP server runs inside the single container on 127.0.0.1 on the AWS EC2 VM, behind Caddy; only 80/443 are public. Two tools on a separate MCP server using Streamable HTTP: warranty_policy_search and claim_history_lookup. ADK connects with "
              "McpToolset; CrewAI connects with the MCP client in its own environment. The run id travels in a header set by framework code, "
              "not by the model, so a model cannot ask for another claim's history. Every call is logged and checked against what the agent "
              "reported.")
box(s, 0.5, 1.5, 2.9, 1.1, "Evidence Agent\n(ADK)", LBLUE, BLUE, 14, True)
box(s, 0.5, 2.9, 2.9, 1.1, "Investigator Agent\n(ADK)", LBLUE, BLUE, 14, True)
box(s, 0.5, 4.3, 2.9, 1.1, "Forensic Agent\n(CrewAI, via A2A)", GREEN, DGREEN, 14, True)
box(s, 5.0, 2.2, 3.3, 2.5, "MCP server\nStreamable HTTP\nvalidates inputs and outputs\nscopes history to the claim\nlogs every call (JSONL)",
    AMBER, DAMBER, 14, True)
box(s, 9.9, 1.9, 2.9, 1.0, "warranty_policy_search\n(policies.json)", GREY, DGREY)
box(s, 9.9, 3.9, 2.9, 1.0, "claim_history_lookup\n(SQLite)", GREY, DGREY)
for y in (2.05, 3.45):
    arrow(s, 3.4, y, 5.0, 2.8 if y < 3 else 3.4, both=True)
arrow(s, 3.4, 4.85, 5.0, 4.2, both=True)
arrow(s, 8.3, 2.9, 9.9, 2.4, both=True)
arrow(s, 8.3, 4.0, 9.9, 4.4, both=True)
label(s, 3.3, 1.2, 1.9, "McpToolset", 14, DGREY)
label(s, 3.3, 5.05, 2.0, "MCP client", 14, DGREY)
text(s, 0.5, 5.7, 12.3, 1.2, [
    "- Run id and claim id come from framework headers, not model output.",
    "- An agent that did not complete its required MCP call does not count as completed; MCP log is cross-checked against agent records.",
], 15)

# ================================================================ slide 7
s = new_slide(7, "Messaging pattern: blackboard versus peer-to-peer",
              "Three messaging styles were measured on the same 40 cases offline (results/messaging_comparison.csv): blackboard (the product), "
              "direct message passing (ADK session state, Evidence to Investigator only) and no sharing. Accuracy is 40 of 40 in all three by "
              "construction; the doubles ignore what other agents say. Differences are time, context shared and what is recorded: only the "
              "blackboard gives signed read receipts (2 per run) and lets the Forensic Agent see the verified Evidence report. A true "
              "pub/sub broadcast arm was not built; say so. A2A: the Forensic Agent (CrewAI) is the only agent reached over A2A because "
              "it runs in a different framework and Python environment.")
if SUMMARY:
    rows7 = [["Style (40 cases each)", "Correct", "Median (s)", "Blackboard reads", "Context shared (chars)", "Forensic saw Evidence"]]
    names7 = {"blackboard": "Blackboard (product default)", "direct message passing": "Direct message passing",
              "no sharing": "No sharing"}
    for k, nm in names7.items():
        c = _cfg(k, "e4_messaging")
        rows7.append([nm, f"{c['correct']}/{c['runs']}", _s(c["wall_ms"]["median"]), f"{c['mean_blackboard_reads']:.0f}",
                      f"{c['mean_context_chars_shared']:.0f}", f"{c['forensic_saw_evidence_runs']}/{c['runs']}"])
    table(s, rows7, [3.4, 1.2, 1.5, 1.8, 2.3, 2.1], y=1.2, pt=13, first_col_bold=True)
else:
    text(s, 0.5, 1.2, 12.3, 0.5, "results/summary.json missing: numbers pending.", 15, False, DAMBER)
text(s, 0.5, 3.3, 6.0, 0.4, "What the numbers show", 18, True, BLUE)
text(s, 0.5, 3.75, 6.0, 3.2, [
    "- Accuracy is the same (40/40) by construction; it is not the reason for the choice.",
    "- Blackboard and direct passing take about the same time (median 24.3 s vs 24.1 s); blackboard shares twice the context (4,740 vs 2,370 chars).",
    "- No sharing is fastest (12.2 s) but the agents cannot use each other's work.",
    "- A pub/sub broadcast arm was not built.",
], 14)
text(s, 6.9, 3.3, 6.0, 0.4, "Why blackboard (design reasons)", 18, True, BLUE)
text(s, 6.9, 3.75, 6.0, 3.2, [
    "- One signed, append-only record: auditability and tamper evidence.",
    "- Read rules keep Forensic and Investigator opinions independent.",
    "- 1 of 3 agents (Forensic, CrewAI) is reached over A2A; the request carries a reference, not the report.",
    "- Writers must share one machine (SQLite WAL), which shaped the deployment.",
], 14)


# ================================================================ slide 8
s = new_slide(8, "Securing the multi-agent system",
              "Layered defence; nothing relies on detection alone. The screen is a pattern list, so a paraphrase can slip through; the "
              "later layers (untrusted wrapping, output checks, citation verification, deterministic adjudicator, human review) still "
              "bound the damage. Evidence: 150 prompt-injection tests and 56 evidence-integrity tests in the 801-test run of 6 Nov 2026. "
              "Honest limit: tests use a manipulated test double, not real model resistance.")
sec = [
    ["Layer", "Mechanism"],
    ["Input guardrail", "Injection screen on normalised text (hidden characters, look-alikes, spacing); a hit escalates the claim"],
    ["Safeguard", "Claim text wrapped in untrusted blocks; text cannot close its block; adjudicator alone decides"],
    ["Callbacks", "ADK before/after agent, model, tool and tool-error callbacks; CrewAI task guardrail; output refused if it carries credentials or instructions"],
    ["Verification", "Quotes must be on the cited line; MCP references must match the server log; Pydantic extra=forbid"],
    ["Integrity", "Blackboard entries hashed and HMAC-signed, append-only; tampering sends the run to a human"],
    ["Containment", "CrewAI network guard blocks non-local connections; services bind to 127.0.0.1"],
]
table(s, sec, [2.4, 9.9], y=1.2, pt=14, first_col_bold=True)

# ================================================================ slide 9
s = new_slide(9, "Experimental design",
              "Dataset: 40 synthetic cases, 8 per category, ground truth stored apart from the agents' data. E3 architecture and E4 messaging each "
              "use 3 x 40 runs and share runs; E5 uses the 40 cases plus 18 sample claims under two prompt structures (standard and "
              "evidence-first ReAct), 116 runs. Persona counts are unbalanced: honest 38, vague 6, abusive 6, injection 8 per structure. "
              "Decision variance is zero by construction because the offline doubles ignore prompt wording: 0 of 58 paired comparisons changed. "
              "Boxplot: system latency per claim, measured on a macOS laptop, offline. Cost: offline INR 0, 0 Gemini requests. Live cost was "
              "not measured: the 4 live runs used 9, 6, 7 and 7 requests, so 40 live runs would need about 240 to 360 requests against a free "
              "tier of 20 per day per model. No token counts or prices exist for the live runs.")
d9 = [
    ["Design element", "Value"],
    ["Cases and categories", "40 synthetic, 8 per category (valid, missing evidence, policy violation, suspicious, conflict/injection)"],
    ["Runs", "236 offline runs, 0 failed: E3 architecture 3 x 40, E4 messaging 3 x 40 (shared), E5 persona x prompt structure 116"],
    ["E5 personas (per structure)", "honest 38, vague 6, abusive 6, injection 8 (unbalanced); 0 of 58 paired decisions changed"],
    ["Cost", "Offline INR 0. Live not measured: 4 live runs used 9, 6, 7, 7 requests; 40 live runs need about 240 to 360"],
]
h9 = table(s, d9, [2.9, 9.4], y=1.15, pt=12, first_col_bold=True)
pic9 = RES / "boxplot_latency.png"
y9 = 1.15 + h9 + 0.3
if pic9.exists():
    pw = (BOTTOM - 0.35 - y9) * 2240 / 832
    s.shapes.add_picture(str(pic9), Inches(0.5), Inches(y9), width=Inches(min(pw, 8.6)))
    text(s, 9.25, y9, 3.6, 3.2, [
        "System latency per claim, offline doubles, macOS laptop. Not the deployment numbers (AWS VM, offline mode: about 3 s per claim).",
        "Parallel-only is about 12 s; all other wirings about 24 s.",
    ], 12, False, DGREY)
text(s, 0.5, BOTTOM - 0.35, 12.3, 0.3, "Variance: decisions identical by construction; only latency varies.", 12, False, DAMBER)

# ================================================================ slide 10
s = new_slide(10, "Validation: does it work as expected",
              "Evaluation method, stated truthfully: reference-based. Each case has a ground-truth label (expected final decision) written "
              "per case, and results are scored against it. There is NO LLM-as-judge. Offline 40-case run EV-20261119-141842-e46c4a on a "
              "macOS laptop: combined system 40 of 40; single agents on the decision alone: Evidence 65.0%, Investigator 92.5%, Forensic 75.0%. "
              "Tests: macOS rerun 795 passed and 1 failed (tests/test_provider_reliability.py::test_the_managers_deadline_keeps_the_agents_record, "
              "an existing test, fails alone, not edited), 155 passed in the CrewAI suite. Windows on 6 Nov 2026: 801 and 155. Latencies "
              "were measured offline on a macOS laptop (12 s per ADK agent in this stack, cause not investigated) and are not the deployment "
              "numbers: on the AWS VM in OFFLINE mode (2026-11-19) agents took 445, 266 and 1,142 ms and a claim about 3 s (run RUN-dash-6ac716c9bd). On the macOS laptop (Docker or native) a "
              "claim takes about 24 s with about 12 s per ADK agent, cause not isolated; on the VM (offline) agents took 445, 266 and 1,142 ms "
              "(results/aws_run_RUN-dash-6ac716c9bd.json). The VM now runs live mode; its one partial live run took 57.8 s and 23.6 s for the two ADK agents (results/aws_live_run_RUN-dash-6d15294eca.json), not comparable. If asked: those 3 s figures are offline test doubles. Cases were written by the developers and run on test "
              "doubles, so this shows pipeline correctness, not model accuracy.")
if SUMMARY:
    L = SUMMARY["per_agent_and_system_latency_ms_all_runs"]
    bm = SUMMARY["baseline_eval_40"]
    lat = lambda k: f"median {_s(L[k]['median'])} s, p95 {_s(L[k]['p95'])} s (n={L[k]['n']})"
    d10 = [
        ["Check", "Result", "Source"],
        ["Expected final decisions, 40 cases offline", "40 of 40 (combined); 116 of 116 E5 runs matched", "EV-20261119-141842-e46c4a"],
        ["Tests, macOS 2026-11-19", "795 passed, 1 failed (existing, not edited); CrewAI 155 passed", "results/test_run.txt"],
        ["Tests, Windows 2026-11-06", "801 passed; CrewAI 155 passed", "docs/EVIDENCE.md"],
        ["Evidence / Investigator latency (laptop)", f"{_s(L['evidence_ms']['median'])} s / {_s(L['investigator_ms']['median'])} s median", "236 runs, summary.json"],
        ["Forensic / system latency (laptop)", f"{L['forensic_ms']['median']:.0f} ms / {_s(L['wall_ms']['median'])} s median", "236 runs, summary.json"],
        ["AWS VM, OFFLINE mode, 2026-11-19", "agents 445, 266, 1,142 ms; about 3 s per claim (laptop: about 24 s)", "results/aws_run_RUN-dash-6ac716c9bd.json"],
    ]
else:
    d10 = [["Check", "Result", "Source"], ["results/summary.json missing", "pending", ""]]
h = table(s, d10, [4.3, 5.4, 2.6], y=1.15, pt=12)
box(s, 0.5, 1.15 + h + 0.3, 12.3, 1.3,
    "Evaluation method: reference-based, scored against a ground-truth expected decision per case. No LLM-as-judge is used.\n"
    "Offline runs use labelled rule-based test doubles; laptop latencies are not deployment numbers; live Gemini accuracy has not been measured.",
    AMBER, DAMBER, 14, True)

# ================================================================ slide 11
s = new_slide(11, "Team responsibility matrix",
              "Lead and Support per activity come from TEAM.json (matrix). Each activity has one lead and one supporting member; "
              "the repository history shows one committer, so the split is by area of work. It is a PROPOSED split, to be confirmed by the team (TEAM.json matrix_note). Be ready to name "
              "who did what if asked.")
ACT = list(TEAM.get("matrix", {}).keys())
short = [m.get("name", "").split()[0] for m in members]
mrows11 = [["Activity"] + short]
for act in ACT:
    ent = TEAM["matrix"][act]
    mrows11.append([act.replace("/ (", "(")] + [("Lead" if ent.get("L") == m["name"] else "Support" if ent.get("S") == m["name"] else "") for m in members])
table(s, mrows11, [5.3, 1.75, 1.75, 1.75, 1.75][:1 + len(members)], y=1.1, pt=11)
text(s, 0.5, 6.7, 12.3, 0.4, "Lead = main owner, Support = second member. PROPOSED split, to be confirmed by the team.", 12, False, DGREY)

# ================================================================ slide 12
s = new_slide(12, "Demo screenshots",
              "Screenshots in docs/screenshots/ are from 1 November 2026, before the dashboard was simplified; the findings are the same. "
              "The right-hand box is the deployment architecture: AWS EC2 t4g.small (ARM, 2 GB RAM + 2 GB swap, Ubuntu 24.04, ap-south-1), Docker Compose, "
              "Caddy with Let's Encrypt HTTPS on 13-204-3-14.sslip.io (Elastic IP 13.204.3.14) and basic auth, the single image (911 MB) with dashboard, ADK agents, MCP server and "
              "CrewAI A2A service, and one volume for the SQLite blackboard. "
              "VERIFIED ON 2026-11-19 over HTTPS in OFFLINE mode (docs/submission/DEPLOYED.txt): wrong password rejected (401); the four sample claims gave "
              "APPROVE, ESCALATE, ESCALATE, REQUEST_EVIDENCE (about 3 s each); before the Elastic IP was attached, a stop/start changed the public IP and the site came back by itself "
              "on the new sslip.io host name. Since then the VM has an Elastic IP (13.204.3.14, URL https://13-204-3-14.sslip.io/, stable across stop/start) and runs LIVE mode "
              "(Gemini gemini-3.5-flash, enabled 2026-11-19). One live claim was run there (RUN-dash-6d15294eca, WC-2026-0411): the two ADK agents completed on Gemini, "
              "the CrewAI Forensic Agent failed with HTTP 503 after its retry, so the run was INCOMPLETE, human approval required, APPROVE from the deterministic adjudicator "
              "over the two preserved reports: a partial run showing graceful degradation, not a complete live run. Free tier: the app caps Gemini at 20 requests per day and "
              "5 per minute and a claim costs 6 to 9, so only about 2 to 3 live claims per day; budget a dry run before the demo, and tell examiners. "
              "CI/CD: GitHub Actions ci.yml on every PR and push to main (compile, bash -n, secret grep, Docker build, no pytest); the VM pulls origin/main with a systemd timer "
              "(about a minute to detect, rebuild time not measured) and rolls back if the app is not healthy; it deploys main whatever the CI result; "
              "set AUTO_UPDATE=off in deploy/.env on the VM during the assessment. "
              "PLACEHOLDER FOR THE USER (H1): the three screenshots on the left are local, from 1 November. Paste 2 or 3 real screenshots of https://13-204-3-14.sslip.io/ "
              "(the login prompt, a finished claim report, the evaluation tab) over them before presenting; they cannot be generated here and none are faked. "
              "Image 911 MB; memory 339 MiB idle and 383 MiB peak were measured in Docker Desktop on the laptop, not on the VM. "
              "The three screenshots on the left are local, from 1 November; replace them with screenshots of the deployed URL before presenting.")
caps = [("1_claim_investigation_start.png", "Claim intake"),
        ("2_report_normal_claim.png", "Report: normal claim"),
        ("3_report_prompt_injection_0412.png", "Report: prompt injection (0412)")]
x = 0.5
for fn, cap in caps:
    p = SHOTS / fn
    if p.exists():
        from PIL import Image
        with Image.open(p) as im:
            iw, ih = im.size
        mw, mh = 2.9, 3.4
        sc = min(mw / iw, mh / ih)
        s.shapes.add_picture(str(p), Inches(x), Inches(1.25), Inches(iw * sc), Inches(ih * sc))
    text(s, x, 4.75, 3.0, 0.8, [cap, "(pre-simplification, 1 Nov)"], 14, False, DGREY, PP_ALIGN.CENTER)
    x += 3.1
# H1 PLACEHOLDER (user): replace the three local screenshots above with 2 or 3 screenshots of the live site
# (login prompt, finished claim report, evaluation tab). Not generated here; do not fake them.
box(s, 9.9, 1.25, 3.0, 0.5, "Deployed on AWS (2026-11-20)", NAVY, NAVY, 12, True, WHITE)
box(s, 9.9, 1.8, 3.0, 0.55, "EC2 t4g.small, Elastic IP\nap-south-1, live mode", GREY, DGREY, 11)
box(s, 9.9, 2.45, 3.0, 0.55, "Caddy: HTTPS (Let's Encrypt,\nsslip.io) + basic auth", LBLUE, BLUE, 11)
box(s, 9.9, 3.1, 3.0, 0.7, "Docker: single image 911 MB\ndashboard, ADK, MCP, CrewAI A2A", GREEN, DGREEN, 11)
box(s, 9.9, 3.9, 3.0, 0.6, "Volume: blackboard.sqlite3", PURPLE, RGBColor(0x6D, 0x28, 0xD9), 11)
box(s, 9.9, 4.6, 3.0, 0.8, "CI/CD: GitHub Actions CI;\nVM pulls origin/main\n(timer, health check, rollback)", AMBER, DAMBER, 11)
text(s, 0.5, 5.5, 12.3, 0.5, "Left: offline mode, test-double models, local screenshots from 1 Nov (not the deployed site).",
     14, False, DGREY)
text(s, 0.5, 5.95, 12.3, 0.9,
     ["Verified on AWS over HTTPS (2026-11-19, offline mode): 401 on wrong password; 4 of 4 sample claims (about 3 s each).",
      "Live mode on the VM: one partial run (2 of 3 agents on Gemini, CrewAI hit HTTP 503), escalated to a human. Free tier: about 2 to 3 live claims a day."], 14, True, DGREEN)

# ================================================================ slide 13
s = new_slide(13, "Conclusions",
              "Summarise what is proven and what is not. Proven: end-to-end pipeline on real frameworks and protocols, deterministic decisions, "
              "40 of 40 offline, 4 live runs, deployed on AWS (Elastic IP, live mode since 2026-11-19) and verified over HTTPS on 2026-11-19 in offline mode; one additional PARTIAL live run on the VM (2 of 3 agents completed, CrewAI hit HTTP 503), do not call it complete. Not proven: live accuracy, scale. Free tier allows about 2 to 3 live claims a day. Be upfront that comparisons and deployment numbers come "
              "from the results files and that offline agents are test doubles.")
text(s, 0.5, 1.2, 6.0, 0.4, "Achieved", 20, True, DGREY if False else DGREEN)
text(s, 0.5, 1.65, 6.0, 4.9, [
    "- Hierarchical manager with sequential and parallel stages over two frameworks.",
    "- Blackboard, two MCP tools, A2A between ADK and CrewAI.",
    "- Deterministic adjudicator: agents recommend, rules decide every rupee.",
    "- Layered security; 801 and 155 tests passed (6 Nov 2026); 40 of 40 offline cases correct.",
    "- 4 complete live Gemini runs (6 to 9 requests each).",
    "- Deployed on AWS (EC2, Docker, Caddy HTTPS, Elastic IP) and verified 2026-11-19 offline: 4 of 4 claims, 401 on wrong password.",
    "- GitHub Actions CI plus pull-based update of the VM with rollback.",
], 16)
text(s, 6.9, 1.2, 6.0, 0.4, "Limits and next steps", 20, True, DAMBER)
text(s, 6.9, 1.65, 6.0, 4.9, [
    "- Default agents are offline test doubles; no live accuracy figure.",
    "- Injection screen is a pattern list; cases are synthetic and developer-written.",
    "- Blackboard is SQLite: one machine only, which fixes the deployment shape.",
    "- One VM; one partial live run there (CrewAI 503), live capped at about 2 to 3 claims a day on the free tier.",
    "- CI does not run pytest; the VM deploys main whatever CI says.",
    "- Next: larger live evaluation, authenticated reviewers, broader attack coverage.",
], 16)

# ================================================================ slide 14
s = new_slide(14, "References",
              "Same sources as the report's reference list (content_shared.py), in short form; full entries with URLs are in the report. "
              "Project documents are in the repository docs folder.")
refs = SHORT_REFS + ["Versions used: google-adk 2.13.0, crewai 1.15.33. Repository docs: ARCHITECTURE_GUIDE.md, EVIDENCE.md."]
text(s, 0.5, 1.2, 12.3, 5.7, ["- " + r for r in refs], 14)

# ---------------------------------------------------------------- save
if OUT.exists():
    shutil.copy2(OUT, OUT.with_name(OUT.name + f".bak-{date.today():%Y%m%d}"))
prs.save(OUT)
print(f"saved {OUT} ({len(prs.slides)} slides)")
print("summary.json:", "present" if SUMMARY else "absent", "| arch csv:", bool(ARCH), "| messaging csv:", bool(MSG))
for w in WARNINGS:
    print("FIT WARNING:", w)
sys.exit(0)
