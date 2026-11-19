"""Greyscale diagrams for the final report, drawn with matplotlib.

Run through build_report.py (or directly: python report_figures.py). Output: docs/submission/figures/*.png
Black, white and greys only, so they print and export cleanly.
"""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle, Ellipse

FIG_DIR = Path(__file__).resolve().parent / "figures"
FONT = "DejaVu Sans"
plt.rcParams["font.family"] = FONT


def _canvas(w, h):
    fig, ax = plt.subplots(figsize=(w, h), dpi=200)
    ax.set_xlim(0, w * 10)
    ax.set_ylim(0, h * 10)
    ax.axis("off")
    return fig, ax


def box(ax, x, y, w, h, text, fc="#ffffff", lw=1.2, fs=7.5, bold=False, ls="-", round_=True):
    style = "round,pad=0.02,rounding_size=1.2" if round_ else "square,pad=0"
    p = FancyBboxPatch((x, y), w, h, boxstyle=style, fc=fc, ec="black", lw=lw, ls=ls)
    ax.add_patch(p)
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fs,
            fontweight="bold" if bold else "normal", color="black", linespacing=1.25)
    return (x, y, w, h)


def region(ax, x, y, w, h, title, fc="#f2f2f2", ls="--"):
    ax.add_patch(Rectangle((x, y), w, h, fc=fc, ec="#444444", lw=1.0, ls=ls))
    ax.text(x + 1.2, y + h - 1.6, title, ha="left", va="top", fontsize=7.5, fontweight="bold", color="black")


def cylinder(ax, x, y, w, h, text, fs=7.5):
    ax.add_patch(Rectangle((x, y + 1.2), w, h - 2.4, fc="#e6e6e6", ec="black", lw=1.2))
    ax.add_patch(Ellipse((x + w / 2, y + 1.2), w, 2.4, fc="#e6e6e6", ec="black", lw=1.2))
    ax.add_patch(Ellipse((x + w / 2, y + h - 1.2), w, 2.4, fc="#cfcfcf", ec="black", lw=1.2))
    ax.text(x + w / 2, y + h / 2 - 0.3, text, ha="center", va="center", fontsize=fs, linespacing=1.25)


def arrow(ax, p, q, text=None, dashed=False, both=False, tx=0, ty=1.2, fs=6.5, rad=0.0, ha="center"):
    style = "<|-|>" if both else "-|>"
    ax.annotate("", xy=q, xytext=p,
                arrowprops=dict(arrowstyle=style, color="black", lw=1.0, ls="--" if dashed else "-",
                                shrinkA=0, shrinkB=0, mutation_scale=9,
                                connectionstyle=f"arc3,rad={rad}"))
    if text:
        ax.text((p[0] + q[0]) / 2 + tx, (p[1] + q[1]) / 2 + ty, text, ha=ha, va="center", fontsize=fs,
                bbox=dict(fc="white", ec="none", pad=0.6))


def _save(fig, name):
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    out = FIG_DIR / name
    fig.savefig(out, bbox_inches="tight", facecolor="white", pad_inches=0.08)
    plt.close(fig)
    return out


def fig_architecture():
    fig, ax = _canvas(10.0, 7.0)
    box(ax, 33, 63, 34, 5.5, "Browser: claim investigation page\n(upload PDF, report, human review)", fs=7)
    box(ax, 33, 53, 34, 6, "Dashboard (Starlette, port 8110)\nPDF intake, job manager, report views", fs=7)
    arrow(ax, (50, 63), (50, 59))

    region(ax, 3, 24, 94, 26.5, "Level 1: manager (orchestration/manager.py)")
    box(ax, 33, 41, 34, 6, "Typed ExecutionPlan: 5 tasks\nbaseline, evidence, investigation,\nforensics, adjudication", fs=6.8)
    arrow(ax, (62, 53), (62, 47))

    box(ax, 6, 26.5, 26, 9, "Evidence Agent\nGoogle ADK LlmAgent\nstep 1 of SequentialAgent", fs=6.8)
    box(ax, 37, 26.5, 26, 9, "Investigator Agent\nGoogle ADK LlmAgent\nstep 2 of SequentialAgent", fs=6.8)
    box(ax, 68, 26.5, 26, 9, "Forensic Agent\nCrewAI Agent / Task / Crew\nseparate process, port 8103", fs=6.8)
    arrow(ax, (42, 41), (19, 35.5), "1 dispatch", tx=-6, ty=1.8)
    arrow(ax, (50, 41), (50, 35.5), "2a release", tx=6, ty=0.8)
    arrow(ax, (58, 41), (81, 35.5), "2b dispatch over A2A", tx=9, ty=1.8)

    cylinder(ax, 36, 8, 28, 10, "Blackboard (SQLite, WAL)\nsigned reports, receipts,\nseal, decision")
    arrow(ax, (14, 26.5), (38, 17.5), "publish Evidence\nreport", tx=0, ty=0)
    arrow(ax, (50, 18), (50, 26.5), "consume", tx=5, ty=0, both=True)
    arrow(ax, (74, 26.5), (62, 17.5), "consume (ContextRef),\npublish Forensic report", tx=0, ty=0)

    box(ax, 70, 8, 26, 10, "MCP server (port 8102)\nwarranty_policy_search\nclaim_history_lookup", fs=6.8)
    arrow(ax, (90, 26.5), (90, 18), "MCP", dashed=True, tx=0, ty=0)

    box(ax, 3, 8, 28, 10, "Deterministic adjudicator\nverified reports only\nthe only decision authority", fs=6.8)
    arrow(ax, (36, 12), (31, 12), "sealed,\nverified", tx=0, ty=4.2)
    box(ax, 3, 0.5, 28, 5, "Human reviewer\n(approve / reject / request info)", fs=6.5)
    arrow(ax, (17, 8), (17, 5.5))
    ax.text(97, 2.5, "solid arrow: control or data flow    dashed arrow: MCP tool call\n"
                     "All three agents call the MCP server (see the MCP tools figure); only one arrow is drawn.",
            ha="right", fontsize=6, va="center")
    return _save(fig, "fig_mas_architecture.png")


def fig_mcp():
    fig, ax = _canvas(9.4, 5.4)
    region(ax, 2, 8, 38, 42, "Agents (MCP clients)")
    box(ax, 5, 36, 32, 8, "Evidence Agent (ADK)\nMcpToolset, tool_filter =\n[warranty_policy_search]", fs=6.8)
    box(ax, 5, 23, 32, 8, "Investigator Agent (ADK)\nMcpToolset, tool_filter =\n[both tools]", fs=6.8)
    box(ax, 5, 10.5, 32, 8, "Forensic Agent (CrewAI)\nMCPServerHTTP, tool_filter =\n[claim_history_lookup]", fs=6.8)

    region(ax, 50, 0.5, 43, 49.5, "MCP server (official MCP SDK, port 8102)")
    box(ax, 53, 36, 37, 7, "Streamable HTTP endpoint /mcp\nlogging middleware on every tools/call", fs=6.5)
    box(ax, 53, 24, 25, 9, "warranty_policy_search\npolicy_id and/or search_query\nlimit 1-10, returns terms\nand a source pointer", fs=5.8)
    box(ax, 53, 11.5, 25, 9, "claim_history_lookup\nclaim_id, claimant_id,\nserial_number; scope rule:\nthis claim's own data only", fs=5.8)
    cylinder(ax, 80, 24.5, 11.5, 8, "policies\n.json", fs=5.8)
    cylinder(ax, 80, 12, 11.5, 8, "claimsleuth.db\n(SQLite)", fs=5.2)
    arrow(ax, (78, 28.5), (80, 28.5), None)
    arrow(ax, (78, 16), (80, 16), None)
    arrow(ax, (37, 40), (53, 40), "tools/list,\ntools/call", ty=3)
    arrow(ax, (37, 27), (53, 28.5), None)
    arrow(ax, (37, 14.5), (53, 16), None)
    ax.text(2, 3.5, "Headers X-ClaimSleuth-Run-Id, -Claim-Id and -Caller are set by\nframework code, not by the model. Each call is logged to\nruntime/logs/mcp_tool_calls.jsonl (run, claim, caller, tool, status, ms).",
            ha="left", va="center", fontsize=6.0, bbox=dict(fc="white", ec="black", lw=0.6, pad=2))
    return _save(fig, "fig_mcp_tools.png")


def fig_sequence():
    fig, ax = _canvas(10.0, 7.4)
    names = ["Manager\n(orchestration)", "Evidence Agent\n(ADK)", "Blackboard\n(SQLite)", "Investigator\n(ADK)",
             "A2A client /\nCrewAI service", "Adjudicator"]
    xs = [8, 24, 40, 56, 72, 90]
    top, bot = 70, 3
    for x, n in zip(xs, names):
        box(ax, x - 7, top, 14, 4.5, n, fs=6.3)
        ax.plot([x, x], [top, bot], color="#555555", lw=0.8, ls=(0, (4, 3)))
    msgs = [
        (0, 1, 66, "1 run Evidence task"),
        (1, 2, 62, "2 publish validated Evidence report"),
        (2, 0, 58, "3 ContextRef: Evidence COMPLETED"),
        (0, 3, 54, "4a release (authorise) Investigator"),
        (0, 4, 50, "4b A2A FORENSIC_REQUEST + ContextRef"),
        (2, 3, 45.5, "5a consume report (receipt)"),
        (2, 4, 41.5, "5b consume report with own key (receipt)"),
        (3, 2, 37, "6a publish Investigator report"),
        (4, 2, 33, "6b publish Forensic report"),
        (4, 0, 29, "7 FORENSIC_RESPONSE over A2A"),
        (0, 2, 25, "8 seal run, verify reports"),
        (0, 5, 21, "9 verified reports only"),
        (5, 2, 17, "10 record final decision"),
    ]
    for a, b, y, t in msgs:
        xa, xb = xs[a], xs[b]
        sgn = 1 if xb > xa else -1
        ax.annotate("", xy=(xb - sgn * 0.5, y), xytext=(xa + sgn * 0.5, y),
                    arrowprops=dict(arrowstyle="-|>", lw=1.0, color="black", mutation_scale=9))
        ax.text((xa + xb) / 2, y + 1.3, t, ha="center", fontsize=6.2, bbox=dict(fc="white", ec="none", pad=0.4))
    ax.text(50, 8, "Steps 4 to 6: the Investigator and the Forensic Agent run in parallel.\n"
                   "Neither can read the other's report (read permissions on the Blackboard).",
            fontsize=6.3, ha="center", va="center", bbox=dict(fc="white", ec="black", lw=0.6, pad=2))
    return _save(fig, "fig_a2a_blackboard_sequence.png")


def fig_deployment():
    fig, ax = _canvas(9.5, 7.2)
    box(ax, 2, 40, 16, 8, "Browser\n(reviewer, examiner)", fs=7)
    box(ax, 2, 22, 16, 8, "Gemini API\n(live mode, key\nfrom env file; app caps\n20/day, 5/min)", fs=6.0, ls="--")
    region(ax, 24, 3, 70, 58, "AWS EC2 t4g.small (ARM, 2 GB RAM), ap-south-1, Elastic IP 13.204.3.14")
    region(ax, 27, 6, 64, 38, "Docker Compose on the VM (ports 80 and 443 public; 22 restricted)", fc="#ececec")
    box(ax, 29, 31, 22, 9, "Caddy (caddy:2-alpine)\nHTTPS: Let's Encrypt on\n13-204-3-14.sslip.io,\nbasic auth", fs=6.0)
    box(ax, 55, 22, 34, 17, "Single image claimsleuth-single (911 MB)\none container, uid 1000,\ndashboard :7860 (compose network only)\n\ndashboard + ADK agents\nMCP server :8102 (127.0.0.1)\nCrewAI A2A service :8103 (127.0.0.1)", fs=6.3)
    cylinder(ax, 55, 8, 34, 11, "Volume runtime/\nblackboard.sqlite3 (WAL), runs, logs", fs=6.2)
    box(ax, 60, 47, 31, 9, "systemd timer (every minute)\nauto_update.sh: fetch, boot.sh,\nhealth check, rollback", fs=6.0)
    box(ax, 60, 64, 34, 6.5, "GitHub: origin/main\nCI on PR and push (compile, bash -n,\nsecret grep, Docker build; no pytest)", fs=6.0)
    arrow(ax, (18, 44), (29, 38), "HTTPS", ty=2)
    arrow(ax, (51, 35), (55, 32), "HTTP")
    arrow(ax, (72, 22), (72, 19), None, both=True)
    arrow(ax, (55, 30), (18, 26), "HTTPS out", ty=2, dashed=True)
    arrow(ax, (86, 64), (86, 56), "pull", tx=3, ty=0)
    arrow(ax, (75, 47), (75, 44), None)
    ax.text(1, 8, "One image: the dashboard starts\nMCP and CrewAI itself. One VM: the\nSQLite blackboard needs all writers\non one machine. Elastic IP: the URL\nstays the same after a stop/start.",
            fontsize=6.0, ha="left", va="center", bbox=dict(fc="white", ec="black", lw=0.6, pad=2))
    return _save(fig, "fig_deployment.png")


def make_all():
    return {
        "arch": fig_architecture(),
        "mcp": fig_mcp(),
        "seq": fig_sequence(),
        "deploy": fig_deployment(),
    }


if __name__ == "__main__":
    for k, v in make_all().items():
        print(k, v)
