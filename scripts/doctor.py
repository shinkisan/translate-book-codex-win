#!/usr/bin/env python3
"""Check runtime dependencies and print actionable Windows diagnostics."""

import importlib.util
import platform
import sys

from platform_tools import find_calibre_convert, find_pandoc


def main():
    print(f"Python: {sys.executable} ({platform.python_version()})")

    checks = [
        ("Calibre ebook-convert", find_calibre_convert(), True),
        ("Pandoc", find_pandoc(), True),
        ("pypandoc", importlib.util.find_spec("pypandoc") is not None, True),
        ("beautifulsoup4", importlib.util.find_spec("bs4") is not None, False),
        ("Markdown", importlib.util.find_spec("markdown") is not None, False),
    ]

    missing_required = False
    for name, result, required in checks:
        status = str(result) if result else "NOT FOUND"
        suffix = " (required)" if required else " (optional fallback)"
        print(f"{name}: {status}{suffix}")
        missing_required |= required and not bool(result)

    if missing_required:
        print("\nInstall Python packages with: python -m pip install -r requirements.txt")
        print("Install Calibre: winget install --id calibre.calibre --exact --accept-source-agreements --accept-package-agreements")
        print("Install Pandoc: winget install --id JohnMacFarlane.Pandoc --exact --accept-source-agreements --accept-package-agreements")
        print("Alternatively, set CALIBRE_EBOOK_CONVERT and PANDOC to their executable paths.")
        return 1

    print("\nRuntime is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
