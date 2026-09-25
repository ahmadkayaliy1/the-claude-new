"""Tiny helper that builds a Markdown report (docs/exploration/) + raw JSON (data/research/probes/)."""
from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any, Iterable

from tradingsystem.core.settings import PROJECT_ROOT
from tradingsystem.core.timeutil import iso, now_ms

DOCS = PROJECT_ROOT / "docs" / "exploration"
RAW = PROJECT_ROOT / "data" / "research" / "probes"


class Report:
    def __init__(self, name: str, title: str) -> None:
        self.name = name
        self.lines: list[str] = [f"# {title}", "", f"_Generated {iso(now_ms())} by `research/probes/{name}.py` "
                                 "from live/real data. Re-run to refresh._", ""]
        self.raw: dict[str, Any] = {}

    def h(self, text: str, level: int = 2) -> None:
        self.lines += ["", "#" * level + " " + text, ""]

    def p(self, text: str) -> None:
        self.lines += [text, ""]

    def bullet(self, text: str) -> None:
        self.lines.append(f"- {text}")

    def table(self, headers: list[str], rows: Iterable[Iterable[Any]]) -> None:
        self.lines.append("| " + " | ".join(headers) + " |")
        self.lines.append("|" + "---|" * len(headers))
        for r in rows:
            self.lines.append("| " + " | ".join(_fmt(c) for c in r) + " |")
        self.lines.append("")

    def code(self, text: str, lang: str = "") -> None:
        self.lines += [f"```{lang}", text.rstrip(), "```", ""]

    def save(self) -> Path:
        DOCS.mkdir(parents=True, exist_ok=True)
        RAW.mkdir(parents=True, exist_ok=True)
        path = DOCS / f"{self.name.removeprefix('probe_')}.md"
        path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        (RAW / f"{self.name}.json").write_text(json.dumps(self.raw, indent=2, default=str), encoding="utf-8")
        print(f"report written: {path}")
        return path


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:,.6g}" if abs(v) < 1e6 else f"{v:,.0f}"
    return str(v).replace("|", "\\|")


def pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = min(len(s) - 1, max(0, int(round(q / 100 * (len(s) - 1)))))
    return s[k]


def summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    return {"n": len(values), "min": min(values), "p50": pct(values, 50), "p90": pct(values, 90),
            "p99": pct(values, 99), "max": max(values), "mean": statistics.fmean(values)}


class Timer:
    def __enter__(self) -> "Timer":
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.ms = (time.perf_counter() - self.t0) * 1000
