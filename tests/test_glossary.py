import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import glossary  # noqa: E402


@contextmanager
def temp_book_dir(chunks=None, glossary_data=None):
    """Spin up a temp directory laid out like a real <book>_temp/."""
    with tempfile.TemporaryDirectory() as tmp:
        if chunks:
            for name, body in chunks.items():
                with open(os.path.join(tmp, name), 'w', encoding='utf-8') as f:
                    f.write(body)
        glossary_path = os.path.join(tmp, 'glossary.json')
        if glossary_data is not None:
            with open(glossary_path, 'w', encoding='utf-8') as f:
                json.dump(glossary_data, f, ensure_ascii=False, indent=2)
        yield tmp, glossary_path


def make_glossary(*pairs, top_n=20):
    """Build a minimal valid glossary from (source, target[, category]) tuples."""
    terms = []
    for p in pairs:
        if len(p) == 2:
            source, target = p
            category = ''
        else:
            source, target, category = p
        terms.append({
            'source': source,
            'target': target,
            'category': category,
            'frequency': 0,
        })
    return {
        'version': glossary.GLOSSARY_SCHEMA_VERSION,
        'terms': terms,
        'high_frequency_top_n': top_n,
    }


class CountFrequenciesTests(unittest.TestCase):
    def test_counts_frequencies_across_multiple_chunks(self):
        chunks = {
            'chunk0001.md': "Manhattan is busy. Manhattan in spring.",
            'chunk0002.md': "Brooklyn nights. Manhattan again.",
            'chunk0003.md': "Just Brooklyn here.",
        }
        g = make_glossary(('Manhattan', '曼哈顿', 'place'), ('Brooklyn', '布鲁克林', 'place'))

        with temp_book_dir(chunks=chunks, glossary_data=g) as (tmp, gpath):
            glossary.count_frequencies(gpath, tmp)
            updated = glossary.load_glossary(gpath)

        by_source = {t['source']: t['frequency'] for t in updated['terms']}
        self.assertEqual(by_source['Manhattan'], 3)
        self.assertEqual(by_source['Brooklyn'], 2)

    def test_term_not_present_yields_zero(self):
        chunks = {'chunk0001.md': "Nothing relevant here."}
        g = make_glossary(('Manhattan', '曼哈顿'))

        with temp_book_dir(chunks=chunks, glossary_data=g) as (tmp, gpath):
            glossary.count_frequencies(gpath, tmp)
            updated = glossary.load_glossary(gpath)

        self.assertEqual(updated['terms'][0]['frequency'], 0)

    def test_handles_special_regex_characters_in_source(self):
        chunks = {
            'chunk0001.md': "Built in C++. Loves .NET. Knows O(n) algorithms.",
            'chunk0002.md': "More C++ code. Another O(n) call. Another .NET service.",
        }
        g = make_glossary(('C++', 'C加加'), ('.NET', '.NET框架'), ('O(n)', '线性'))

        with temp_book_dir(chunks=chunks, glossary_data=g) as (tmp, gpath):
            glossary.count_frequencies(gpath, tmp)
            updated = glossary.load_glossary(gpath)

        by_source = {t['source']: t['frequency'] for t in updated['terms']}
        self.assertEqual(by_source['C++'], 2)
        self.assertEqual(by_source['.NET'], 2)
        self.assertEqual(by_source['O(n)'], 2)

    def test_word_boundary_avoids_false_positives(self):
        # "cat" should not match inside "category" or "concatenate".
        chunks = {'chunk0001.md': "category concatenate cat caterwaul cat."}
        g = make_glossary(('cat', '猫'))

        with temp_book_dir(chunks=chunks, glossary_data=g) as (tmp, gpath):
            glossary.count_frequencies(gpath, tmp)
            updated = glossary.load_glossary(gpath)

        # Two real "cat" tokens; "category", "concatenate", "caterwaul" excluded.
        self.assertEqual(updated['terms'][0]['frequency'], 2)

    def test_handles_cjk_source_terms(self):
        chunks = {
            'chunk0001.md': "他在曼哈顿散步。曼哈顿很热闹。",
            'chunk0002.md': "曼哈顿的夜晚。",
        }
        g = make_glossary(('曼哈顿', 'Manhattan'))

        with temp_book_dir(chunks=chunks, glossary_data=g) as (tmp, gpath):
            glossary.count_frequencies(gpath, tmp)
            updated = glossary.load_glossary(gpath)

        self.assertEqual(updated['terms'][0]['frequency'], 3)

    def test_excludes_output_chunks_from_count(self):
        # Regression-critical: translated outputs sit alongside source chunks
        # but must not contribute to source-side frequency.
        chunks = {
            'chunk0001.md': "Manhattan once.",
            'output_chunk0001.md': "曼哈顿 mentioned, with Manhattan stuck inside as residue.",
            'chunk0002.md': "Manhattan twice.",
            'output_chunk0002.md': "Manhattan Manhattan Manhattan inflate me!",
        }
        g = make_glossary(('Manhattan', '曼哈顿'))

        with temp_book_dir(chunks=chunks, glossary_data=g) as (tmp, gpath):
            glossary.count_frequencies(gpath, tmp)
            updated = glossary.load_glossary(gpath)

        # Only chunk0001+chunk0002 should count: 2 occurrences total.
        self.assertEqual(updated['terms'][0]['frequency'], 2)

    def test_rejects_single_cjk_char_term_with_warning(self):
        chunks = {'chunk0001.md': "他他他他他"}
        g = make_glossary(('他', 'he'))

        captured = StringIO()
        with temp_book_dir(chunks=chunks, glossary_data=g) as (tmp, gpath):
            with mock.patch.object(sys, 'stderr', captured):
                glossary.count_frequencies(gpath, tmp)
            updated = glossary.load_glossary(gpath)

        self.assertEqual(updated['terms'][0]['frequency'], 0)
        self.assertIn("single-character CJK", captured.getvalue())


class LoadGlossaryTests(unittest.TestCase):
    def test_missing_glossary_raises_filenotfound(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                glossary.load_glossary(os.path.join(tmp, 'glossary.json'))

    def test_malformed_json_raises_actionable_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'glossary.json')
            with open(path, 'w', encoding='utf-8') as f:
                f.write("{not valid json")
            with self.assertRaises(ValueError) as ctx:
                glossary.load_glossary(path)
            self.assertIn("not valid JSON", str(ctx.exception))
            self.assertIn(path, str(ctx.exception))

    def test_missing_terms_key_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'glossary.json')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({'version': 1}, f)
            with self.assertRaises(ValueError) as ctx:
                glossary.load_glossary(path)
            self.assertIn("'terms'", str(ctx.exception))

    def test_version_mismatch_raises_actionable_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'glossary.json')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({'version': 99, 'terms': []}, f)
            with self.assertRaises(ValueError) as ctx:
                glossary.load_glossary(path)
            self.assertIn("schema version mismatch", str(ctx.exception))


class SelectTermsForChunkTests(unittest.TestCase):
    def test_unions_local_and_top_n(self):
        g = make_glossary(
            ('Manhattan', '曼哈顿'),
            ('Brooklyn', '布鲁克林'),
            ('Queens', '皇后区'),
            ('Bronx', '布朗克斯'),
            top_n=2,
        )
        # Frequencies — Manhattan and Brooklyn most frequent.
        for term in g['terms']:
            term['frequency'] = {'Manhattan': 100, 'Brooklyn': 80, 'Queens': 5, 'Bronx': 1}[term['source']]

        # Chunk only mentions Bronx — local hit. Top-2 are Manhattan + Brooklyn.
        chunk_text = "We went to the Bronx today."
        selected = glossary.select_terms_for_chunk(g, chunk_text)

        sources = sorted(t['source'] for t in selected)
        self.assertEqual(sources, ['Bronx', 'Brooklyn', 'Manhattan'])

    def test_respects_max_terms_cap(self):
        g = make_glossary(*[(f"Term{i:03d}", f"译{i:03d}") for i in range(100)], top_n=100)
        for i, term in enumerate(g['terms']):
            term['frequency'] = 1000 - i
        chunk_text = "no local hits here"
        selected = glossary.select_terms_for_chunk(g, chunk_text, max_terms=5)
        self.assertEqual(len(selected), 5)

    def test_sorted_by_frequency_desc(self):
        g = make_glossary(('A', 'a'), ('B', 'b'), ('C', 'c'), top_n=3)
        for term, freq in zip(g['terms'], [1, 100, 50]):
            term['frequency'] = freq
        chunk_text = "no hits"
        selected = glossary.select_terms_for_chunk(g, chunk_text)
        self.assertEqual([t['source'] for t in selected], ['B', 'C', 'A'])


class HashTests(unittest.TestCase):
    def test_glossary_hash_stable_across_key_order(self):
        g1 = {
            'version': 1,
            'terms': [{'source': 'A', 'target': 'a', 'category': '', 'frequency': 1}],
            'high_frequency_top_n': 20,
        }
        g2 = {
            'high_frequency_top_n': 20,
            'terms': [{'frequency': 1, 'category': '', 'target': 'a', 'source': 'A'}],
            'version': 1,
        }
        self.assertEqual(glossary.glossary_hash(g1), glossary.glossary_hash(g2))

    def test_glossary_hash_changes_when_target_changes(self):
        g1 = make_glossary(('Manhattan', '曼哈顿'))
        g2 = make_glossary(('Manhattan', '曼哈顿区'))
        self.assertNotEqual(glossary.glossary_hash(g1), glossary.glossary_hash(g2))

    def test_term_hash_changes_when_target_changes(self):
        t1 = {'source': 'Manhattan', 'target': '曼哈顿', 'category': 'place'}
        t2 = {'source': 'Manhattan', 'target': '曼哈顿区', 'category': 'place'}
        self.assertNotEqual(glossary.term_hash(t1), glossary.term_hash(t2))

    def test_term_hash_changes_when_category_changes(self):
        t1 = {'source': 'Apple', 'target': '苹果', 'category': 'fruit'}
        t2 = {'source': 'Apple', 'target': '苹果', 'category': 'company'}
        self.assertNotEqual(glossary.term_hash(t1), glossary.term_hash(t2))


class FormatTermsForPromptTests(unittest.TestCase):
    def test_empty_terms_returns_empty_string(self):
        self.assertEqual(glossary.format_terms_for_prompt([]), '')

    def test_renders_two_col_table(self):
        terms = [{'source': 'Manhattan', 'target': '曼哈顿'}]
        out = glossary.format_terms_for_prompt(terms)
        self.assertIn('| 原文 | 译文 |', out)
        self.assertIn('| Manhattan | 曼哈顿 |', out)

    def test_escapes_pipes_in_term_text(self):
        terms = [{'source': 'A|B', 'target': 'X|Y'}]
        out = glossary.format_terms_for_prompt(terms)
        self.assertIn(r'A\|B', out)
        self.assertIn(r'X\|Y', out)


class SaveGlossaryAtomicTests(unittest.TestCase):
    def test_atomic_write_survives_simulated_interrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'glossary.json')
            original = make_glossary(('Manhattan', '曼哈顿'))
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(original, f)

            # Patch json.dump to fail mid-write.
            doomed = make_glossary(('Manhattan', 'BROKEN'))
            with mock.patch('glossary.json.dump', side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    glossary.save_glossary(path, doomed)

            # Original file must be untouched.
            with open(path, 'r', encoding='utf-8') as f:
                still_there = json.load(f)
            self.assertEqual(still_there['terms'][0]['target'], '曼哈顿')

            # And no leftover .glossary-*.tmp in the dir.
            leftovers = [f for f in os.listdir(tmp) if f.startswith('.glossary-')]
            self.assertEqual(leftovers, [])

    def test_save_then_load_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'glossary.json')
            g = make_glossary(('Manhattan', '曼哈顿', 'place'), ('Brooklyn', '布鲁克林', 'place'))
            glossary.save_glossary(path, g)
            loaded = glossary.load_glossary(path)
            self.assertEqual(loaded, g)


if __name__ == '__main__':
    unittest.main()
