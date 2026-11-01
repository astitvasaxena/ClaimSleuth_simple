"""Parsing of the claim packet (see claim_text.py) as it appears inside an LLM prompt.

Used only by the offline TEST DOUBLES (ADK and CrewAI) to read the same packet a real model would read.
Plain Python, no framework imports, so both virtual environments can use it.
"""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

_DOC_LINE = re.compile(r"^(?P<doc>[A-Za-z0-9_.-]+)#L(?P<line>\d+): (?P<text>.*)$", re.M)
_LABEL = re.compile(r"^\s*([A-Za-z][A-Za-z ]{1,40}?)\s*:\s*(.+?)\s*$")
_FORM = re.compile(r'<untrusted source="claim_form">\n(.*?)\n</untrusted>', re.S)
_KINDS = re.compile(r"([A-Za-z0-9_.-]+) \((INVOICE|DIAGNOSTIC|OTHER)\)")
LABELS = {"serial number": "serial", "serial no": "serial", "purchase date": "purchase_date",
          "date of inspection": "inspection_date", "inspection date": "inspection_date",
          "customer": "name", "client": "name", "diagnosis": "diagnosis", "technician findings": "diagnosis",
          "estimated cost": "repair_cost", "repair cost": "repair_cost", "policy number": "policy_id"}


def transcript(messages: Any) -> list[dict[str, str]]:
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]
    return [{"role": m.get("role", ""), "content": str(m.get("content") or "")} for m in messages]


def json_objects(text: str) -> list[dict]:
    decoder, found, i = json.JSONDecoder(), [], 0
    while (i := text.find("{", i)) != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
            if isinstance(obj, dict):
                found.append(obj)
            i = end
        except json.JSONDecodeError:
            i += 1
    return found


def iso_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value.strip()) if value else None
    except ValueError:
        return None


class Packet:
    """The claim packet as it appears in the prompt (claim form + numbered document lines)."""

    def __init__(self, text: str) -> None:
        form = _FORM.search(text)
        self.form: dict = json.loads(form.group(1)) if form else {}
        self.kinds = {doc: kind for doc, kind in _KINDS.findall(text)}
        self.lines: dict[str, dict[int, str]] = {}
        for m in _DOC_LINE.finditer(text):
            self.lines.setdefault(m["doc"], {})[int(m["line"])] = m["text"]
        self.fields: dict[str, dict[str, tuple[str, int, str]]] = {}
        for doc, lines in self.lines.items():
            for n, line in sorted(lines.items()):
                lm = _LABEL.match(line)
                name = LABELS.get(lm.group(1).strip().lower()) if lm else None
                if name and name not in self.fields.setdefault(doc, {}):
                    self.fields[doc][name] = (lm.group(2), n, line.strip())

    def doc_of(self, kind: str) -> str | None:
        return next((d for d, k in self.kinds.items() if k == kind), None)

    def get(self, doc: str | None, name: str) -> tuple[str, int, str] | None:
        return self.fields.get(doc or "", {}).get(name)


def cite(doc: str, field: tuple[str, int, str]) -> dict:
    return {"doc_id": doc, "line": field[1], "quote": field[2]}
