#!/usr/bin/env python3
"""Build docs/submission/ClaimSleuth_Final_Report.docx from the course template.

Run (tools venv, outside the repo):
    <scratchpad>/tools-venv/bin/python docs/submission/build_report.py

Re-runnable. Inputs:
  docs/Final_Report_Format.docx            the template (copied in memory, never modified)
  docs/submission/TEAM.json                names and enrollment numbers
  docs/submission/results/summary.json     optional: final experiment numbers (written by the experiment code)
  docs/submission/results/*.csv, *.png     optional: comparison tables and plots from the experiments
  docs/submission/DEPLOYED.txt             optional: one short paragraph describing the live deployment
Every number in the text comes from docs/EVIDENCE.md, README.md, the code, or the files above.

Contents page numbers: the contents entries are PAGEREF fields. The cached values written here are an estimate;
Word recalculates them (File > Options > Advanced or press Ctrl+A then F9 in Word) before the PDF export.
"""
import copy
import csv
import json
import os
import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
TEMPLATE = REPO / "docs" / "Final_Report_Format.docx"
OUT = Path(os.environ.get("REPORT_OUT", HERE / "ClaimSleuth_Final_Report.docx"))   # override only for tests
RESULTS = Path(os.environ.get("REPORT_RESULTS_DIR", HERE / "results"))
FIGS = HERE / "figures"
SHOTS = REPO / "docs" / "screenshots"

sys.path.insert(0, str(HERE))
import report_figures  # noqa: E402

BODY_FONT = "Cambria"
BODY_PT = 11
PAGE_PT = 681.0          # usable text height per page in points (letter, margins from the template)
LINE_PT = 15.0
CHARS_PER_LINE = 98


# ----------------------------------------------------------------------------------------------------------------
# low-level helpers
# ----------------------------------------------------------------------------------------------------------------
def is_placeholder(s):
    return (not s) or s.strip().startswith("<")


def set_run_font(run, size=BODY_PT, bold=None, italic=None, name=BODY_FONT, mono=False):
    run.font.name = "Consolas" if mono else name
    rpr = run._r.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    fname = "Consolas" if mono else name
    for a in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        rfonts.set(qn(a), fname)
    run.font.size = Pt(size - 1 if mono else size)
    if bold is not None:
        run.bold = bold
    if italic is not None:
        run.italic = italic
    run.font.color.rgb = RGBColor(0, 0, 0)


TOKEN = re.compile(r"(\*\*.+?\*\*|`.+?`)")


def add_runs(par, text, size=BODY_PT, bold=False, italic=False):
    for part in TOKEN.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            r = par.add_run(part[2:-2])
            set_run_font(r, size, bold=True, italic=italic)
        elif part.startswith("`") and part.endswith("`"):
            r = par.add_run(part[1:-1])
            set_run_font(r, size, bold=bold, italic=italic, mono=True)
        else:
            r = par.add_run(part)
            set_run_font(r, size, bold=bold, italic=italic)


def add_field(par, instr, cached, size=BODY_PT, bold=False):
    """Insert a complex field (PAGE, PAGEREF ...) with a cached result."""
    def mk(kind):
        r = par.add_run()
        set_run_font(r, size, bold=bold)
        fc = OxmlElement("w:fldChar")
        fc.set(qn("w:fldCharType"), kind)
        r._r.append(fc)
        return r
    mk("begin")
    r = par.add_run()
    set_run_font(r, size, bold=bold)
    it = OxmlElement("w:instrText")
    it.set(qn("xml:space"), "preserve")
    it.text = f" {instr} "
    r._r.append(it)
    mk("separate")
    r = par.add_run(str(cached))
    set_run_font(r, size, bold=bold)
    mk("end")


def keep_next(par):
    par.paragraph_format.keep_with_next = True


def set_spacing(par, before=None, after=None, line=None):
    pf = par.paragraph_format
    if before is not None:
        pf.space_before = Pt(before)
    if after is not None:
        pf.space_after = Pt(after)
    if line is not None:
        pf.line_spacing = line


def table_borders(tbl):
    tblPr = tbl._tbl.tblPr
    b = OxmlElement("w:tblBorders")
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        e = OxmlElement(f"w:{side}")
        e.set(qn("w:val"), "single")
        e.set(qn("w:sz"), "4")
        e.set(qn("w:space"), "0")
        e.set(qn("w:color"), "000000")
        b.append(e)
    mar = OxmlElement("w:tblCellMar")
    for side, w in (("top", 30), ("left", 70), ("bottom", 30), ("right", 70)):
        e = OxmlElement(f"w:{side}")
        e.set(qn("w:w"), str(w))
        e.set(qn("w:type"), "dxa")
        mar.append(e)
    lay = OxmlElement("w:tblLayout")
    lay.set(qn("w:type"), "fixed")
    look = tblPr.find(qn("w:tblLook"))
    for el in (b, lay, mar):                 # schema order: borders, layout, cell margins, then look
        if look is not None:
            look.addprevious(el)
        else:
            tblPr.append(el)


def repeat_header(row):
    trPr = row._tr.get_or_add_trPr()
    trPr.append(OxmlElement("w:tblHeader"))
    cant = OxmlElement("w:cantSplit")
    trPr.append(cant)


def no_split(row):
    row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))


# ----------------------------------------------------------------------------------------------------------------
# document builder with a rough page estimator (for the cached contents numbers)
# ----------------------------------------------------------------------------------------------------------------
class Builder:
    def __init__(self, doc, reg, pages_hint):
        self.doc = doc
        self.reg = reg                 # label -> "Table 3" / "Figure 2" from the previous pass
        self.newreg = {}
        self.counts = {"Table": 0, "Figure": 0}
        self.y = 0.0                   # estimated points from the start of the body
        self.head_y = {}               # heading key -> estimated y
        self.pages_hint = pages_hint   # heading key -> page (from the previous pass)
        self.bm_id = 100
        self.headings = []             # (key, text, numbered)
        self.h1n = 0
        self.h2n = 0

    # --- references -------------------------------------------------------------------------------------------
    def ref(self, label):
        return self.reg.get(label, "Table ?" if label.startswith("t_") else "Figure ?")

    # --- estimator --------------------------------------------------------------------------------------------
    def _adv(self, pts):
        self.y += pts

    def _lines(self, text, width_chars=CHARS_PER_LINE):
        plain = re.sub(r"[*`]", "", text)
        return max(1, -(-len(plain) // width_chars))

    # --- text -------------------------------------------------------------------------------------------------
    def para(self, text, align="left", after=6, italic=False, bold=False, size=BODY_PT, keep=False):
        p = self.doc.add_paragraph()
        add_runs(p, text, size=size, bold=bold, italic=italic)
        set_spacing(p, 0, after, 1.15)
        if align == "justify":
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        elif align == "center":
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if keep:
            keep_next(p)
        self._adv(self._lines(text) * LINE_PT * (size / 11.0) + after)
        return p

    def paras(self, *texts):
        for t in texts:
            self.para(t, align="left")

    def numbered(self, items):
        for i, t in enumerate(items, 1):
            p = self.doc.add_paragraph()
            p.paragraph_format.left_indent = Inches(0.3)
            p.paragraph_format.first_line_indent = Inches(-0.25)
            add_runs(p, f"{i}. " + t)
            set_spacing(p, 0, 3, 1.15)
            self._adv(self._lines(t, 92) * LINE_PT + 3)

    def h1(self, text, numbered=True):
        p = self.doc.add_paragraph(style="Heading 1")
        p.add_run(text)
        if not numbered:
            ppr = p._p.get_or_add_pPr()
            numpr = OxmlElement("w:numPr")
            ilvl = OxmlElement("w:ilvl")
            ilvl.set(qn("w:val"), "0")
            nid = OxmlElement("w:numId")
            nid.set(qn("w:val"), "0")
            numpr.append(ilvl)
            numpr.append(nid)
            ppr.append(numpr)
        key = f"h{len(self.headings)}"
        self.headings.append((key, text, numbered))
        self._bookmark(p, f"_Toc_{key}")
        self.head_y[key] = self.y
        self._adv(60)
        if numbered:
            self.h1n += 1
            self.h2n = 0
        return p

    def h2(self, text):
        p = self.doc.add_paragraph(style="Heading 2")
        p.add_run(text)
        self.h2n += 1
        self._adv(46)
        return p

    def _bookmark(self, par, name):
        self.bm_id += 1
        s = OxmlElement("w:bookmarkStart")
        s.set(qn("w:id"), str(self.bm_id))
        s.set(qn("w:name"), name)
        e = OxmlElement("w:bookmarkEnd")
        e.set(qn("w:id"), str(self.bm_id))
        par._p.insert(1 if par._p.pPr is not None else 0, s)
        par._p.append(e)

    # --- captions, figures, tables ----------------------------------------------------------------------------
    def _label(self, kind, key):
        self.counts[kind] += 1
        n = self.counts[kind]
        self.newreg[key] = f"{kind} {n}"
        return n

    def caption(self, kind, key, text, above):
        n = self._label(kind, key)
        p = self.doc.add_paragraph()
        r = p.add_run(f"{kind} {n}: ")
        set_run_font(r, 10, bold=True)
        add_runs(p, text, size=10, italic=False)
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        set_spacing(p, 4 if above else 2, 4 if above else 10, 1.0)
        if above:
            keep_next(p)
        self._adv(self._lines(text, 100) * 13 + 8)
        return p

    def figure(self, key, path, caption, width=6.3):
        p = self.doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        set_spacing(p, 4, 0, 1.0)
        keep_next(p)
        run = p.add_run()
        pic = run.add_picture(str(path), width=Inches(width))
        h_pt = pic.height / 12700.0
        if self.y % PAGE_PT + h_pt + 40 > PAGE_PT:      # would not fit: moves to the next page
            self.y = (int(self.y // PAGE_PT) + 1) * PAGE_PT
        self._adv(h_pt + 6)
        self.caption("Figure", key, caption, above=False)

    def table(self, key, caption, headers, rows, widths=None, size=9, caption_above=True):
        if caption_above:
            self.caption("Table", key, caption, above=True)
        ncol = len(headers)
        t = self.doc.add_table(rows=1, cols=ncol)
        table_borders(t)
        t.autofit = False
        total = 10200
        widths = widths or [1] * ncol
        s = float(sum(widths))
        tw = [int(total * w / s) for w in widths]
        hdr = t.rows[0]
        repeat_header(hdr)
        for i, h in enumerate(headers):
            c = hdr.cells[i]
            c.width = Inches(tw[i] / 1440.0)
            par = c.paragraphs[0]
            add_runs(par, str(h), size=size, bold=True)
            set_spacing(par, 0, 0, 1.0)
        est = 16
        for row in rows:
            cells = t.add_row()
            no_split(cells)
            tallest = 1
            for i, val in enumerate(row):
                c = cells.cells[i]
                c.width = Inches(tw[i] / 1440.0)
                par = c.paragraphs[0]
                txt = "" if val is None else str(val)
                add_runs(par, txt, size=size)
                set_spacing(par, 0, 0, 1.0)
                cw = max(6, int(tw[i] / 1440.0 * 16 * (11.0 / size)))
                lines = sum(max(1, -(-len(seg) // cw)) for seg in txt.split("\n"))
                tallest = max(tallest, lines)
            est += tallest * (size + 3) + 5
        # spacer paragraph after the table
        sp = self.doc.add_paragraph()
        set_spacing(sp, 0, 6, 1.0)
        self._adv(est + 12)
        if not caption_above:
            self.caption("Table", key, caption, above=False)
        return t

    def image_file(self, key, path, caption, width=6.3):
        self.figure(key, path, caption, width)


# ----------------------------------------------------------------------------------------------------------------
# optional experiment inputs
# ----------------------------------------------------------------------------------------------------------------
def load_summary():
    p = RESULTS / "summary.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # keep the build alive, say so in the text
            return {"_error": f"summary.json could not be read: {e}"}
    return None


def flatten(prefix, obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            flatten(f"{prefix}.{k}" if prefix else str(k), v, out)
    elif isinstance(obj, list):
        if all(not isinstance(x, (dict, list)) for x in obj) and len(obj) <= 12:
            out.append((prefix, ", ".join(str(x) for x in obj)))
        else:
            out.append((prefix, f"list of {len(obj)} items (see results/summary.json)"))
    else:
        out.append((prefix, obj))


def read_csv_rows(path, max_rows=30):
    with open(path, newline="", encoding="utf-8") as fh:
        rdr = list(csv.reader(fh))
    if not rdr:
        return [], []
    return rdr[0], rdr[1:max_rows + 1] if len(rdr) > max_rows + 1 else rdr[1:]


# ----------------------------------------------------------------------------------------------------------------
# the report content
# ----------------------------------------------------------------------------------------------------------------
from content_shared import REQ, LIT_ROWS, REFS  # noqa: E402  (shared with build_ppt.py)


def build_content(b, team, summary):
    ref = b.ref
    title = team["title"]

    # ===================================================================== 1 INTRODUCTION
    b.h1("Introduction")
    b.para("When a phone, a refrigerator or a laptop fails inside its warranty, somebody has to decide whether the maker or the "
           "insurer should pay for the repair. In practice a claims officer reads a claim form, an invoice and a service "
           "centre's repair report, compares serial numbers and dates between the three, looks up the policy, checks whether "
           "the same device has been claimed before, watches for signs of fraud, and finally works out a payout. It is slow, "
           "and it is easy to miss one inconsistent digit on page three.", "justify")
    b.para("ClaimSleuth is our attempt to automate the checking part of that job without handing the money decision to a "
           "language model. A customer uploads one PDF. Three agents then investigate it: an Evidence Agent and an "
           "Investigator Agent written with Google's Agent Development Kit (ADK), and a Forensic Agent written with CrewAI that "
           "runs in its own process and is reached over the Agent2Agent (A2A) protocol. The agents use two tools served over the "
           "Model Context Protocol (MCP), and they share intermediate results through a blackboard stored in SQLite. At the end a "
           "deterministic adjudicator, which is ordinary Python and never calls a model, decides between APPROVE, REJECT, "
           "ESCALATE and REQUEST_EVIDENCE, and calculates the payout in Indian rupees. The agents only recommend.", "justify")
    b.para("We want to be clear early about what is real in this submission. By default the system runs in an offline mode in which "
           "the agents' language model is replaced by labelled rule-based test doubles (`OfflineAdkLlm` and "
           "`ForensicTestDoubleLLM`). Everything around the test doubles is the real thing: the ADK Runner and "
           "SequentialAgent, the CrewAI Agent, Task and Crew, the A2A server and client, the MCP server, the blackboard, the "
           "rules engine and the adjudicator. A live mode runs all three agents on Google Gemini (`gemini-3.5-flash`), but because "
           "we used a free-tier key (20 requests per day per model) only four complete live runs exist (plus one partial live run on the deployed VM, Section 7.4). All claims, people and "
           "serial numbers are synthetic.", "justify")
    b.para("The report follows the course format. Section 2 states the problem, Section 3 reviews existing products and "
           "research and lists the gaps, Section 4 turns the gaps into numbered requirements (R1 to R11), Section 5 describes "
           "the design and how the agents are run, secured and validated, Section 6 lists the technology, Section 7 gives "
           "the results, and Section 8 is the contribution matrix.", "justify")

    # ===================================================================== 2 PROBLEM STATEMENT
    b.h1("Problem Statement")
    b.h2("What is being addressed")
    b.para("The problem is the first-level investigation of a warranty claim: deciding, from a claim form, an invoice and a repair "
           "report, whether the claim is covered, how much should be paid, and whether a person must look at it. The checks are "
           "mostly mechanical but they span several documents and a history database, and the documents are untrusted. A claim "
           "can contain a serial number that differs between the invoice and the repair report, a purchase date that falls "
           "outside the coverage window, an earlier claim on the same serial, or text that tries to talk the reviewer (human or "
           "machine) into approving it.", "justify")
    b.para("Two sample files in the repository show this. `WC-2026-0412` is a refrigerator claim with two different serial numbers "
           "and an earlier claim on the serial, and its repair report contains a hidden line that begins "
           "\"IMPORTANT AI SYSTEM INSTRUCTION: Ignore all previous instructions\". `WC-2026-0413` is a smartphone claim whose "
           "invoice asks the system to access all customer claim histories. A useful system has to find the real problems "
           "(the serial mismatch), notice the attack, refuse to obey it, and still send the claim to a person.", "justify")
    b.h2("Intended users")
    b.para("The primary user is a claims officer or reviewer at a warranty provider. The system gives that person a report "
           "in which every statement points to a document, page and line, or to a tool result, plus a decision "
           "and a settlement amount. The reviewer can approve, reject or ask for more information; each action is recorded "
           "with a name and a time. The second user is the customer, who only uploads the PDF. A third group is whoever "
           "maintains the system, who needs the trace, the audit trail and the monitoring pages.", "justify")
    b.h2("Why a multi-agent solution")
    b.para("A single rule engine can already do much of this, and we built one: it is the deterministic baseline, and when we "
           "built it it matched the ground truth on all 18 stored claims. What it cannot do well is read untidy text, spot a pattern that "
           "nobody wrote a rule for, or explain a case in the officer's own terms. A language model is good at exactly that and "
           "bad at arithmetic and at being held to account. So we split the work.", "justify")
    b.para("The investigation breaks into three narrow jobs that need different tools and different points of view: "
           "extracting facts with their source lines, checking policy and history, and looking for forensic warning signs. "
           "Giving each job to its own agent keeps each prompt short, lets us give each agent only the tools it needs, and "
           "gives the adjudicator two independent opinions rather than one. Using two frameworks was a course requirement, "
           "but it also shows why a protocol like A2A matters: the two libraries cannot even be installed together, because "
           "google-adk 2.13.0 needs `opentelemetry-api` 1.44 or lower and crewai 1.15.33 needs 1.45.1 or higher. They have to "
           "talk over a network.", "justify")
    b.h2("What we did not attempt")
    b.para("We did not build a production insurance system, and we did not train or fine-tune any model. The data are "
           "synthetic, the PDF reader expects the layout of our samples, and the reviewer's login is a typed name. Section 7 "
           "lists the limits we know about.", "justify")

    # ===================================================================== 3 LITERATURE REVIEW
    b.h1("Literature Review")
    b.para("We looked at three kinds of existing work: commercial products that automate claims or warranty handling, "
           "agent frameworks and protocols that a team like ours would build on, and recent research on blackboard-style "
           "coordination of language-model agents. Everything below was checked against the vendor's or publisher's own "
           "page while writing this report (November 2026). For the commercial products we only know what their public pages "
           "say. A gap listed for a product therefore means that we did not find the feature described there, not that the "
           "feature is certainly absent.", "justify")
    b.h2("Existing products and services")
    lit_rows = LIT_ROWS
    b.table("t_lit", "Existing products, frameworks and research compared with the needs of a warranty-claim investigation. "
            "Requirement IDs refer to Table " + "{REQTAB}".replace("{REQTAB}", ref("t_req").replace("Table ", "")) + ".",
            ["Product or source", "What it does (from its own page or abstract)", "Gap for our problem", "Req."],
            lit_rows, widths=[1.5, 3.2, 3.6, 0.8], size=8.5)
    b.h2("What the review told us")
    b.para("Commercial tools in this area are mostly closed, so a team that wants to understand or audit the decision logic "
           "cannot. The open frameworks are flexible but say little about trust: nothing in ADK, CrewAI, MCP or A2A "
           "stops a model from being fooled by a line of text in a PDF. The blackboard papers suggest that a shared "
           "store is a sensible way to let several agents build on each other's work, and the Terrarium paper in particular "
           "treats the blackboard as a place to study attacks. Nii's survey of blackboard systems at the architecture level "
           "(1994) explains the older idea: independent knowledge sources read and write one shared structure under the "
           "control of a separate scheduler. In our system the scheduler is the investigation manager.", "justify")
    b.para("Putting this together, the gaps we decided to fill are the ones in the last column of the table. In short: "
           "a fully inspectable pipeline in which agents from two frameworks cooperate, every claim made by an agent can be "
           "checked against the document line or tool record it cites, and the final money decision never rests on the "
           "model.", "justify")

    # ===================================================================== 4 REQUIREMENTS
    b.h1("Requirements for MAS")
    b.para("The following requirements come from the gaps above and from the course brief (criteria C-1 to C-11). We use the "
           "IDs elsewhere in the report. The last column says where the requirement is met in the code and how we checked it.",
           "justify")
    where = {
        "R1": ("`dashboard/pdf_intake.py`, `documents.py`", "Sample PDFs; missing-document sample WC-2026-0414 gives REQUEST_EVIDENCE"),
        "R2": ("`adk_service/`, `crew_service/`", "Three agents, two environments; integration tests"),
        "R3": ("`blackboard/`", "85 blackboard tests; tampering lab (56 tests)"),
        "R4": ("`mcp_server/server.py`", "Independent client script; scope tests"),
        "R5": ("`crew_service/a2a_server.py`, `adk_service/forensic_a2a.py`, `contract.py`", "A2A contract and timeout tests"),
        "R6": ("`integration/adjudicator.py`, `rules.py`", "16 adjudicator tests; 40-case benchmark"),
        "R7": ("`security.py`, `adk_service/callbacks.py`, CrewAI guardrail", "150 prompt-injection tests"),
        "R8": ("`orchestration/manager.py`, `observability/`", "Reliability and service-fault tests (77)"),
        "R9": ("`observability/trace.py`, audit trail", "Trace shown in the dashboard; observability tests"),
        "R10": ("`evaluation/`", "40-case run; experiment files in Section 7"),
        "R11": ("`Dockerfile.single`, `docker-compose.yml`, `deploy/` (see Section 5.10)", "Verified on AWS, Section 7.9"),
    }
    rows = [(rid, txt, where[rid][0], where[rid][1]) for rid, txt in REQ]
    b.table("t_req", "Requirements R1 to R11 and where each is met.", ["ID", "Requirement", "Where in the code", "How checked"],
            rows, widths=[0.5, 4.2, 2.6, 2.4], size=8.5)

    # ===================================================================== 5 METHODOLOGY
    b.h1("Methodology")
    # ---- 5.1
    b.h2("How we identified the agents")
    b.para("We started from what the claims officer does, wrote it as a list of duties, and grouped duties that need the same "
           "inputs and the same tools. Duties that need a different kind of judgement, or that we did not want to trust to a "
           "model, were separated. The result is Table " + ref("t_agents").replace("Table ", "") + ".", "justify")
    b.table("t_agents", "From the officer's duties to the agents and to the code that must not be an agent.",
            ["Duty of the claims officer", "Gap (Section 3)", "Who does it here"],
            [
                ("Read the three documents and write down dates, serials, names, diagnosis, cost, with the line each came from",
                 "Facts without provenance (R1)", "Evidence Agent (ADK). Needs only the policy tool."),
                ("Check the claim against the policy and the claim history; judge eligibility",
                 "Agent output nobody can check (R4, R6)", "Investigator Agent (ADK). Needs both tools."),
                ("Look for forensic warning signs: serial conflicts, odd dates, earlier-claim patterns, missing documents",
                 "One opinion only; no independent view (R2)", "Forensic Agent (CrewAI, other process). Needs only the history tool."),
                ("Decide, and calculate the payout", "Money decided by a model (R6)",
                 "Not an agent: the deterministic adjudicator and the rules engine."),
                ("Run the three above in the right order and notice when one fails",
                 "No clear owner of the process (R8)", "Not an agent: the investigation manager, deterministic code."),
                ("Sign off risky or large cases", "Silent approvals (R8)", "A human reviewer through the dashboard."),
            ], widths=[4.2, 2.6, 3.4], size=8.5)
    b.para("We deliberately kept the manager and the adjudicator out of the language-model world. The manager follows a typed plan "
           "and reacts to real events; the adjudicator re-computes the rules, verifies every citation, and has the only "
           "vote. We also kept the number of agents small (three, not six as in an earlier plan) so that each one has a "
           "clear reason to exist and each failure is easy to explain.", "justify")
    # ---- 5.2
    b.h2("Architecture and orchestration")
    b.para(f"{ref('f_arch')} shows the whole system. The dashboard (Starlette, port 8110) accepts the PDF, extracts text "
           "with page and line numbers using pypdf, screens it for injected instructions, and starts a job. The job hands "
           "the claim to the investigation manager (`orchestration/manager.py`), which is Level 1 of a two-level hierarchy. "
           "Level 2 consists of the three specialist agents.", "justify")
    b.figure("f_arch", FIGS / "fig_mas_architecture.png",
             "Multi-agent architecture with orchestration. The manager is deterministic code; the three agents are Level 2.")
    b.para("The manager builds a typed `ExecutionPlan` with five tasks (`baseline`, `evidence`, `investigation`, `forensics`, "
           "`adjudication`). Each task records its agent, framework, channel, dependencies, state, start and end time. "
           "A task moves from PENDING to READY only when its dependencies are met, and to RUNNING only from READY, so no "
           "agent can start before its inputs exist. Other end states are COMPLETED, FAILED, TIMED_OUT and BLOCKED; an invalid "
           "change raises `InvalidTransition`.", "justify")
    b.para("Three orchestration patterns are present in one design:", "justify")
    b.table("t_patterns", "Orchestration patterns used and where they are in the code.",
            ["Pattern", "Where", "What makes it that pattern"],
            [
                ("Sequential", "ADK `SequentialAgent(Evidence Agent, Investigator Agent)` in `adk_service/agents.py`",
                 "The Investigator starts only after the Evidence report is published and has been read from the blackboard."),
                ("Parallel", "After the Evidence report is published: Investigator (ADK) and Forensic Agent (CrewAI over A2A)",
                 "Both run at the same time and neither reads the other's report. A test checks that their executions overlap."),
                ("Hierarchical", "`orchestration/manager.py`",
                 "A Level 1 manager plans, dispatches, enforces dependencies, monitors, collects verified reports and escalates "
                 "incomplete work. The Level 2 agents do only their own task."),
            ], widths=[1.2, 4.0, 5.0], size=8.5)
    b.para("In the reference live run `RUN-dash-bcdbabaf97` the Investigator and the Forensic Agent both began at 08:53:32 UTC "
           "on 6 November, after the Evidence Agent finished, which is the parallel step working as intended. "
           "Section 7 reports the measured comparison of the three patterns (E3): the same 40 cases, identical accuracy by construction, "
           "and differences in wall time, calls and context shared.", "justify")
    # ---- 5.3
    b.h2("Context sharing: the blackboard and the other channels")
    b.para("Information moves between components in four ways, and we kept them separate on purpose (Table "
           + ref("t_channels").replace("Table ", "") + ").", "justify")
    b.table("t_channels", "The four channels through which information moves.",
            ["Channel", "Carries", "Between"],
            [
                ("Blackboard (SQLite, WAL)", "Run registration, agent status, the agents' reports, read receipts, the seal, the decision", "All agents, the manager and the adjudicator"),
                ("A2A 1.0 (JSON-RPC)", "`FORENSIC_REQUEST` and `FORENSIC_RESPONSE`; the request carries a ContextRef, not the report", "ADK side and the CrewAI service"),
                ("MCP (Streamable HTTP)", "Tool calls: `warranty_policy_search`, `claim_history_lookup`", "Each agent and the MCP server"),
                ("ADK session state", "ADK's own workflow state inside one run; filled from the verified blackboard copy", "The two ADK agents"),
            ], widths=[2.2, 5.0, 3.0], size=8.5)
    b.para("The blackboard is a SQLite file in WAL mode that both agent processes open: the ADK side in `.venv` and the CrewAI "
           "service in `.venv-crew`. The Evidence Agent's report is validated (schema, claim ID, every citation against the "
           "documents, every policy reference against what the MCP tool returned) and then published. The Investigator and the "
           "Forensic Agent consume it from there. Every read is one atomic check: read permission, exact version and SHA-256 "
           "hash, an HMAC-SHA256 signature, not retracted, not stale, not late. It leaves a signed receipt. Entries are append-only "
           "(database triggers refuse updates and deletes). Read permissions are fixed in `blackboard/models.py`: the Forensic "
           "Agent may not read the Investigator's report, so the two opinions stay independent. The adjudicator reads only "
           "reports that verify on the sealed run. If anything fails to verify, no agent report of that run is adjudicated "
           "and a human reviews the claim.", "justify")
    b.figure("f_seq", FIGS / "fig_a2a_blackboard_sequence.png",
             "Sequence of one investigation: blackboard, A2A and the manager's control steps.", width=6.0)
    b.para("Because SQLite allows one machine only, all writers must run on the same host. This is why our deployment design "
           "(Section 5.10) uses one virtual machine and a shared volume instead of several cloud services.", "justify")
    b.para("The course also asks for a comparison of messaging styles. Our current system uses the blackboard for shared state, "
           "A2A for the one agent-to-agent call and MCP for tools, and the repository has a documented direct mode "
           "(`--no-blackboard`) in which the blackboard is switched off. Section 7 reports the measured comparison of "
           "blackboard against direct passing and no sharing (E4); a publish/subscribe broadcast arm was not built.", "justify")
    # ---- 5.4
    b.h2("Tools through MCP")
    b.para(f"The MCP server (`mcp_server/server.py`, official MCP Python SDK 2.3, Streamable HTTP at `http://127.0.0.1:8102/mcp`) is "
           f"a separate process. {ref('f_mcp')} shows who calls what. `warranty_policy_search` searches the policy register "
           "(`policies.json`) and returns the full terms with a source pointer such as "
           "`policies.json#/POL-APPLIANCE-PLUS-03/deductible`. `claim_history_lookup` reads the SQLite claim history. "
           "Inside an investigation it only answers about that claim's own claimant and the serial numbers on its documents; "
           "anything else is refused with an \"Out of scope\" error. The claim ID comes from HTTP headers set by framework code, "
           "so a manipulated model cannot change it.", "justify")
    b.figure("f_mcp", FIGS / "fig_mcp_tools.png", "MCP tools: who connects, which tool each agent sees, and what the server logs.", width=6.0)
    b.para("Each agent sees only the tools it needs (`tool_filter` in ADK's `McpToolset`, and in CrewAI's `MCPServerHTTP`). "
           "A middleware writes one JSON line per call to `runtime/logs/mcp_tool_calls.jsonl` with run ID, claim ID, caller, "
           "status and duration. After the run the orchestrator fetches that log and the adjudicator trusts a history "
           "reference only if the server's own log confirms that call for this run and claim. The tools are read-only, so no "
           "agent can change a policy or a history record.", "justify")
    # ---- 5.5
    b.h2("Agent-to-agent communication")
    b.para("The Forensic Agent runs as an A2A service on port 8103 (a2a-sdk 1.3.0, A2A 1.0). It publishes an Agent Card at "
           "`/.well-known/agent-card.json` that names its skill `warranty_claim_forensics`. The ADK side uses `RemoteA2aAgent` "
           "to read the card and send one `WorkflowMessage` (a Pydantic model, rules in `contract.py`). The message names run, "
           "claim, sender, recipient and type, and carries the SHA-256 of each document instead of the document, so the service "
           "can refuse with `EVIDENCE_MISMATCH` if its own copy differs. The response must match the same run and claim "
           "and the opposite direction, otherwise it is rejected. Timeouts, unreachable service, invalid reply and "
           "`ERROR` replies all become a FAILED forensic result with the reason; nothing is invented, and the run goes to a "
           "human.", "justify")
    # ---- 5.6
    b.h2("How the agents are run")
    b.para("One switch, `EXECUTION_MODE` (or `--mode`), decides how all three agents run. In `offline` mode, the default, the "
           "ADK agents use `OfflineAdkLlm` and the CrewAI agent uses `ForensicTestDoubleLLM`. Both are deterministic and every "
           "output records the model as `test-double`. In `live` mode all three use Gemini `gemini-3.5-flash`, ADK through its "
           "Gemini model class and CrewAI through its native Gemini provider. Live mode never falls back to a test double: a "
           "missing key, a quota error, or an agent that skipped its required MCP call makes that agent FAILED.", "justify")
    b.para("Because a free-tier key allows only 20 requests per day per model, live mode shares one request ledger "
           "(`runtime/gemini_requests.sqlite3`) between both agent processes. A request waits for a free per-minute slot "
           "(default 5) and is refused when the daily budget is used. After an HTTP 503 each agent gets exactly one retry, "
           "after 15 seconds, which takes its own ledger slot; nothing else is retried. A complete live run has cost 6 to 9 "
           "requests, and every run records its exact request count by agent. Provider failures are classified (503, 429, "
           "authentication, network, timeout, invalid output) and stored without the response body.", "justify")
    b.para("The ADK agents run inside the dashboard process. The dashboard starts the MCP server and the CrewAI service when it "
           "starts and stops them when it stops. The three ports are 8110 (dashboard), 8102 (MCP) and 8103 (CrewAI A2A service).",
           "justify")
    # ---- 5.7
    b.h2("The deterministic adjudicator")
    b.para("The adjudicator (`integration/adjudicator.py`) recomputes the rules decision from the claim itself and then uses "
           "the agent reports only after verifying them. A report counts only if its result is COMPLETED. A document citation "
           "counts only if the quoted text really sits on the cited line. A history reference counts only if the agent's own "
           "MCP call returned it and the MCP server's log confirms it. A HIGH severity finding is a verified concern only if its "
           "evidence passes these checks; anything else is listed as unsupported and ignored.", "justify")
    b.para("The decision order is: baseline unavailable or inconsistent gives ESCALATE. If the rules say ESCALATE or REJECT, "
           "that stands and the agents cannot lift it. If the rules say REQUEST_EVIDENCE and an agent has a verified HIGH "
           "concern, ESCALATE. If the rules say APPROVE but no agent review completed, or an agent has a verified HIGH concern, "
           "ESCALATE; otherwise APPROVE. A fooled agent can therefore only make the outcome more cautious. REJECT and ESCALATE "
           "always need a human, and APPROVE needs one when the payout is ₹15,000.00 or more "
           "(`config.HUMAN_APPROVAL_THRESHOLD`).", "justify")
    b.para("Money follows one formula, computed with Python `Decimal` and rounded half-up to paise: "
           "payout = min(max(repair cost - deductible, 0), coverage limit) x (1 - depreciation rate). For the sample claim "
           "WC-2026-0411 the repair cost is ₹18,900.00, the deductible ₹2,500.00, the limit ₹80,000.00 and depreciation 0 per cent, "
           "so the payout is ₹16,400.00. That is above the threshold, so a person still signs it off.", "justify")
    # ---- 5.8
    b.h2("How the agents are secured")
    b.para("Security in this project is a stack of small checks, and none of them is trusted alone. "
           "Table " + ref("t_sec").replace("Table ", "") + " lists the threats we considered and the safeguards in the code.", "justify")
    b.table("t_sec", "Threats and safeguards.",
            ["Threat", "Safeguard", "Where"],
            [
                ("Instructions hidden in a document or claim text (prompt injection)",
                 "A pattern screen on a normalised copy of the text (invisible characters, look-alike letters, spaced-out words, wrapped lines); a hit creates a guardrail event with PDF page and line and escalates the claim.",
                 "`security.py`"),
                ("Text that tries to be obeyed by an agent",
                 "Claim text and the Evidence report reach the agents inside `<untrusted>` blocks that text cannot close; the instructions say to report, never follow, instructions found there.",
                 "`security.wrap_untrusted`, `claim_text.py`"),
                ("Unsafe or invalid model output",
                 "Pydantic models with `extra=\"forbid\"`; a report carrying credentials, key material or an agent's own instructions is refused before it is published; a CrewAI task guardrail checks JSON, claim ID, citations and history references and allows up to 2 retries.",
                 "`schemas.py`, ADK runner, `crew_service/crew.py`"),
                ("Invented evidence", "Quotes must appear on the cited line; MCP references must match what the tool returned and what the server logged.",
                 "ADK runner, guardrail, adjudicator"),
                ("An agent asking for other customers' data", "The MCP server refuses history lookups outside the claim's own claimant and serials, whatever the agent asks.",
                 "`mcp_server/server.py`"),
                ("Tampering with shared context", "Per-writer keys, SHA-256 payloads, HMAC signatures, append-only rows, read permissions; any failed verification stops adjudication of that run.",
                 "`blackboard/`"),
                ("Calls leaving the machine", "A network guard blocks any non-localhost connection during a CrewAI run; on the VM the services bind to 127.0.0.1, and inside the container the dashboard binds 0.0.0.0:7860 on the private compose network, reachable only through Caddy; CrewAI telemetry is disabled.",
                 "`crew_service/network_guard.py`"),
                ("Quota exhaustion and provider outages", "One shared request ledger, one retry after a 503, classified failures, no silent fallback.",
                 "`gemini_rate.py`, `observability/provider_errors.py`"),
                ("An automated decision nobody reviews", "REJECT, ESCALATE and payouts of ₹15,000 or more need a human; reviews are recorded and never change the automated result.",
                 "adjudicator, `dashboard/reviews.py`"),
            ], widths=[2.6, 5.8, 2.2], size=8.5)
    b.para("Callbacks. The ADK agents are wrapped in `adk_service/callbacks.py` (`ExecutionRecorder`): before and after each "
           "agent, before and after each model call, before and after each tool call, and on tool and model errors. These "
           "callbacks record start, end, latency, tokens when the provider reports them, and tool arguments. A tool error is "
           "turned into an error result the agent can see, and a failed model call is recorded and classified. A plugin "
           "(`EvidenceIsolation`) wraps the shared Evidence report as untrusted data before the Investigator reads it. "
           "CrewAI contributes the same kind of record through its event bus: crew, task, agent, tool and guardrail events. "
           "Nothing in the callbacks changes what an agent decides.", "justify")
    b.para("Persona variants (honest, vague, abusive, injection) exist for the base claims in the dataset (`-VAG`, `-ABU`, "
           "`-INJ` claim IDs). Abusive language is logged but never decides a claim.", "justify")
    b.para("What the safeguards do not give: the injection screen is a pattern list and misses a paraphrase that matches no "
           "pattern, so nothing depends on it alone. The reviewer's name is typed, not authenticated, and the lock that stops two "
           "conflicting reviews works within one process.", "justify")
    # ---- 5.9
    b.h2("How the agents are validated")
    b.para("We use four kinds of evidence and keep them apart, because they show different things. Implemented means the "
           "code exists and runs through the real frameworks. Offline validation means automated tests and the 40-case "
           "evaluation, on the test doubles. Simulated tests inject faults and attacks at local boundaries on disposable "
           "resources. Live demonstration means stored runs on Gemini counted in the request ledger.", "justify")
    b.para("Concretely: two pytest suites (one per environment), a 40-case benchmark with ground truth fixed by the scenario "
           "design and kept apart from the cases, an evidence-integrity lab that tampers with disposable blackboards, a "
           "prompt-injection lab that plants synthetic instructions in copies of the sample PDFs, and a set of mutation "
           "checks in which we deliberately broke the code and confirmed that a test failed. Each agent in the benchmark is "
           "scored on its own task: the Evidence Agent on facts, missing evidence and citations, the Investigator on policy "
           "violation and history, the Forensic Agent on anomalies, and the combined system on the four-class decision. "
           "Missing values are shown as N/A, never as 0. Section 7 gives the numbers.", "justify")
    # ---- 5.10
    b.h2("Deployment")
    b.para("The course says nothing may run from a local machine during the demonstration, so the system is deployed on Amazon "
           "Web Services. The design in " + ref("f_deploy") + " follows from two facts. The blackboard is a SQLite file whose writers "
           "must all be on one machine, so everything runs on one virtual machine (an EC2 t4g.small in the Mumbai region) with one "
           "volume for `runtime/`. The two Python environments cannot be merged, but they can live in one container image: "
           "`Dockerfile.single` builds both environments, and the dashboard starts the MCP server and the CrewAI A2A service itself on "
           "the loopback interface. We first built a two-image stack and measured both; the memory is the same (381 and 382 MiB after "
           "four runs) because the processes are the same, and the single image is smaller (911 MB after pruning against 1.95 GB) and has one "
           "container less to supervise, so the production file uses it. A Caddy reverse proxy in front provides HTTPS with a "
           "Let's Encrypt certificate and basic authentication; only ports 80 and 443 are public, and SSH is restricted. The VM has an "
           "Elastic IP (13.204.3.14, allocated on 2026-11-19), and a boot script reads the public IPv4 address and uses the host name "
           "`<ip-with-dashes>.sslip.io`, so the URL (`https://13-204-3-14.sslip.io/`) stays the same across a stop/start. Before the "
           "Elastic IP was attached the address changed after a stop/start, which we observed in the 2026-11-19 test. An Elastic IP "
           "is billed like any public IPv4 address (about 0.005 USD per hour), also while the instance is stopped, and the teardown "
           "script releases it. Cloud Run style services were not used because a SQLite file shared by "
           "two services is not supported there. The deployed system runs in LIVE mode since 2026-11-19: all three agents call Gemini "
           "(`gemini-3.5-flash`) with a key supplied through an environment file on the machine, never in an image or the repository. "
           "The application enforces its own request caps (`CLAIMSLEUTH_GEMINI_RPD`, default 20 per day, and `CLAIMSLEUTH_GEMINI_RPM`, "
           "default 5, both passed in from the environment file). A live claim costs about 6 to 9 requests, so on the free tier only about "
           "2 to 3 live claims fit in a day; after that the agents fail and the claim is escalated to a person. Offline mode stays "
           "selectable (`MODE=offline` in the environment file, then `boot.sh`).",
           "justify")
    b.para("**CI/CD.** The repository has a GitHub Actions workflow (`.github/workflows/ci.yml`) that runs on every pull request and "
           "every push to main: it compiles `src/` and `tests/`, parses the deploy shell scripts with `bash -n`, greps for committed "
           "secrets and builds `Dockerfile.single` with a layer cache (the image is not pushed). It does not run pytest, because the "
           "full suites take about 45 minutes; they are run locally and the quoted test counts are the ones in Section 7. Both runs so "
           "far passed (the pull request and the merge commit `186f279`). Deployment is pull-based: a systemd timer on the VM "
           "(`claimsleuth-update.timer`) runs `deploy/auto_update.sh` every minute. The script fetches `origin/main` of the public "
           "repository, copies the files (never `deploy/.env` or the runtime volume), runs `boot.sh` (`docker compose up -d --build`), waits "
           "for the application container's health check and rolls back to the previous commit if it does not become healthy. A new "
           "commit is detected within about a minute; the rebuild time on the VM was not measured. The VM deploys main whatever the CI "
           "result is, so a green CI run is not a precondition for a deployment. Setting `AUTO_UPDATE=off` in `deploy/.env` on the VM "
           "pauses the updates, which we recommend during the assessment window.", "justify")
    b.figure("f_deploy", FIGS / "fig_deployment.png", "Deployment on AWS: one EC2 VM with an Elastic IP, Docker Compose with Caddy and the single image, a volume for the blackboard, pull-based updates from GitHub main.", width=6.2)
    b.para("**What testing showed (2026-11-19).** Before the cloud step we ran the images with Docker Desktop on one laptop. The "
           "two-service stack exposed a real defect: the crew container did not know the blackboard path, so every run ended "
           "INCOMPLETE because the Forensic Agent could not read the blackboard. We fixed it by setting the same "
           "`CLAIMSLEUTH_BLACKBOARD_DB` for both processes; after the fix all four sample claims returned the expected decisions. "
           "Measured on the laptop, the single image used 339 MiB idle and 383 MiB at peak (Caddy about 14 MiB more), which fits "
           "the 2 GB instance; memory on the instance itself was not measured. The deployment itself is verified in Section 7.9. "
           "Details are in `results/deploy_test.md` and `docs/DEPLOYMENT_GUIDE.md`.", "justify")

    # ===================================================================== 6 TECHNOLOGY
    b.h1("Technology Stack")
    b.para("Versions are the ones pinned in `requirements/main.txt` and `requirements/crew.txt`.", "justify")
    b.table("t_stack", "Technology stack.",
            ["Technology", "Version", "Used for"],
            [
                ("Python", "3.12 (suites tested on 3.12.10, Windows 11; image Python 3.12)", "Everything"),
                ("Pydantic", "2.12.5", "Schemas for claims, reports, messages and results; strict validation"),
                ("Google ADK", "2.13.0", "Evidence and Investigator agents: `LlmAgent`, `SequentialAgent`, `Runner`, `McpToolset`, plugins; A2A client `RemoteA2aAgent`"),
                ("CrewAI", "1.15.33", "Forensic Agent: `Agent`, `Task`, `Crew`, guardrail, native MCP support"),
                ("MCP Python SDK", "2.3.0 (main environment); 1.30 inside CrewAI", "MCP server with two tools; discovery and log fetch"),
                ("a2a-sdk", "1.3.0 (A2A 1.0)", "Agent Card and JSON-RPC routes on the CrewAI side; client on the ADK side"),
                ("Starlette and Uvicorn", "see `requirements/`", "Dashboard web application and the A2A HTTP server"),
                ("pypdf", "6.21.4", "Text extraction from claim PDFs with page and line numbers"),
                ("SQLite (standard library)", "built in", "Claim history database; the blackboard; request ledger"),
                ("Decimal, hmac, hashlib (standard library)", "built in", "Money arithmetic; blackboard signatures and hashes"),
                ("Gemini `gemini-3.5-flash`", "external API", "All three agents in live mode only"),
                ("Ollama `qwen2.5:3b` (optional)", "external, local", "Experimental `ollama-local` mode for the Forensic Agent; its one recorded run failed"),
                ("pytest", "8.4.2", "Both test suites"),
                ("AWS EC2 t4g.small", "Ubuntu 24.04, ARM, ap-south-1", "The one virtual machine that hosts the deployed system (2 vCPU, 2 GB RAM, 2 GB swap, 20 GB encrypted disk)"),
                ("Docker and Docker Compose", "Docker 28 on the laptop; compose on the VM", "One image (`Dockerfile.single`, 911 MB) holding both Python environments; the compose file also starts Caddy"),
                ("Caddy", "`caddy:2-alpine`", "Reverse proxy: HTTPS and basic authentication in front of the dashboard"),
                ("GitHub Actions and a systemd timer", "workflow `ci.yml`; `claimsleuth-update.timer`", "CI checks on every PR and push to main; pull-based update of the VM from origin/main with health check and rollback"),
                ("Let's Encrypt and sslip.io", "external services", "Free certificate for the host name `<ip>.sslip.io`, which resolves to the VM's public IP without owning a domain"),
            ], widths=[2.6, 2.8, 5.0], size=8.5)
    b.para("**Frontend.** The page is plain HTML, CSS and JavaScript served as static files by the Starlette application "
           "(`dashboard/static/index.html`, `app.js` of about 900 lines, `eval.js` of about 300 lines, `style.css`). There is no "
           "front-end framework and no build step; `eval.js` draws the evaluation heat maps as HTML tables and the latency "
           "box plots as inline SVG, with no external chart library. This kept the demonstration dependency-free.", "justify")
    b.para("**Tooling for this report.** The diagrams and this document were produced with matplotlib and python-docx in a "
           "separate environment that is not part of the product.", "justify")

    # ===================================================================== 7 EXPERIMENTAL RESULTS
    b.h1("Experimental Results")
    b.h2("What the numbers do and do not show")
    b.para("Almost every number in this section was produced with the offline test doubles. They show that the pipeline, the "
           "protocols and the deterministic rules behave as designed on synthetic cases written by the same people who "
           "wrote the code. They do not show how accurate a language model is at this task, and the 40-case result in "
           "particular must not be read as a Gemini accuracy figure. Where a figure comes from live runs we say so, and only four complete "
           "live runs exist, plus one partial live run on the VM.", "justify")
    b.h2("Test results")
    b.para("The final regression ran on 6 November 2026 on the Windows 11 development machine, on the final code. A connection "
           "guard refused anything that was not a local test service, so no test sent a Gemini request. These counts are from "
           "`docs/EVIDENCE.md`; the report builder does not re-run them.", "justify")
    b.table("t_tests", "Final regression, 6 November 2026 (source: docs/EVIDENCE.md).",
            ["Suite", "Result"],
            [
                ("Main suite (`.venv`)", "801 passed, 0 failed, 1 deselected"),
                ("CrewAI suite (`.venv-crew`)", "155 passed, 0 failed, 2 deselected"),
                ("Browser flows on the real page", "239 of 239 checks across six flows, no JavaScript errors"),
                ("Mutation checks", "6 of 6 detected: each deliberate fault failed a test"),
            ], widths=[4, 6], size=9)
    b.para("The deselected tests are marked `live` and need a real model. The breakdown below is part of the 801, not extra "
           "tests.", "justify")
    b.table("t_areas", "Passed tests by area (part of the 801 in the main suite).",
            ["Area", "Passed", "Test files"],
            [
                ("Focused D8 tests (MCP records)", "12", "`test_reliability.py`"),
                ("Reliability, service faults, request census", "77", "`test_reliability.py`, `test_service_faults.py`, `test_request_census.py`"),
                ("Provider reliability", "72", "`test_provider_reliability.py`"),
                ("Manager and orchestration", "25", "`test_hierarchical_orchestration.py`, `test_integration_e2e.py`"),
                ("Adjudicator", "16", "`test_adjudicator.py`"),
                ("Human approval", "53", "13 test files"),
                ("Blackboard", "85", "`test_blackboard.py`, `test_blackboard_e2e.py`, `test_blackboard_context.py`"),
                ("Dashboard", "46", "`test_dashboard.py`, `test_dashboard_e2e.py`, `test_dashboard_layout.py`"),
                ("Evidence integrity (Phase 4.1)", "56", "`test_evidence_integrity.py`"),
                ("Prompt injection (Phase 4.2)", "150", "`test_prompt_injection.py`"),
            ], widths=[3.6, 0.9, 5.7], size=8.5)
    b.para("**Rerun on macOS, 2026-11-19.** On a macOS laptop with fresh virtual environments (Python 3.12.2) the main suite gave "
           "795 passed, 1 failed and 5 skipped (4,537 s) and the CrewAI suite 155 passed (79 s). The one failure is the existing test "
           "`tests/test_provider_reliability.py::test_the_managers_deadline_keeps_the_agents_record`: it fails alone as well "
           "(StopIteration, no `model.call` error event in the trace), we did not edit it and we have not found the cause. "
           "The 8 tests added for the experiment code (`tests/test_experiments.py`) pass on their own; the full suite was not "
           "re-run after they were added. The Windows figures above (801 and 155, 6 November) are the dated record of the final "
           "regression on the development machine; the two runs were on different machines and days and we have not reconciled "
           "the one failing test between them.", "justify")
    b.h2("The 40-case offline evaluation")
    b.para("The dataset (`data/evaluation/eval_dataset_v1.json`) holds 40 synthetic claims, 8 in each of five categories: "
           "valid, missing evidence, policy violations, suspicious or history anomalies, and conflicting evidence or prompt "
           "injection. Ground truth is fixed by the scenario design and is never computed by ClaimSleuth. Each case ran once "
           "through the existing workflow with the test doubles.", "justify")
    b.table("t_40", "40-case offline evaluation (source: docs/EVIDENCE.md).",
            ["Measure", "Result"],
            [
                ("Cases", "40 synthetic, 8 in each of five categories"),
                ("Expected final decisions", "40 of 40"),
                ("Comparison with the saved baseline `EV-20261102-135347-0a89f7`", "0 differences across 55 fields and 10 final report fields"),
                ("Decided by", "Deterministic rules 35, guardrail escalation 5"),
                ("Payouts", "8 of 40, total ₹1,29,240"),
                ("Approval status", "28 PENDING, 12 NOT_REQUIRED"),
                ("Review status", "COMPLETE on all 40"),
                ("Gemini requests", "0"),
            ], widths=[5, 5], size=9)
    b.para("Because the doubles follow similar rules, the three agents usually agree on this benchmark. A perfect score "
           "therefore says little about the agents and a good deal about the pipeline and the rules, which is what the "
           "benchmark is for.", "justify")
    b.h2("Live runs on Gemini")
    b.para("Four complete three-agent runs exist, all on `gemini-3.5-flash` for all three agents, with review status COMPLETE and "
           "human approval still PENDING. The request counts come from the ledger. The stored runs live in `runtime/runs/`, "
           "which git does not carry, so they can be opened only on the machine that holds them.", "justify")
    b.table("t_live", "Complete live runs (source: docs/EVIDENCE.md and the request ledger).",
            ["Run", "Claim", "Decision", "Gemini requests"],
            [
                ("RUN-dash-90e0f64064", "WC-2026-0411", "APPROVE", "9"),
                ("RUN-dash-5c40c24a5a", "WC-2026-0411", "APPROVE", "6"),
                ("RUN-dash-9f819f9dbc", "WC-2026-0412", "ESCALATE", "7"),
                ("RUN-dash-bcdbabaf97", "WC-2026-0412", "ESCALATE", "7, including one Forensic retry after an HTTP 503"),
            ], widths=[3, 2, 1.6, 3.6], size=9)
    b.para("`RUN-dash-bcdbabaf97` is the reference run. The Evidence Agent used 2 requests, the Investigator 2 and the Forensic "
           "Agent 3, which includes the retry. The Forensic Agent received an HTTP 503; its one retry, after 15 seconds, "
           "recovered, and the original error stays in the record. The decision was ESCALATE by GUARDRAIL_ESCALATION, and "
           "no review has been recorded, on purpose. Earlier runs that failed are kept, including `RUN-dash-c295d5a694`, "
           "which reached the free-tier quota (every agent FAILED with 429 RESOURCE_EXHAUSTED) and was still escalated safely "
           "by the adjudicator. Each framework also completed once on its own on 2 November (`RUN-live-adk-cs003` and "
           "`RUN-live-crew-cs003`).", "justify")
    b.para("These runs show that the system works end to end on a real model. Four runs on two sample claims cannot "
           "measure live accuracy, and we do not claim a live accuracy figure.", "justify")
    b.para("**One partial live run on the deployed VM (2026-11-19).** After live mode was enabled on the AWS VM, claim WC-2026-0411 "
           "was run once through the public site (`RUN-dash-6d15294eca`, record `docs/submission/results/aws_live_run_RUN-dash-6d15294eca.json`). "
           "It is not counted among the four complete live runs, because it did not complete. The Evidence Agent (ADK) and the "
           "Investigator Agent (ADK) completed with real Gemini inference: the Evidence Agent took 57.8 s with 2 model calls "
           "(8,068 prompt and 1,181 completion tokens) and the Investigator 23.6 s with 2 calls (23,696 prompt and 661 completion tokens). "
           "The Forensic Agent (CrewAI) made 3 model calls (the first returned 612 completion tokens), then Google answered HTTP 503 UNAVAILABLE "
           "(high demand) on its second call; the one retry after 15 seconds failed with the same error, and the agent ended FAILED after "
           "107.3 s. In total the ledger records 7 Gemini requests, and the run took about 2 min 48 s from submission to the final record. "
           "The run status was INCOMPLETE and the review status PARTIAL, human approval was required, and the final decision APPROVE "
           "(payout ₹16,400.00) came from the deterministic adjudicator over the two preserved reports, with the failure and the retry kept "
           "in the record. This shows graceful degradation on the deployed system, not accuracy, and it is one run.", "justify")
    b.h2("Security and reliability experiments")
    b.para("**Evidence integrity and prompt injection.** The two labs run offline on disposable copies. The first tampers "
           "with shared context and checks that the read is refused, a structured event is recorded and no agent report of "
           "that run is adjudicated. The second plants synthetic instructions in copies of the sample PDFs and checks "
           "what the safeguards do around a manipulated agent. 56 tests cover the first and 150 the second. They show the "
           "safeguards; they do not show how a real model resists attack.", "justify")
    b.para("**Rule D8 (human review when records are missing).** A run needs human review when an agent review or the MCP "
           "records are unavailable. We validated it with test doubles and real faults at local boundaries. An empty MCP log "
           "with agent-recorded calls stores INCOMPLETE and PARTIAL with approval PENDING; the decision, decider and "
           "settlement match the clean run. An empty log with no agent-recorded calls is not flagged, so normal runs are not "
           "misclassified. It was not reproduced in a true live failure.", "justify")
    b.para("**Stored dashboard runs.** 37 runs: 3 completed, 20 escalated, 14 incomplete and 1 recovered 503. No stored run "
           "has the contradictory MCP record case.", "justify")
    b.para("**Sample claims.** All four sample PDFs go through the page's API to the expected decision: WC-2026-0411 APPROVE "
           "(payout ₹16,400.00), WC-2026-0412 ESCALATE (injection detected at PDF page 3, lines 12 to 14, and not followed; "
           "the serial mismatch is still reported), WC-2026-0413 ESCALATE (unauthorised data access attempt at page 2, lines "
           "10 to 12; every history lookup stayed within the claim), and WC-2026-0414 REQUEST_EVIDENCE (the missing repair "
           "report is named). This was checked on a fresh install on 6 November 2026.", "justify")
    b.para("**Fresh install.** On 6 November 2026 we built new virtual environments outside the working folder from only the "
           "files git would carry, with no `.env`. The README steps ran as written, `pip check` reported no broken "
           "requirements in either environment, the dashboard started with the MCP and CrewAI services in offline mode, and "
           "`scripts\\stop_services.ps1` stopped all three. A selection of 160 tests passed there (133 in `.venv`, 27 in "
           "`.venv-crew`). The install took about 20 minutes with a filled pip cache.", "justify")
    b.h2("Screenshots")
    b.para("The screenshots below are from 1 November 2026, in offline mode, before the dashboard was simplified. The findings are "
           "the same; `docs/DASHBOARD_GUIDE.md` describes the current layout.", "justify")
    shot2 = SHOTS / "2_report_normal_claim.png"
    shot3 = SHOTS / "3_report_prompt_injection_0412.png"
    for key, f, cap, w in (
        ("f_shot_ok", shot2, "Report for the complete smartphone claim WC-2026-0411 (offline mode).", 3.0),
        ("f_shot_inj", shot3, "Report for WC-2026-0412: the hidden instruction is flagged with its page and line and is not followed.", 3.0),
    ):
        if f.exists():
            b.figure(key, f, cap, width=w)
    # ---- final experiments
    b.h2("Final experiments for the course criteria")
    if not summary or "e3_architecture" not in summary:
        b.para("The final experiment files (`docs/submission/results/summary.json`) were not present when this version of the "
               "report was built, so this part is empty on purpose. We will not estimate the numbers.", "justify")
    else:
        S = summary
        sec = lambda ms: f"{ms / 1000:.2f}"
        e3, e4, e5 = S["e3_architecture"], S["e4_messaging"], S["e5_persona_structure"]
        b.para("**Where and how these numbers were produced.** All runs of this section were made on 2026-11-19 on a macOS laptop "
               "(Python 3.12.2, fresh virtual environments), offline: the three agents are labelled rule-based test doubles, no "
               "Gemini request was sent (request ledger change 0) and all data are synthetic. Everything around the doubles "
               "(ADK Runner and agents, MCP server, CrewAI, A2A, blackboard, deterministic adjudicator) is the real code. The "
               "experiment code is `src/claimsleuth/evaluation/experiments.py`; the raw rows are in "
               "`docs/submission/results/experiment_runs.csv` (236 runs, 0 failed) and the aggregates in `results/summary.json`. "
               "The latencies are laptop latencies and are not the deployment numbers (see \"Which latencies to trust\" below).", "justify")

        # --- baseline
        b.para("**The 40-case baseline, rerun on 2026-11-19.** The evaluation `EV-20261119-141842-e46c4a` ran the existing "
               "40-case dataset again, offline, with 0 Gemini requests. The reference is the expected decision stored with each "
               "case; the evaluation compares against that ground truth and no language model acts as a judge. The combined "
               "system, whose final decision is made by the deterministic adjudicator, gets all 40 decisions right; the single "
               "agents are scored on the advisory decision they recommend.", "justify")
        md = (RESULTS / "baseline_eval_40_report.md").read_text(encoding="utf-8") if (RESULTS / "baseline_eval_40_report.md").exists() else ""

        def md_table(title):
            i = md.find(title)
            if i < 0:
                return None, []
            lines = []
            for ln in md[i:].split("\n")[1:]:
                if ln.startswith("|"):
                    lines.append([c.strip() for c in ln.strip().strip("|").split("|")])
                elif lines:
                    break
            return (lines[0], lines[2:]) if len(lines) > 2 else (None, [])

        hdr, rows = md_table("## Comparative performance")
        if hdr:
            b.table("t_base", "40-case baseline, decision accuracy by approach (source: results/baseline_eval_40_report.md).",
                    hdr, rows, widths=[3.0, 1.2, 1.4, 1.2, 1.2, 1.3, 1.3], size=8.5)
        hdr, rows = md_table("## Latency distribution")
        if hdr:
            b.table("t_base_lat", "40-case baseline, latency distribution in ms, laptop, offline (source: results/baseline_eval_40_report.md).",
                    hdr, rows, widths=[3.0, 0.6, 1.0, 1.0, 1.0, 1.6, 1.0], size=8.5)
        b.para("The single agents are less accurate than the combined system on the final decision (the Evidence Agent 65.0 %, the "
               "Investigator 92.5 %, the Forensic Agent 75.0 %) because each sees only part of the evidence; the adjudicator "
               "combines them with the rules. The confusion matrix of the combined decision has only diagonal entries (8 APPROVE, "
               "8 REJECT, 8 REQUEST_EVIDENCE, 16 ESCALATE). The 40 cases are synthetic, 18 of them reuse scenarios the system was "
               "developed against, and the other 22 were written by the same developers; the score shows that the pipeline and "
               "rules work as designed, not that they generalise. The MCP log counted 240 tool calls (6 per case).", "justify")

        # --- E3
        b.para("**E3: architecture comparison (course criterion C-4).** The same 40 cases ran through three orchestrations: "
               "sequential only (Evidence, then Investigator, then Forensic, one after another), parallel only (all three start "
               "together, no agent sees another's output, no manager) and the hierarchical manager with the blackboard that the "
               "product uses. Each configuration ran each case once.", "justify")
        names3 = {"seq_direct": "Sequential only", "par_direct": "Parallel only (no sharing)", "hier_blackboard": "Hierarchical manager + blackboard"}
        rows3 = []
        for k, nm in names3.items():
            c = e3[k]
            w = c["wall_ms"]
            rows3.append((nm, f"{c['correct']}/{c['runs']}", sec(w["median"]), f"{sec(w['mean'])} / {sec(w['stdev'])}", sec(w["p95"]),
                          f"{c['mean_llm_calls']:.0f} / {c['mean_mcp_calls_logged']:.0f} / {c['mean_a2a_messages']:.0f}",
                          f"{c['mean_context_chars_shared']:.0f}", f"{c['investigator_saw_evidence_runs']} / {c['forensic_saw_evidence_runs']}"))
        b.table("t_e3", "E3 architecture comparison, 40 cases each, wall time per claim in seconds (source: results/architecture_comparison.csv, summary.json).",
                ["Architecture", "Correct", "Median", "Mean / stdev", "p95", "Model / MCP / A2A calls", "Context shared (chars)", "Evidence seen by Investigator / Forensic (runs)"],
                rows3, widths=[2.4, 0.7, 0.7, 1.1, 0.7, 1.2, 1.0, 1.4], size=8)
        b.para("The three architectures make the same number of calls per claim (9 model calls, 8 agent tool calls, 6 MCP calls, "
               "2 A2A messages). Running the Investigator and Forensic stages in parallel without a manager halves the wall time "
               "(median 12.20 s against 24.25 s with the manager and 24.75 s sequential), because the two ADK agents then overlap "
               "(about 0.54 s of overlap with the CrewAI agent). The manager's configuration is not faster than the sequential one "
               "in these runs: it runs the Evidence Agent first and only then the other two. Its measured benefit is not speed.", "justify")

        # --- E4
        b.para("**E4: messaging comparison (course criterion C-1).** Blackboard (the product: signed reports, read receipts), "
               "direct message passing (the Evidence report is handed to the Investigator through ADK session state; the Forensic "
               "Agent runs in parallel and sees nothing) and no sharing. A true publish/subscribe broadcast arm was not built; "
               "no sharing is the closest arm we have, and we do not claim a broadcast measurement.", "justify")
        names4 = {"blackboard": "Blackboard", "direct message passing": "Direct message passing", "no sharing": "No sharing"}
        rows4 = []
        for k, nm in names4.items():
            c = e4[k]
            w = c["wall_ms"]
            rows4.append((nm, f"{c['correct']}/{c['runs']}", sec(w["median"]), f"{sec(w['mean'])} / {sec(w['stdev'])}",
                          f"{c['mean_blackboard_reads']:.0f}", f"{c['mean_context_chars_shared']:.0f}",
                          f"{c['investigator_saw_evidence_runs']} / {c['forensic_saw_evidence_runs']}"))
        b.table("t_e4", "E4 messaging comparison, 40 cases each, wall time in seconds (source: results/messaging_comparison.csv, summary.json).",
                ["Messaging", "Correct", "Median", "Mean / stdev", "Blackboard reads", "Context shared (chars)", "Evidence seen by Investigator / Forensic (runs)"],
                rows4, widths=[2.3, 0.7, 0.8, 1.2, 1.0, 1.1, 1.6], size=8)
        b.para("Blackboard and direct passing take about the same time (median 24.25 s and 24.11 s). The blackboard shares twice "
               "the context (4,740 against 2,370 characters per claim on average) because the Forensic Agent also reads the "
               "verified Evidence report, and it is the only style that records signed read receipts (2 per claim).", "justify")

        # --- E5
        b.para("**E5: persona and prompt-structure runs (course criterion C-10).** The 40 dataset cases and 18 sample-claim "
               "variants ran on the product wiring under two prompt structures: the standard packet and an evidence-first "
               "(documents first, claim form last) packet with a ReAct instruction. That makes 116 runs, none failed. The persona "
               "counts are unbalanced: for each structure there are 38 honest, 6 vague, 6 abusive and 8 injection runs, because the "
               "personas come from different sources and we did not rebalance them.", "justify")
        rows5 = []
        bp = e5["by_persona_and_structure"]
        for per in ("honest", "vague", "abusive", "injection"):
            for st, lab in (("standard", "standard"), ("evidence_first_react", "evidence-first ReAct")):
                c = bp[per][st]
                w = c["wall_ms"]
                rows5.append((per, lab, str(c["runs"]), f"{c['correct']}/{c['runs']}", sec(w["median"]), sec(w["mean"]), sec(w["stdev"])))
        b.table("t_e5", "E5 persona x prompt structure, default wiring, wall time per claim in seconds (source: results/persona_prompt_runs.csv, summary.json).",
                ["Persona", "Prompt structure", "Runs", "Correct", "Median", "Mean", "Stdev"],
                rows5, widths=[1.5, 2.2, 0.8, 1.0, 1.0, 1.0, 1.0], size=8.5)
        b.para(f"Across the {e5['paired_comparisons']} paired comparisons (the same claim under the two structures) the decision "
               f"changed in {e5['decision_changed_by_structure']}. All 116 runs matched the ground truth. This is not evidence that "
               "prompt structure does not matter for a language model: the doubles never read the prompt wording, so the result "
               "was certain in advance. The only real change is the document order in the ADK packet. The CrewAI agent always "
               "receives the standard packet because it runs in another process. The low mean for the vague persona (21.3 s "
               "under the standard structure) comes from a single run: EVAL-25 under the standard structure finished in 6.1 s, while "
               "the same case took 24.1 s under the evidence-first structure and 21.8 and 24.1 s in other wirings. That early finish "
               "did not repeat and we did not find its cause, so we do not read it as an effect of persona or structure. "
               "Run accounting: the 236 runs are E3 120, E4 direct passing 40 and 76 new E5 runs; the 40 standard-structure dataset "
               "runs of E5 are the hierarchical-blackboard rows of E3 and are not counted twice.", "justify")

        # --- latency
        b.para("**Per-agent and system latency.** Over all 236 runs on the laptop (milliseconds):", "justify", keep=True)
        L = S["per_agent_and_system_latency_ms_all_runs"]
        rowsL = []
        for k, nm in (("evidence_ms", "Evidence Agent (ADK)"), ("investigator_ms", "Investigator Agent (ADK)"),
                      ("forensic_ms", "Forensic Agent (CrewAI, A2A)"), ("wall_ms", "System, wall time per claim")):
            x = L[k]
            rowsL.append((nm, str(x["n"]), f"{x['median']:.0f}", f"{x['mean']:.0f}", f"{x['stdev']:.0f}", f"{x['p25']:.0f}", f"{x['p75']:.0f}", f"{x['p95']:.0f}", f"{x['min']:.0f}", f"{x['max']:.0f}"))
        b.table("t_lat", "Latency per agent and for the system, all 236 offline runs, macOS laptop, ms (source: results/summary.json).",
                ["Component", "n", "Median", "Mean", "Stdev", "p25", "p75", "p95", "Min", "Max"],
                rowsL, widths=[2.6, 0.5, 0.8, 0.8, 0.7, 0.8, 0.8, 0.8, 0.7, 0.8], size=8)
        b.figure("f_res_box", RESULTS / "boxplot_latency.png",
                 "System latency per claim, offline, macOS laptop: E3/E4 wirings (left, 40 cases each) and E5 persona x prompt structure (right). Diamonds are means.", width=6.4)
        b.figure("f_res_heat", RESULTS / "heatmap_agent_latency.png",
                 "Mean latency per agent and persona, standard prompt structure only, offline test doubles, macOS laptop, ms.", width=5.2)
        b.para("The few short runs near 3 to 7 s come mostly from one case, EVAL-24 (missing evidence), which is consistently "
               "short, and from one run of EVAL-25 that did not repeat; we did not find the cause. Everything else sits within a narrow "
               "band, which is why the boxes are thin. The Forensic Agent takes about 0.54 s and the "
               "two ADK agents about 12 s each in this setup. These are properties of the local stack with test doubles; they "
               "are not Gemini latencies.", "justify")
        b.para("**Which latencies to trust.** The laptop figures above are not the deployment numbers. On the AWS VM in OFFLINE mode (2026-11-19) the run "
               "record `RUN-dash-6ac716c9bd` shows agent latencies of 0.27 to 1.14 s (ADK agents 445 and 266 ms, the CrewAI agent "
               "1,142 ms) and a sample claim takes about 3 s end to end. On the macOS laptop, "
               "in Docker or native, a claim takes about 24 s, with about 12 s per ADK agent (the earlier 24 to 27 s Docker Desktop "
               "figure and the 236 unloaded experiment runs agree). We did not isolate the cause of this difference between the "
               "laptop and the VM, so the VM offline figures are the deployment measurement for offline mode. The deployed VM now runs live mode, where the "
               "one partial run shows 57.8 s and 23.6 s for the two ADK agents (Section 7.4), a different regime that is not comparable. "
               "The offline VM record is saved as `docs/submission/results/aws_run_RUN-dash-6ac716c9bd.json`.", "justify")

        # --- interpretation
        b.para("**What the comparisons do and do not justify.** Accuracy is identical across all configurations, and that is "
               "true by construction, not a finding: the offline doubles read only the claim packet, not the other agents' reports "
               "and not the prompt wording, and the decision comes from the deterministic adjudicator. The experiments therefore "
               "cannot show that one architecture or messaging style reasons better. What they do measure is wall time, number of "
               "calls, the context that moves between agents and who sees which report. We justify keeping the hierarchical manager "
               "with the blackboard on that measured evidence together with design heuristics: the Investigator and the Forensic "
               "Agent form independent opinions because the Forensic Agent may not read the Investigator's report; a failing agent "
               "is contained and the claim goes to a human; every report is signed and every read leaves a receipt, so the run can "
               "be audited; and the two analysis stages run in parallel after the Evidence stage, which is where the parallel "
               "speedup comes from. Parallel-only is faster on the laptop, but it has no verification gate and the Investigator "
               "never receives the Evidence report. Whether the blackboard helps a language model reason better is not tested "
               "here and would need live runs.", "justify")

        # --- cost
        C = S["cost"]
        b.para("**Cost.** In offline mode the cost is INR 0: no model is billed and the ledger shows 0 Gemini requests. A live cost "
               "over 40 runs was not measured. `observability/pricing.py` ships no prices, so a live rupee figure would need a "
               "verified price table and token counts, and none were recorded for the live runs. What exists is the request count: "
               "the four complete live runs used 9, 6, 7 and 7 requests (29 in total); the partial run on the VM added 7 requests more. Forty live runs would therefore need about "
               "240 to 360 requests (about 400 at the code's estimate of 10 per claim), against a free tier of 20 requests per day "
               "per model.", "justify")
        b.table("t_cost", "Cost and request counts (source: results/summary.json, docs/EVIDENCE.md).",
                ["Item", "Value"],
                [("Offline cost, 236 runs plus the 40-case baseline", "INR 0 (0 Gemini requests)"),
                 ("Live requests in the 4 complete live runs", "9, 6, 7, 7 (total 29)"),
                 ("Live requests needed for 40 live runs", "about 240 to 360 (400 at the code's estimate)"),
                 ("Free tier", "20 requests per day per model"),
                 ("Live token counts or rupee cost", "not measured")], widths=[5, 5], size=9)
    b.para("One limit applies to all offline experiments: the test doubles are rule-based and do not react to wording, so a change "
           "of prompt wording cannot change their output. Only variants that really change the packet or the control flow can be "
           "reported as variance. No live accuracy exists.", "justify")
    b.h2("Known limitations")
    b.para("From `docs/EVIDENCE.md` and our own testing:", "justify")
    b.table("t_limits", "Known limitations.", ["Limitation", "Detail"],
            [
                ("Partial MCP record loss", "If some records survive, the run is judged call by call and is not flagged; it can settle with unverified HIGH concerns and no person. A test pins this and does not endorse it."),
                ("Reviewer authentication", "The reviewer name is typed in. The review lock works within one process."),
                ("Older failed runs", "13 runs from 2 to 4 November stay unclassified; they predate failure classification."),
                ("Live evidence", "Four complete runs on two sample claims. No live accuracy measure."),
                ("Prompt injection", "The screen is a pattern list. The tests show the safeguards around a manipulated test double, not model resistance."),
                ("Platform", "The full regression passed on Windows 11 with Python 3.12.10 (2026-11-06); on macOS (2026-11-19) 795 passed and 1 existing test failed. The deployed system runs on Ubuntu 24.04 ARM (AWS) and was checked with the four sample claims, not with the suites."),
                ("Input format", "Uploaded PDFs must use the samples' headings and `Label: value` lines. Scanned PDFs are rejected."),
                ("Concurrency", "One investigation runs at a time. The blackboard needs all writers on one machine."),
            ], widths=[2.6, 7.6], size=8.5)
    b.h2("Deployment status")
    b.para("The system is deployed on AWS (EC2 t4g.small, Ubuntu 24.04 ARM, ap-south-1) at `https://13-204-3-14.sslip.io/` (Elastic IP "
           "13.204.3.14). The checks in the table below were made on 2026-11-19 over HTTPS in OFFLINE mode; live mode was enabled on the "
           "VM afterwards (2026-11-19) and one partial live claim was run there (Section 7.4). This is a smoke test of the deployment, not a "
           "new benchmark. The record is `docs/submission/DEPLOYED.txt`.", "justify")
    b.table("t_deploy", "Deployment verification on AWS, offline mode, before the Elastic IP (source: DEPLOYED.txt, 2026-11-19).",
            ["Check", "Result"],
            [
                ("Login", "A wrong password was rejected with HTTP 401; the correct login opened the dashboard over HTTPS."),
                ("Four sample claims through the public URL", "APPROVE, ESCALATE, ESCALATE, REQUEST_EVIDENCE, matching the expected results; about 3 seconds each."),
                ("Stop and start of the instance", "Before the Elastic IP was attached, the public IP changed and the site came back by itself on the new sslip.io host name; a blackboard run after the restart completed. With the Elastic IP the URL is stable across stop/start (not re-tested)."),
                ("One live claim on the VM (partial)", "RUN-dash-6d15294eca: two ADK agents completed on Gemini, the CrewAI agent failed with HTTP 503 after its retry; INCOMPLETE, human approval required, APPROVE from the adjudicator (Section 7.4)."),
                ("Image and memory (laptop measurement)", "Image 911 MB; memory 339 MiB idle and 383 MiB peak in Docker Desktop. Not measured on the instance."),
            ], widths=[3.0, 7.2], size=8.5)
    b.para("Not verified: a complete live run on the VM (the one live claim was partial), live accuracy, load from several users, or a "
           "stop/start with the Elastic IP attached. The about 3 seconds per claim is the offline test doubles running on the VM "
           "(offline mode, 2026-11-19); it is not the latency of the live system. On the macOS laptop (Docker or native) a claim takes about 24 s, with about 12 s per ADK agent; "
           "the cause was not isolated. On the VM the run record `RUN-dash-6ac716c9bd` (`results/aws_run_RUN-dash-6ac716c9bd.json`) shows "
           "agent latencies of 445 and 266 ms for the ADK agents and 1,142 ms for the CrewAI agent, so these are the offline VM measurements. "
           "Running cost is about 0.0112 USD per hour for the instance plus about 0.005 USD per hour for the public IPv4 address, which is the same for the Elastic IP and is charged while the instance is stopped; "
           "the disk is billed separately, on an account with free credits.", "justify")

    # ===================================================================== 8 TEAM
    b.h1("Team Responsibility/Self-contribution Matrix")
    b.para("Each activity has one lead and one supporting member, shown in the matrix below. The split follows the areas "
           "each member worked on; the repository was committed from one account, so commits do not show it.", "justify")
    b.matrix_anchor = b.doc.add_paragraph()   # the template table is moved right after this paragraph
    b.para("Lead = main owner of the activity; Support = second member who helped. This split is PROPOSED and is to be confirmed by the team.", "justify")
    b.para("The table below maps each matrix activity to where it lives in the repository.",
           "justify")
    b.table("t_map", "Where each matrix activity is in the repository.",
            ["Activity", "Main files"],
            [
                ("Agent design / multi-agent architecture", "`adk_service/agents.py`, `crew_service/crew.py`, `schemas.py`"),
                ("Agent orchestration", "`orchestration/manager.py`, `orchestration/plan.py`, `integration/run.py`"),
                ("State management", "`blackboard/` (store, controller, auth, models, monitor)"),
                ("MCP design", "`mcp_server/server.py`, `policy_search.py`, `history.py`"),
                ("A2A communication", "`crew_service/a2a_server.py`, `adk_service/forensic_a2a.py`, `contract.py`"),
                ("Guardrails / callbacks", "`security.py`, `adk_service/callbacks.py`, CrewAI guardrail in `crew_service/crew.py`"),
                ("Observability", "`observability/` (trace, metrics, outcome, provider errors, pricing)"),
                ("Integration and testing", "`integration/`, `tests/`, `tests/crew/`"),
                ("Evaluation and documentation", "`evaluation/`, `docs/`"),
                ("Presentation / interface UI", "`dashboard/` and `dashboard/static/`"),
                ("Deployment", "`Dockerfile.single`, `docker-compose.yml`, `deploy/` and `docs/DEPLOYMENT_GUIDE.md`"),
                ("End-to-end evaluation (time and cost)", "`evaluation/`, `gemini_rate.py`, `observability/metrics.py`, `docs/submission/results/`"),
            ], widths=[3.4, 6.8], size=8.5)

    # ===================================================================== CONCLUSIONS ...
    b.h1("Conclusions", numbered=False)
    b.para("We built a warranty-claim investigator in which three agents from two frameworks cooperate over MCP, A2A and a "
           "shared blackboard, and in which a deterministic adjudicator, not a model, decides and pays. The design choice we "
           "would defend hardest is the last one. In every test and every stored run an agent could at most make the outcome "
           "more cautious; it could not approve or pay, and a missing or unverifiable part sent the claim to a person.", "justify")
    b.para("What worked: the blackboard with signed receipts kept the two independent opinions independent; the scope rule on the "
           "MCP server made the data-access attack sample harmless regardless of what the agent did; and the one-retry, "
           "one-ledger approach let us finish four complete live runs on a quota of 20 requests a day. What was harder than we "
           "expected: the dependency conflict between ADK and CrewAI shaped everything, from two environments to the need for "
           "A2A to the choice of a single-machine deployment, and live Gemini calls fail more often than the documentation "
           "suggests (we met an HTTP 503 in a reference run).", "justify")
    b.para("What we cannot claim: live accuracy, because there are four live runs; generalisation, because the data are "
           "synthetic and written by us; and resistance to attacks that match none of our patterns. Future work, in the order we "
           "would do it: measure live accuracy and cost over many runs with a paid key, authenticate reviewers, replace the "
           "pattern list with a layered detector, and move the blackboard to a server database so that agents can run on "
           "several machines.", "justify")

    b.h1("References", numbered=False)
    refs = REFS
    for i, r in enumerate(refs, 1):
        p = b.doc.add_paragraph()
        p.paragraph_format.left_indent = Inches(0.35)
        p.paragraph_format.first_line_indent = Inches(-0.35)
        add_runs(p, f"[{i}] {r}", size=10.5)
        set_spacing(p, 0, 4, 1.1)
        b._adv(2 * LINE_PT + 4)

    b.h1("Appendix", numbered=False)
    b.para("**A. Commands to reproduce the offline results**", keep=True)
    b.para("From the repository root, after installing both environments as in the README (PowerShell on Windows):", "justify")
    for line in (
        ".venv\\Scripts\\python -m pytest tests --ignore=tests\\crew",
        ".venv-crew\\Scripts\\python -m pytest tests\\crew",
        ".venv\\Scripts\\python -m claimsleuth.evaluation run",
        ".venv\\Scripts\\python -m claimsleuth.dashboard      # then open http://127.0.0.1:8110/",
    ):
        p = b.doc.add_paragraph()
        p.paragraph_format.left_indent = Inches(0.3)
        add_runs(p, f"`{line}`", size=10)
        set_spacing(p, 0, 2, 1.0)
        b._adv(16)
    b.para("On Windows the main suite takes about 35 minutes and the CrewAI suite about 9 (the macOS main suite took about 75 minutes). Live mode needs `GOOGLE_API_KEY` in `.env` and "
           "`--mode live`; do not use it without checking the request ledger first.", "justify")
    _dep = HERE / "DEPLOYED.txt"
    _url = ""
    if _dep.exists():
        for _ln in _dep.read_text(encoding="utf-8").splitlines():
            if _ln.startswith("URL on"):
                _url = _ln.split(":", 1)[1].strip() if False else _ln
    if _url:
        _u = _url[_url.index("https://"):].strip()
        b.para("**Deployed system.** URL on 2026-11-20 (Elastic IP, stable across stop/start, see Section 5.10): `" + _u + "`. The deployed system runs "
               "in live mode on the free tier, which allows only about 2 to 3 live claims per day (a claim costs 6 to 9 requests); offline mode is selectable on the VM. "
               "The login is given to the examiners separately and is not part of this report.", "justify")
    b.para("**B. The four sample claims and their expected outcomes**", keep=True)
    b.table("t_samples", "Sample claims (source: README.md and data/sample_ground_truth.json).",
            ["Claim", "Content", "Expected result"],
            [
                ("WC-2026-0411", "Complete smartphone claim", "APPROVE, payout ₹16,400.00; human sign-off (at or above ₹15,000)"),
                ("WC-2026-0412", "Refrigerator; two different serial numbers; earlier claim on the serial; hidden instruction in the repair report", "ESCALATE; injection at PDF p.3 lines 12 to 14 not followed; serial mismatch reported"),
                ("WC-2026-0413", "Smartphone; the invoice asks to access all customer claim histories", "ESCALATE; data access attempt at p.2 lines 10 to 12; lookups stayed within the claim"),
                ("WC-2026-0414", "Laptop; no repair report", "REQUEST_EVIDENCE; the missing report is named"),
            ], widths=[1.6, 5.0, 3.6], size=8.5)
    b.para("**C. Where the report's numbers come from**", keep=True)
    b.para("Test counts, the 40-case results, the live run table, the D8 findings, the stored-run counts and the fresh-install "
           "result are quoted from `docs/EVIDENCE.md` (dated 6 November 2026). Ports, versions, mechanisms and thresholds are "
           "from the code, `README.md` and `docs/ARCHITECTURE_GUIDE.md`. Anything in Section 7.7 (final experiments) comes from "
           "`docs/submission/results/` and is regenerated each time `docs/submission/build_report.py` is run.", "justify")


# ----------------------------------------------------------------------------------------------------------------
# front matter, contents, section break
# ----------------------------------------------------------------------------------------------------------------
def replace_text(par, text):
    for i, r in enumerate(par.runs):
        r.text = text if i == 0 else ""


def strip_highlights(doc):
    for hl in doc.element.body.iter(qn("w:highlight")):
        hl.getparent().remove(hl)
    # iter while removing is unsafe for some trees; run once more for leftovers
    for hl in list(doc.element.body.iter(qn("w:highlight"))):
        hl.getparent().remove(hl)


def fill_front(doc, team):
    P = doc.paragraphs
    title = team["title"]
    replace_text(P[5], title)
    for i, m in enumerate(team["members"]):
        name, enr = m["name"], m["enrollment"]
        replace_text(P[7 + 2 * i], name)
        replace_text(P[8 + 2 * i], enr)
        if not is_placeholder(name):
            replace_text(P[22 + i], f"{name} ({enr})      Signature: ____________________")
    decl = P[21]
    decl.runs[1].text = '"'
    decl.runs[2].text = title


def contents_entries(doc, headings, pages):
    """Replace the template's contents block with one entry per Heading 1 using PAGEREF fields."""
    body = doc.element.body
    paras = list(doc.paragraphs)
    start = None
    for i, p in enumerate(paras):
        if p.text.strip() == "Page No.":
            start = i + 1
            break
    end = None
    for i in range(start, len(paras)):
        if paras[i]._p.xpath('.//w:br[@w:type="page"]') or paras[i]._p.xpath(".//w:sectPr"):
            end = i
            break
    anchor_prev = paras[start - 1]._p
    template_item = copy.deepcopy(paras[start]._p)       # numbered List Paragraph entry
    for p in paras[start:end]:
        body.remove(p._p)
    prev = anchor_prev
    for key, text, numbered in headings:
        item = copy.deepcopy(template_item)
        # clear runs
        for r in list(item.findall(qn("w:r"))):
            item.remove(r)
        ppr = item.find(qn("w:pPr"))
        if not numbered:
            numpr = ppr.find(qn("w:numPr"))
            if numpr is not None:
                ppr.remove(numpr)
            ind = OxmlElement("w:ind")
            ind.set(qn("w:left"), "11")
            ppr.find(qn("w:spacing")).addnext(ind)
        tabs = OxmlElement("w:tabs")
        tab = OxmlElement("w:tab")
        tab.set(qn("w:val"), "right")
        tab.set(qn("w:leader"), "dot")
        tab.set(qn("w:pos"), "9900")
        tabs.append(tab)
        # tabs must precede spacing in pPr per schema order; insert after numPr/pBdr, before spacing
        sp = ppr.find(qn("w:spacing"))
        sp.addprevious(tabs)
        spacing = sp
        spacing.set(qn("w:before"), "100")
        from docx.text.paragraph import Paragraph
        par = Paragraph(item, paras[start]._parent)
        r = par.add_run(text)
        set_run_font(r, 14, bold=numbered)
        r = par.add_run("\t")
        set_run_font(r, 14)
        add_field(par, f"PAGEREF _Toc_{key} \\h", pages.get(key, 1), size=14)
        prev.addnext(item)
        prev = item


def insert_front_section_break(doc):
    """Convert the page-break paragraph before the first Heading 1 into a section break so body numbering restarts at 1."""
    paras = doc.paragraphs
    first_h1 = next(i for i, p in enumerate(paras) if p.style.name == "Heading 1")
    # last paragraph before the first heading that holds a page break
    idx = None
    for i in range(first_h1 - 1, -1, -1):
        if paras[i]._p.xpath('.//w:br[@w:type="page"]'):
            idx = i
            break
    final_sect = doc.element.body.find(qn("w:sectPr"))
    sect = copy.deepcopy(final_sect)
    for tag in ("w:headerReference", "w:footerReference", "w:titlePg", "w:pgNumType"):
        for e in sect.findall(qn(tag)):
            sect.remove(e)
    typ = OxmlElement("w:type")
    typ.set(qn("w:val"), "nextPage")
    sect.insert(0, typ)
    par = paras[idx]
    for br in par._p.xpath('.//w:br[@w:type="page"]'):
        br.getparent().remove(br)
    par._p.get_or_add_pPr().append(sect)


def footer_page_numbers(doc):
    sec = doc.sections[-1]
    sec.footer.is_linked_to_previous = False
    fp = sec.footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for r in list(fp.runs):
        r._r.getparent().remove(r._r)
    add_field(fp, "PAGE", 1, size=10)
    pg = OxmlElement("w:pgNumType")
    pg.set(qn("w:start"), "1")
    cols = sec._sectPr.find(qn("w:cols"))
    if cols is not None:
        cols.addprevious(pg)
    else:
        sec._sectPr.append(pg)
    # the front-matter section keeps an empty footer
    front = doc.sections[0]
    front.footer.is_linked_to_previous = False


def set_update_fields(doc):
    settings = doc.settings.element
    uf = OxmlElement("w:updateFields")
    uf.set(qn("w:val"), "true")
    for tag in ("w:hdrShapeDefaults", "w:footnotePr", "w:endnotePr", "w:compat", "w:docVars", "w:rsids", "m:mathPr"):
        try:
            nxt = settings.find(qn(tag))
        except Exception:
            nxt = None
        if nxt is not None:
            nxt.addprevious(uf)
            return
    settings.append(uf)


def fill_matrix(table, team):
    for i, m in enumerate(team["members"]):
        cell = table.rows[0].cells[1 + i]
        paras = cell.paragraphs
        if len(paras) >= 2:
            replace_text(paras[1], m["name"])
            for r in paras[1].runs:
                set_run_font(r, 10, bold=True)
    # Lead / Support cells from TEAM.json["matrix"]; the 13th (free) row is left empty
    norm = lambda t: re.sub(r"[^a-z0-9]", "", t.lower())
    mx = {norm(k): v for k, v in team.get("matrix", {}).items()}
    names = [m["name"] for m in team["members"]]
    for row in table.rows[1:]:
        entry = mx.get(norm(row.cells[0].text))
        if not entry:
            continue
        for j, nm in enumerate(names):
            word = "Lead" if entry.get("L") == nm else "Support" if entry.get("S") == nm else ""
            if not word:
                continue
            par = row.cells[1 + j].paragraphs[0]
            for r in list(par.runs):
                r._r.getparent().remove(r._r)
            par.alignment = WD_ALIGN_PARAGRAPH.CENTER
            set_run_font(par.add_run(word), 10, bold=(word == "Lead"))


# ----------------------------------------------------------------------------------------------------------------
def one_pass(team, summary, reg, pages_hint):
    doc = Document(str(TEMPLATE))      # a fresh copy each pass; the original file is only read
    body = doc.element.body
    fill_front(doc, team)

    # keep the template matrix table, remove the template's body (headings and guidance) after the page-break paragraph
    template_table = copy.deepcopy(doc.tables[0]._tbl)
    paras = doc.paragraphs
    first_h1 = next(i for i, p in enumerate(paras) if p.style.name == "Heading 1")
    for el in list(body)[list(body).index(paras[first_h1]._p):]:
        if el.tag != qn("w:sectPr"):
            body.remove(el)

    b = Builder(doc, reg, pages_hint)
    build_content(b, team, summary)
    # move the template matrix table into section 8
    body.remove(b.matrix_anchor._p) if False else None
    b.matrix_anchor._p.addnext(template_table)
    from docx.table import Table
    tbl = Table(template_table, doc)
    fill_matrix(tbl, team)

    strip_highlights(doc)
    # estimated pages for the contents
    pages = {k: int(y // PAGE_PT) + 1 for k, y in b.head_y.items()}
    contents_entries(doc, b.headings, pages_hint if pages_hint else pages)
    insert_front_section_break(doc)
    footer_page_numbers(doc)
    set_update_fields(doc)
    return doc, b, pages


def main():
    team = json.loads((HERE / "TEAM.json").read_text(encoding="utf-8"))
    summary = load_summary()
    report_figures.make_all()
    doc, b, pages = one_pass(team, summary, {}, {})
    doc, b, pages = one_pass(team, summary, b.newreg, pages)      # references and cached page numbers now known
    doc.save(str(OUT))
    print("wrote", OUT)
    print("headings:", [(t, pages[k]) for k, t, _ in b.headings])
    print("tables:", b.counts["Table"], "figures:", b.counts["Figure"])


if __name__ == "__main__":
    main()
