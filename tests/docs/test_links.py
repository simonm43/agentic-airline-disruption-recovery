"""Every relative link in the repository's documents opens a file that exists."""

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]
LINK = re.compile(r"\]\(<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\)")
# Third-party skill documentation is not ours to keep consistent.
SKIP = (".agents/",)


def _documents() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "*.md"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.splitlines()
    return [path for path in listed if not path.startswith(SKIP) and (ROOT / path).exists()]


def test_every_relative_document_link_resolves() -> None:
    broken: list[str] = []
    for document in _documents():
        text = (ROOT / document).read_text(encoding="utf-8")
        # Fenced code shows commands and examples, not links.
        text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        for match in LINK.finditer(text):
            url = match.group(1)
            if re.match(r"^[a-z][a-z0-9+.-]*:", url) or url.startswith(("#", "/")):
                continue
            path = unquote(url.split("#", 1)[0].split("?", 1)[0])
            if path and not (ROOT / document).parent.joinpath(path).exists():
                broken.append(f"{document}: {url}")
    assert not broken, "Broken links:\n" + "\n".join(broken)
