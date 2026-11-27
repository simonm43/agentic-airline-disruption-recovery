"""Print a hash-pinned lock for requirements.in; runs inside the target Linux image."""

import json
import subprocess
import sys

subprocess.run(
    [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--dry-run",
        "--ignore-installed",
        "--quiet",
        "--disable-pip-version-check",
        "--only-binary=:all:",
        "--report",
        "/tmp/report.json",
        "-r",
        "requirements.in",
    ],
    check=True,
)
with open("/tmp/report.json", encoding="utf-8") as handle:
    report = json.load(handle)
print("----LOCK-BEGIN----")
for item in sorted(report["install"], key=lambda i: i["metadata"]["name"].lower()):
    digest = item["download_info"]["archive_info"]["hashes"]["sha256"]
    print(
        f"{item['metadata']['name'].lower()}=={item['metadata']['version']} --hash=sha256:{digest}"
    )
print("----LOCK-END----")
