#!/usr/bin/env python3
"""
glossary.py - Term-consistency glossary for parallel-subagent translation.

A separate sub-agent translates each chunk with a fresh context. Without
shared state, the same proper noun can get translated three different ways
across a 100-chunk book. This module manages a hand-editable glossary.json
that the main agent injects into each sub-agent's prompt as a per-chunk term
table — so every sub-agent sees the same canonical translations for the
terms that matter to its chunk.

The schema:

    {
      "version": 1,
      "terms": [
        {"source": "Manhattan", "target": "曼哈顿", "category": "place", "frequency": 12}
      ],
      "high_frequency_top_n": 20
    }

Lives at <temp_dir>/glossary.json and is meant to be hand-edited.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path


GLOSSARY_SCHEMA_VERSION = 1
DEFAULT_TOP_N = 20
DEFAULT_MAX_TERMS = 50

_CJK_RANGES = (
    (0x3400, 0x4DBF),   # CJK Extension A
    (0x4E00, 0x9FFF),   # CJK Unified Ideographs
    (0x3040, 0x309F),   # Hiragana
    (0x30A0, 0x30FF),   # Katakana
    (0xAC00, 0xD7AF),   # Hangul Syllables
)


def _contains_cjk(s):
    for c in s:
        cp = ord(c)
        for lo, hi in _CJK_RANGES:
            if lo <= cp <= hi:
                return True
    return False


def _canonical_json(data):
    """Stable JSON for hashing — sorted keys, no whitespace, unicode preserved."""
    return json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def glossary_hash(glossary):
    """SHA-256 of the canonical glossary. Input order / whitespace insensitive.

    Used by the run-state layer to detect glossary edits and trigger precise
    re-translation of affected chunks.
    """
    return hashlib.sha256(_canonical_json(glossary).encode('utf-8')).hexdigest()


def term_hash(term):
    """SHA-256 of a single term's identifying fields.

    Lets the run-state layer attribute a chunk's translation to specific term
    versions, so editing one term only invalidates the chunks that used it.
    """
    payload = f"{term.get('source', '')}→{term.get('target', '')}|{term.get('category', '')}"
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def load_glossary(path):
    """Load and validate a glossary file. Raises actionable errors on bad input."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Glossary not found: {path}")

    with open(path, 'r', encoding='utf-8') as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Glossary at {path} is not valid JSON: {e}") from e

    if not isinstance(data, dict):
        raise ValueError(f"Glossary at {path} must be a JSON object, got {type(data).__name__}")

    version = data.get('version')
    if version != GLOSSARY_SCHEMA_VERSION:
        raise ValueError(
            f"Glossary schema version mismatch in {path}: "
            f"expected {GLOSSARY_SCHEMA_VERSION}, got {version!r}. "
            f"Delete the file to rebuild, or migrate it by hand."
        )

    terms = data.get('terms')
    if not isinstance(terms, list):
        raise ValueError(f"Glossary at {path} must have a 'terms' array")

    for i, t in enumerate(terms):
        if not isinstance(t, dict):
            raise ValueError(f"Glossary term #{i} in {path} must be an object")
        for required in ('source', 'target'):
            if required not in t:
                raise ValueError(
                    f"Glossary term #{i} in {path} missing required field '{required}'"
                )

    return data


def save_glossary(path, glossary):
    """Atomically write the glossary. Tempfile in same dir, then os.replace."""
    dirname = os.path.dirname(os.path.abspath(path)) or '.'
    fd, tmp_path = tempfile.mkstemp(dir=dirname, prefix='.glossary-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(glossary, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write('\n')
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def _count_in_text(source, text):
    """Count occurrences of source in text. CJK uses substring; ASCII uses
    boundary-aware regex so 'C++' and '.NET' work and 'cat' doesn't match
    'category'."""
    if not source:
        return 0
    if _contains_cjk(source):
        return text.count(source)
    if source.isascii():
        escaped = re.escape(source)
        pattern = rf'(?<!\w){escaped}(?!\w)'
        return len(re.findall(pattern, text))
    return text.count(source)


def count_frequencies(glossary_path, chunks_dir):
    """Scan all source chunks and write per-term frequency back into the glossary.

    Source-chunk discovery excludes `output_chunk*.md` files — counting
    translated text would inflate frequencies on re-runs.
    """
    glossary = load_glossary(glossary_path)
    chunks_dir_path = Path(chunks_dir)

    chunk_paths = sorted(
        p for p in chunks_dir_path.glob('chunk*.md')
        if not p.name.startswith('output_')
    )

    if not chunk_paths:
        print(f"Warning: no chunk*.md files found under {chunks_dir}", file=sys.stderr)

    all_text_parts = []
    for p in chunk_paths:
        try:
            all_text_parts.append(p.read_text(encoding='utf-8'))
        except OSError as e:
            print(f"Warning: could not read {p}: {e}", file=sys.stderr)
    all_text = '\n'.join(all_text_parts)

    for term in glossary['terms']:
        source = term.get('source', '')
        if _contains_cjk(source) and len(source) <= 1:
            print(
                f"Warning: skipping frequency count for single-character CJK term {source!r} "
                f"(would over-match as substring)",
                file=sys.stderr,
            )
            term['frequency'] = 0
            continue
        term['frequency'] = _count_in_text(source, all_text)

    save_glossary(glossary_path, glossary)


def select_terms_for_chunk(glossary, chunk_text, top_n=None, max_terms=DEFAULT_MAX_TERMS):
    """Return terms relevant to a single chunk: union of (terms appearing in
    chunk_text) and (top-N most-frequent terms across the whole book).

    Sorted by frequency desc, source asc as tie-breaker. Capped at max_terms.
    """
    if top_n is None:
        top_n = glossary.get('high_frequency_top_n', DEFAULT_TOP_N)

    terms = glossary.get('terms', [])
    if not terms:
        return []

    local_indices = set()
    for i, t in enumerate(terms):
        source = t.get('source', '')
        if source and source in chunk_text:
            local_indices.add(i)

    by_freq = sorted(
        range(len(terms)),
        key=lambda i: (-terms[i].get('frequency', 0), terms[i].get('source', '')),
    )
    top_indices = set(by_freq[:max(0, top_n)])

    selected = [terms[i] for i in (local_indices | top_indices)]
    selected.sort(key=lambda t: (-t.get('frequency', 0), t.get('source', '')))
    return selected[:max_terms]


def format_terms_for_prompt(terms):
    """Render a compact 2-col markdown table. Empty input yields empty string
    so the caller can omit the rule line entirely."""
    if not terms:
        return ''
    rows = ['| 原文 | 译文 |', '|------|------|']
    for t in terms:
        source = t.get('source', '').replace('|', '\\|')
        target = t.get('target', '').replace('|', '\\|')
        rows.append(f"| {source} | {target} |")
    return '\n'.join(rows)


def main():
    parser = argparse.ArgumentParser(description="Glossary management for translate-book")
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_count = sub.add_parser('count-frequencies', help="Update frequencies in glossary.json")
    p_count.add_argument('temp_dir', help="Path to <book>_temp/ directory")

    p_print = sub.add_parser('print-terms-for-chunk', help="Print markdown table of terms for a chunk")
    p_print.add_argument('temp_dir')
    p_print.add_argument('chunk_filename', help="e.g. chunk0001.md")
    p_print.add_argument('--top-n', type=int, default=None,
                         help="Override high_frequency_top_n from glossary.json")
    p_print.add_argument('--max-terms', type=int, default=DEFAULT_MAX_TERMS,
                         help=f"Cap on terms in the table (default: {DEFAULT_MAX_TERMS})")

    p_hash = sub.add_parser('compute-hash', help="Print glossary_hash to stdout")
    p_hash.add_argument('temp_dir')

    args = parser.parse_args()
    glossary_path = os.path.join(args.temp_dir, 'glossary.json')

    if args.cmd == 'count-frequencies':
        count_frequencies(glossary_path, args.temp_dir)
    elif args.cmd == 'print-terms-for-chunk':
        glossary = load_glossary(glossary_path)
        chunk_path = os.path.join(args.temp_dir, args.chunk_filename)
        with open(chunk_path, 'r', encoding='utf-8') as f:
            chunk_text = f.read()
        terms = select_terms_for_chunk(
            glossary, chunk_text, top_n=args.top_n, max_terms=args.max_terms
        )
        table = format_terms_for_prompt(terms)
        if table:
            print(table)
    elif args.cmd == 'compute-hash':
        glossary = load_glossary(glossary_path)
        print(glossary_hash(glossary))


if __name__ == '__main__':
    main()
