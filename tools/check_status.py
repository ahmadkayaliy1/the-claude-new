"""Validate PROJECT_STATUS.md: every phase must carry the 7 fields required by spec §10.1."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = [
    "Status:", "Description:", "Affected files:", "What was done:", "Why this way:",
    "Notes/open issues:", "The exact next step:",
]
PHASE_RE = re.compile(r"^### Phase (P\d+\.\d+):", re.M)
STATUS_RE = re.compile(r"Status:\s*(✅|🔄|⏳|⚠️)")


def check(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    errors: list[str] = []
    for section in ("## Overview", "## Log of Major Design Decisions", "## Phases"):
        if section not in text:
            errors.append(f"missing section {section!r}")
    matches = list(PHASE_RE.finditer(text))
    if not matches:
        errors.append("no phases found")
    seen: set[str] = set()
    in_progress = 0
    for i, m in enumerate(matches):
        pid = m.group(1)
        if pid in seen:
            errors.append(f"duplicate phase id {pid}")
        seen.add(pid)
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[m.start():end]
        for field in REQUIRED:
            if field not in block:
                errors.append(f"{pid}: missing field {field!r}")
        st = STATUS_RE.search(block)
        if not st:
            errors.append(f"{pid}: status must be one of ✅ 🔄 ⏳ ⚠️")
        elif st.group(1) == "🔄":
            in_progress += 1
    print(f"{len(matches)} phases checked; {in_progress} in progress")
    return errors


if __name__ == "__main__":
    errs = check(ROOT / "PROJECT_STATUS.md")
    for e in errs:
        print("ERROR:", e)
    print("OK" if not errs else f"{len(errs)} error(s)")
    sys.exit(1 if errs else 0)
