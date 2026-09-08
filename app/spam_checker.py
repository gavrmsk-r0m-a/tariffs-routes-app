"""Pure parser and reputation rules for the manual Telegram SPAM workflow."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

PARSER_VERSION = "1"
PHONE_RE = re.compile(r"^[1-9][0-9]{6,20}$")
PREFIX_RE = re.compile(r"^\s*\[[^]]+\]\s*[^:]+:\s*", re.IGNORECASE)


@dataclass
class ParsedResult:
    number: str
    verdict: str
    sources: list[str] = field(default_factory=list)
    status: str = "ready"
    duplicate: bool = False

    @property
    def source(self) -> str | None:
        if not self.sources:
            return None
        strong = next((value for value in self.sources if value.casefold() != "hiya"), None)
        return strong or self.sources[0]


def score_change(score: int, verdict: str, sources: list[str] | None = None) -> tuple[int, int, str]:
    """Return (effective delta, new score, severity), capped to 0..5."""
    before = max(0, min(5, int(score)))
    if verdict == "clear":
        after, severity = max(0, before - 1), "clear"
    else:
        strong = any(source.casefold() != "hiya" for source in (sources or []))
        after, severity = min(5, before + (2 if strong else 1)), "strong" if strong else "soft"
    return after - before, after, severity


def parse_response(raw: str, expected_numbers: list[str]) -> dict:
    """Parse Telegram text without side effects and reconcile it with expected numbers."""
    expected = list(dict.fromkeys(str(n).strip() for n in expected_numbers if str(n).strip()))
    found: dict[str, dict[str, object]] = {}
    issues: list[str] = []
    section: str | None = None
    for line_no, original in enumerate((raw or "").splitlines(), 1):
        line = PREFIX_RE.sub("", original.strip()).strip()
        if not line or "ваш запрос обрабатывается" in line.casefold():
            continue
        if line.casefold() in {"spam", "clear"}:
            section = line.casefold()
            continue
        if section == "spam":
            match = re.match(r"^([1-9][0-9]{6,20})\s*-\s*(.+?)\s*$", line)
            if not match:
                issues.append(f"Строка {line_no}: ожидается NUMBER - SOURCE")
                continue
            number, source = match.group(1), match.group(2).strip()
            item = found.setdefault(number, {"spam": [], "clear": False, "count": 0})
            item["spam"].append(source)
            item["count"] += 1
        elif section == "clear" and PHONE_RE.fullmatch(line):
            item = found.setdefault(line, {"spam": [], "clear": False, "count": 0})
            item["clear"] = True
            item["count"] += 1
        else:
            issues.append(f"Строка {line_no}: не удалось распознать результат")

    rows: list[ParsedResult] = []
    expected_set = set(expected)
    for number in expected:
        item = found.get(number)
        if not item:
            rows.append(ParsedResult(number, "missing", status="missing"))
        elif item["spam"] and item["clear"]:
            rows.append(ParsedResult(number, "conflict", list(dict.fromkeys(item["spam"])), "error", item["count"] > 1))
        elif item["spam"]:
            rows.append(ParsedResult(number, "spam", list(dict.fromkeys(item["spam"])), "ready", item["count"] > 1))
        else:
            rows.append(ParsedResult(number, "clear", status="ready", duplicate=item["count"] > 1))
    for number, item in found.items():
        if number not in expected_set:
            verdict = "conflict" if item["spam"] and item["clear"] else ("spam" if item["spam"] else "clear")
            rows.append(ParsedResult(number, verdict, list(dict.fromkeys(item["spam"])), "extra", item["count"] > 1))
    summary = {
        "requested": len(expected), "received": sum(r.status in {"ready", "error"} for r in rows),
        "spam": sum(r.verdict == "spam" and r.status == "ready" for r in rows),
        "clear": sum(r.verdict == "clear" and r.status == "ready" for r in rows),
        "missing": sum(r.status == "missing" for r in rows), "extra": sum(r.status == "extra" for r in rows),
        "errors": sum(r.status == "error" for r in rows) + len(issues),
    }
    return {"rows": rows, "summary": summary, "issues": issues}
