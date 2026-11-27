import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADR_DIR = ROOT / "docs" / "architecture" / "adr"
REQUIRED_HEADINGS = (
    "## Status",
    "## Context",
    "## Decision",
    "## Alternatives rejected",
    "## Consequences",
    "## Evidence",
)


def test_every_adr_has_the_required_sections_and_is_indexed() -> None:
    adr_files = sorted(ADR_DIR.glob("[0-9][0-9][0-9][0-9]-*.md"))
    assert len(adr_files) >= 10, "the decision record lost its ADRs"
    numbers = [path.name[:4] for path in adr_files]
    assert len(numbers) == len(set(numbers)), "an ADR number is used twice"

    index = (ADR_DIR / "README.md").read_text(encoding="utf-8")
    for path in adr_files:
        number = path.name[:4]
        assert re.fullmatch(rf"{number}-[a-z0-9]+(?:-[a-z0-9]+)*\.md", path.name), path.name
        text = path.read_text(encoding="utf-8")
        assert re.search(rf"^# ADR {number}: \S", text, re.MULTILINE), f"{path.name} title"
        for heading in REQUIRED_HEADINGS:
            assert re.search(rf"^{re.escape(heading)}\s*$", text, re.MULTILINE), (
                f"{path.name} lacks {heading}"
            )
        assert f"]({path.name})" in index, f"README.md does not link {path.name}"
