#!/usr/bin/env bash
# Rebuild report + PPT, render to PDF/PNG for visual checks. Usage: bash docs/submission/sync.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TV="${TOOLS_VENV:-/private/tmp/claude-501/-Users-vaibhav-Study-Sem-7-Agentic-AI-Project-ClaimSleuth-simple/25c06bc2-3a5d-4974-8d3d-db532a5a4632/scratchpad/tools-venv}"
OUT="${RENDER_DIR:-$ROOT/docs/submission/render}"
mkdir -p "$OUT"
"$TV/bin/python" "$ROOT/docs/submission/build_report.py"
"$TV/bin/python" "$ROOT/docs/submission/build_ppt.py"
SOFFICE="$(command -v soffice || echo /Applications/LibreOffice.app/Contents/MacOS/soffice)"
"$SOFFICE" --headless --convert-to pdf --outdir "$OUT" "$ROOT/docs/submission/ClaimSleuth_Final_Report.docx" "$ROOT/docs/submission/ClaimSleuth_Final_Presentation.pptx" >/dev/null 2>&1
for f in ClaimSleuth_Final_Report ClaimSleuth_Final_Presentation; do
  pdftoppm -r 60 -png "$OUT/$f.pdf" "$OUT/$f"
done
echo "rendered to $OUT"
