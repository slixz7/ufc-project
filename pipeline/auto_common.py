"""Shared by every part of the daily job: the logger, the project root, the User-Agent text, and the Report that is printed and written to the GitHub summary."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


log = logging.getLogger("auto_update")
ROOT = Path(__file__).resolve().parents[1]
UA = "ufc-fight-predictor/1.0 (https://github.com/slixz7/ufc-project; personal portfolio project)"


# ----------------------------------------------------------------------------------------------
# report (printed, and written to the GitHub job summary)
# ----------------------------------------------------------------------------------------------
@dataclass
class Report:
    sections: list = field(default_factory=list)
    attention: list = field(default_factory=list)
    failed: list = field(default_factory=list)

    def add(self, title: str, *lines: str):
        self.sections.append((title, [l for l in lines if l]))

    def need(self, text: str):
        self.attention.append(text)

    def render(self, today: date, dry_run: bool) -> str:
        out = [f"# UFC auto-update, {today.isoformat()}" + ("  (DRY RUN: nothing was written)" if dry_run else ""), ""]
        out.append("## Needs your attention")
        out += [f"- {a}" for a in self.attention] if self.attention else ["Nothing."]
        for title, lines in self.sections:
            out += ["", f"## {title}"] + [f"- {l}" for l in lines]
        return "\n".join(out) + "\n"
