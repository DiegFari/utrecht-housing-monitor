"""Create an allowlisted source ZIP without local credentials or diagnostics."""

import argparse
from pathlib import Path
import re
import zipfile

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = (
    "README.md", "SECURITY.md", ".gitignore", ".env.example", "requirements.txt",
    "sites.json", "monitor.py", "housing.py", "fizz.py", "github_state.py",
    ".github/workflows/*.yml", ".github/dependabot.yml", "scripts/*.py",
    "tests/test_*.py", "tests/fixtures/*.html", "tests/fixtures/*.json",
)


def public_files(root=ROOT):
    return sorted({path for pattern in PATTERNS for path in root.glob(pattern)
                   if path.is_file()})


def audit(paths, root=ROOT):
    # Values stay in this process. Findings never print secrets or file contents.
    private = dotenv_values(root / ".env") if (root / ".env").exists() else {}
    needles = [value for key, value in private.items()
               if key in {"SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"}
               and value and "your-address" not in value and "replace-with" not in value]
    passwords = [private.get("SMTP_PASSWORD", "").replace(" ", "")]
    patterns = (r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
                r"\bgh[pousr]_[A-Za-z0-9]{30,}\b",
                r"\bgithub_pat_[A-Za-z0-9_]{30,}\b")
    for path in paths:
        if path.is_symlink():
            raise ValueError("Refusing to publish a symbolic link.")
        relative = path.relative_to(root)
        if relative.name == ".env" or "data" in relative.parts or ".venv" in relative.parts:
            raise ValueError("A private path was selected for publication.")
        text = path.read_text()
        if (any(value in text for value in needles)
                or any(value and len(value) >= 12 and value in text.replace(" ", "")
                       for value in passwords if "replace-with" not in value)
                or any(re.search(pattern, text) for pattern in patterns)):
            raise ValueError("Possible credential found in selected public files; publication stopped.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Audit only; do not create the ZIP.")
    args = parser.parse_args()
    paths = public_files()
    audit(paths)
    if args.check:
        print(f"Public-source audit passed for {len(paths)} allowlisted files.")
        return
    destination = ROOT / "dist" / "utrecht-housing-monitor.zip"
    destination.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, Path("utrecht-housing-monitor") / path.relative_to(ROOT))
    print(f"Created {destination.name}: {len(paths)} source files; no local credentials or diagnostics.")


if __name__ == "__main__":
    main()
