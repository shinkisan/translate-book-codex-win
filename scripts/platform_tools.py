"""Cross-platform discovery for external book-conversion tools."""

import os
import shutil
from pathlib import Path


def _first_existing(candidates):
    for candidate in candidates:
        if not candidate:
            continue
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
        path = Path(candidate).expanduser()
        if path.is_file():
            return str(path)
    return None


def find_calibre_convert():
    """Return the Calibre ``ebook-convert`` executable, if installed."""
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    return _first_existing(
        [
            os.environ.get("CALIBRE_EBOOK_CONVERT"),
            "ebook-convert",
            str(Path(program_files) / "Calibre2" / "ebook-convert.exe"),
            str(Path(program_files_x86) / "Calibre2" / "ebook-convert.exe"),
            "/Applications/calibre.app/Contents/MacOS/ebook-convert",
            "/usr/local/bin/ebook-convert",
            "/usr/bin/ebook-convert",
        ]
    )


def find_pandoc():
    """Return a Pandoc executable from PATH, common installs, or pypandoc."""
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    local_app_data = os.environ.get("LOCALAPPDATA")
    candidates = [
        os.environ.get("PANDOC"),
        "pandoc",
        str(Path(program_files) / "Pandoc" / "pandoc.exe"),
    ]
    if local_app_data:
        candidates.append(str(Path(local_app_data) / "Pandoc" / "pandoc.exe"))

    found = _first_existing(candidates)
    if found:
        return found

    try:
        import pypandoc

        path = pypandoc.get_pandoc_path()
        return path if Path(path).is_file() else None
    except (ImportError, OSError):
        return None
