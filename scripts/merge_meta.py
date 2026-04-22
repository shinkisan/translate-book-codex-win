#!/usr/bin/env python3
"""
merge_meta.py - Merge sub-agent observation files into the canonical glossary.

Three subcommands:

- `prepare-merge <temp_dir>` — scan all `output_chunk*.meta.json` files in
  the temp dir, filter to those whose content hash isn't already in
  `glossary["applied_meta_hashes"]`, classify findings against current
  glossary state, and emit a JSON proposal to stdout. Malformed meta files
  are quarantined (warn + skip + count separately) — they don't crash the
  step.

- `apply-merge <temp_dir>` — read a decisions JSON from stdin, conservatively
  apply the auto-applies and resolved decisions to the glossary, and write
  meta content hashes into `applied_meta_hashes` for every chunk in
  `consumed_chunk_ids`. Atomic save.

- `status <temp_dir>` — read-only observability snapshot for the final
  verification report.

Approach A: scripts NEVER invoke an LLM. All semantic decisions (alias yes/no,
conflict resolution, conflicting-proposal pick) are made by the main agent in
SKILL.md Step 4.5; this script just shapes data and applies pre-decided
choices.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import glossary as glossary_mod
import meta as meta_mod


META_GLOB = 'output_chunk*.meta.json'
TRANSLATED_GLOB = 'output_chunk*.md'
EVIDENCE_REFS_CAP = 5

# Decision kinds and their valid choice sets.
VALID_CHOICES_BY_KIND = {
    'alias': frozenset({'yes_alias', 'no_separate_entity', 'skip'}),
    'conflict': frozenset({'keep_current', 'accept_proposed', 'record_in_notes'}),
    'new_entity_existing_alias': frozenset(
        {'promote_to_separate_entity', 'keep_as_alias', 'skip'}
    ),
    # alias_or_new_entity and conflicting_new_entity_proposals have dynamic
    # use_standalone_N / use_variant_N choices generated from variant counts;
    # validated at apply time against the decision item's options array.
}

VALID_GENDER_VALUES = ('male', 'female', 'nonbinary', 'unknown')


def _glossary_path(temp_dir):
    return os.path.join(temp_dir, 'glossary.json')


def _meta_path(temp_dir, chunk_id):
    return os.path.join(temp_dir, f'output_{chunk_id}.meta.json')


def _try_load_meta(path):
    """Wrap meta.load_meta for graceful degradation. Returns (data, error_msg).

    Honors the "meta is non-blocking" promise: a malformed meta file is
    skipped, not raised, so a single bad sub-agent doesn't crash the merge.
    """
    try:
        return meta_mod.load_meta(path), None
    except (ValueError, OSError) as e:
        return None, str(e)


def _confidence_for_evidence_count(n):
    if n >= 3:
        return 'high'
    if n == 2:
        return 'medium'
    return 'low'


_CONFIDENCE_RANK = {'low': 0, 'medium': 1, 'high': 2}


def _promote_confidence(current, candidate):
    """Return the higher-ranked confidence. Never downgrade."""
    if _CONFIDENCE_RANK.get(candidate, -1) > _CONFIDENCE_RANK.get(current, -1):
        return candidate
    return current


def _append_evidence_ref(term, chunk_id):
    """Append chunk_id to term's evidence_refs (FIFO cap at 5, dedup), then
    promote confidence based on the new length. Returns True if anything
    changed."""
    refs = term.get('evidence_refs', [])
    if chunk_id in refs:
        return False
    refs.append(chunk_id)
    if len(refs) > EVIDENCE_REFS_CAP:
        refs = refs[-EVIDENCE_REFS_CAP:]
    term['evidence_refs'] = refs
    term['confidence'] = _promote_confidence(
        term.get('confidence', 'low'),
        _confidence_for_evidence_count(len(refs)),
    )
    return True


def _append_note(term, line):
    """Append a line to a term's notes, keeping it readable."""
    existing = term.get('notes', '') or ''
    term['notes'] = (existing + '\n' + line).lstrip('\n')


def _build_surface_index(glossary):
    """Map every surface form (source or alias) → (term_id, role)."""
    idx = {}
    for t in glossary['terms']:
        idx[t['source']] = (t['id'], 'source')
        for a in t.get('aliases', []):
            idx[a] = (t['id'], 'alias')
    return idx


def _find_term_by_id(glossary, entity_id):
    for t in glossary['terms']:
        if t['id'] == entity_id:
            return t
    return None


def _find_term_by_surface(glossary, surface):
    """Return the term whose source OR any alias equals `surface`."""
    for t in glossary['terms']:
        if t['source'] == surface or surface in t.get('aliases', []):
            return t
    return None


def cmd_prepare_merge(temp_dir):
    """Scan unmerged metas and propose a merge plan. Output JSON on stdout."""
    glossary_path = _glossary_path(temp_dir)
    glossary = glossary_mod.load_glossary(glossary_path)
    applied = glossary.get('applied_meta_hashes', {})

    meta_paths = sorted(Path(temp_dir).glob(META_GLOB))

    consumed_chunk_ids = []
    malformed_chunk_ids = []
    # Loaded metas keyed by chunk_id, only including non-malformed unmerged ones.
    loaded = {}

    for path in meta_paths:
        try:
            chunk_id = meta_mod.chunk_id_from_meta_path(str(path))
        except ValueError as e:
            sys.stderr.write(f"warn: {e}\n")
            continue

        data, err = _try_load_meta(str(path))
        if err is not None:
            sys.stderr.write(
                f"warn: malformed meta for {chunk_id} at {path} — quarantined ({err})\n"
            )
            malformed_chunk_ids.append(chunk_id)
            continue

        content_hash = meta_mod.meta_content_hash(data)
        if applied.get(chunk_id) == content_hash:
            continue  # already applied, skip silently

        loaded[chunk_id] = data
        consumed_chunk_ids.append(chunk_id)

    # Group new_entities across all loaded metas by source string.
    grouped_new_entities = {}
    for chunk_id, data in loaded.items():
        for entity in data.get('new_entities', []):
            src = entity['source']
            grouped_new_entities.setdefault(src, []).append({
                'chunk_id': chunk_id,
                'target_proposal': entity['target_proposal'],
                'category': entity.get('category', ''),
                'evidence': entity['evidence'],
            })

    surface_idx = _build_surface_index(glossary)

    auto_apply = []
    decisions_needed = []
    next_decision_id = 1

    def _new_decision_id():
        nonlocal next_decision_id
        did = f'd{next_decision_id}'
        next_decision_id += 1
        return did

    # Helper: build the standalone_variants array for a variant pulled out
    # of grouped_new_entities. Each (target, category) pair gets its own
    # variant entry with its evidence_chunks, parallel to conflicting_new_entity_proposals.
    def _standalone_variants_for(proposals):
        grouped_by_pair = {}
        for p in proposals:
            grouped_by_pair.setdefault((p['target_proposal'], p['category']), []).append(p)
        out = []
        for (target, category), ps in grouped_by_pair.items():
            out.append({
                'target_proposal': target,
                'category': category,
                'evidence': ps[0]['evidence'],
                'evidence_chunks': sorted({p['chunk_id'] for p in ps}),
            })
        return out

    # Detect collisions between same-batch new_entity proposals and alias_hypotheses.
    # If any meta says new_entities=[{source:Taig}] AND any meta says
    # alias_hypotheses=[{variant:Taig, may_be_alias_of_source:Tai}], then "Taig" can't
    # be both a standalone source AND an alias of Tai (surface-form uniqueness
    # violation). Emit a single combined `alias_or_new_entity` decision that exposes
    # EVERY competing standalone variant as a separate `use_standalone_N` choice, so
    # the orchestrator can pick the right standalone target if it rejects the alias.
    # Remove the variant's source from grouped_new_entities so the loop below doesn't
    # double-handle it.
    consumed_alias_keys = set()  # (chunk_id, variant, candidate) of already-handled hyps
    for chunk_id, data in loaded.items():
        for ah in data.get('alias_hypotheses', []):
            variant = ah['variant']
            if variant not in grouped_new_entities:
                continue
            candidate_source = ah['may_be_alias_of_source']
            candidate_term = _find_term_by_surface(glossary, candidate_source)
            # The candidate may also be in this batch's pending auto_apply (a NEW
            # entity proposed in another chunk). That's still a valid alias target
            # because apply-merge processes auto_apply (Phase 2) before decisions
            # (Phase 3) — the alias decision will see the canonical source.
            candidate_in_pending = candidate_source in grouped_new_entities
            if candidate_term is None and not candidate_in_pending:
                continue  # candidate truly absent
            if variant in surface_idx:
                # Variant already exists as a surface in the glossary; the
                # new_entity_existing_alias / conflict path will handle it.
                continue
            # Collision detected. Build standalone_variants from ALL proposals
            # (preserves cross-chunk competing target/category pairs).
            proposals = grouped_new_entities.pop(variant)
            standalone_variants = _standalone_variants_for(proposals)
            canonical_candidate = candidate_term['source'] if candidate_term else candidate_source
            options = ['yes_alias'] + [
                f'use_standalone_{i}' for i in range(len(standalone_variants))
            ] + ['skip']
            decisions_needed.append({
                'id': _new_decision_id(),
                'kind': 'alias_or_new_entity',
                'variant': variant,
                'candidate_source': canonical_candidate,
                'alias_evidence': ah['evidence'],
                'standalone_variants': standalone_variants,
                'options': options,
            })
            consumed_alias_keys.add((chunk_id, variant, candidate_source))

    for src, proposals in grouped_new_entities.items():
        target_cat_pairs = {(p['target_proposal'], p['category']) for p in proposals}
        if src in surface_idx:
            owner_id, owner_role = surface_idx[src]
            owner_term = _find_term_by_id(glossary, owner_id)
            if owner_role == 'source':
                # Source already exists as someone's source → flag as conflict.
                # Take the first proposal (sub-agent voted on a translation that
                # disagrees with what's already canonical).
                p = proposals[0]
                if p['target_proposal'] != owner_term['target']:
                    decisions_needed.append({
                        'id': _new_decision_id(),
                        'kind': 'conflict',
                        'entity_source': src,
                        'field': 'target',
                        'current': owner_term['target'],
                        'proposed': p['target_proposal'],
                        'evidence': p['evidence'],
                        'options': ['keep_current', 'accept_proposed', 'record_in_notes'],
                    })
                # else: identical target — nothing to do.
            else:  # role == 'alias'
                p = proposals[0]
                decisions_needed.append({
                    'id': _new_decision_id(),
                    'kind': 'new_entity_existing_alias',
                    'proposed_source': src,
                    'currently_alias_of': owner_id,
                    'proposed_target': p['target_proposal'],
                    'proposed_category': p['category'],
                    'evidence': p['evidence'],
                    'options': ['promote_to_separate_entity', 'keep_as_alias', 'skip'],
                })
        elif len(target_cat_pairs) == 1:
            # All proposals agree → auto_apply with combined evidence.
            target, category = next(iter(target_cat_pairs))
            chunk_ids = sorted({p['chunk_id'] for p in proposals})
            auto_apply.append({
                'action': 'add_entity',
                'entity': {
                    'source': src,
                    'target_proposal': target,
                    'category': category,
                    'evidence': proposals[0]['evidence'],
                },
                'evidence_chunks': chunk_ids,
            })
        else:
            # Conflicting proposals across chunks — main agent must pick.
            variants = _standalone_variants_for(proposals)
            options = [f'use_variant_{i}' for i in range(len(variants))] + ['skip']
            decisions_needed.append({
                'id': _new_decision_id(),
                'kind': 'conflicting_new_entity_proposals',
                'source': src,
                'variants': variants,
                'options': options,
            })

    # After the collision pass + auto_apply build, compute the set of sources
    # that WILL be in the glossary by the time apply-merge processes decisions.
    # auto_apply runs in apply-merge Phase 2, before the decisions phase, so any
    # candidate that's about to be auto-added is a valid alias target this round.
    auto_apply_sources = {entry['entity']['source'] for entry in auto_apply}

    # Alias hypotheses: if variant matches an existing entity (by surface) OR
    # the candidate is in auto_apply (about to be added this batch), flag.
    for chunk_id, data in loaded.items():
        for ah in data.get('alias_hypotheses', []):
            variant = ah['variant']
            candidate_source = ah['may_be_alias_of_source']
            if (chunk_id, variant, candidate_source) in consumed_alias_keys:
                continue  # already wrapped into an alias_or_new_entity decision
            candidate_term = _find_term_by_surface(glossary, candidate_source)
            candidate_in_auto_apply = candidate_source in auto_apply_sources
            if candidate_term is None and not candidate_in_auto_apply:
                continue  # candidate truly absent; alias hypothesis is moot
            if variant in surface_idx or variant in auto_apply_sources:
                # Variant already exists or is about to exist as a standalone source;
                # the new_entity_existing_alias / conflict / alias_or_new_entity
                # path handles those collisions.
                continue
            canonical_candidate = candidate_term['source'] if candidate_term else candidate_source
            decisions_needed.append({
                'id': _new_decision_id(),
                'kind': 'alias',
                'variant': variant,
                'candidate_source': canonical_candidate,
                'evidence': ah['evidence'],
                'options': ['yes_alias', 'no_separate_entity', 'skip'],
            })

    # Sub-agent-flagged conflicts.
    for chunk_id, data in loaded.items():
        for c in data.get('conflicts', []):
            entity_term = _find_term_by_surface(glossary, c['entity_source'])
            if entity_term is None:
                continue  # nothing to conflict with
            field = c['field']
            current_val = entity_term.get(field, '')
            decisions_needed.append({
                'id': _new_decision_id(),
                'kind': 'conflict',
                'entity_source': c['entity_source'],
                'field': field,
                'current': current_val,
                'proposed': c['observed_better'],
                'evidence': c['evidence'],
                'options': ['keep_current', 'accept_proposed', 'record_in_notes'],
            })

    output = {
        'auto_apply': auto_apply,
        'decisions_needed': decisions_needed,
        'consumed_chunk_ids': sorted(consumed_chunk_ids),
        'malformed_meta_chunk_ids': sorted(malformed_chunk_ids),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


def cmd_apply_merge(temp_dir):
    """Read decisions JSON from stdin and apply to the glossary."""
    try:
        decisions_doc = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"error: decisions JSON on stdin is not valid: {e}\n")
        sys.exit(2)

    if not isinstance(decisions_doc, dict):
        sys.stderr.write("error: decisions JSON must be an object\n")
        sys.exit(2)

    auto_apply = decisions_doc.get('auto_apply', [])
    decisions = decisions_doc.get('decisions', [])
    consumed_chunk_ids = decisions_doc.get('consumed_chunk_ids', [])

    if not isinstance(auto_apply, list):
        sys.stderr.write("error: 'auto_apply' must be a list\n")
        sys.exit(2)
    if not isinstance(decisions, list):
        sys.stderr.write("error: 'decisions' must be a list\n")
        sys.exit(2)
    if not isinstance(consumed_chunk_ids, list):
        sys.stderr.write("error: 'consumed_chunk_ids' must be a list\n")
        sys.exit(2)

    glossary_path = _glossary_path(temp_dir)
    glossary = glossary_mod.load_glossary(glossary_path)

    auto_applied = 0
    decisions_resolved = 0
    errors = []

    # Phase 1: process every consumed meta — used_term_sources confidence escalation
    # AND content hash recording. This runs even with empty auto_apply / decisions.
    consumed_metas = {}  # chunk_id -> meta data
    for chunk_id in consumed_chunk_ids:
        meta_path = _meta_path(temp_dir, chunk_id)
        if not os.path.exists(meta_path):
            errors.append(f"no meta file for chunk_id={chunk_id} at {meta_path}")
            continue
        # Defensive: re-derive chunk_id from filename and check.
        derived = meta_mod.chunk_id_from_meta_path(meta_path)
        if derived != chunk_id:
            errors.append(
                f"chunk_id mismatch: consumed_chunk_ids has {chunk_id!r}, "
                f"filename derives {derived!r}"
            )
            continue
        data, err = _try_load_meta(meta_path)
        if err is not None:
            errors.append(f"meta for chunk_id={chunk_id} is malformed: {err}")
            continue
        consumed_metas[chunk_id] = data

    if errors:
        sys.stderr.write("error: apply-merge aborting before any state mutation:\n")
        for e in errors:
            sys.stderr.write(f"  - {e}\n")
        sys.exit(2)

    # Pre-validate every decision payload BEFORE we mutate. If any decision is
    # malformed (wrong kind, invalid choice for kind, missing fields) we abort
    # the entire transaction without writing any hashes — otherwise on retry
    # prepare-merge would skip the affected metas and the unresolved decisions
    # would be lost forever.
    pre_errors = []
    for d in decisions:
        d_id = d.get('id')
        kind = d.get('kind')
        choice = d.get('choice')
        if kind is None or choice is None:
            pre_errors.append(f"decision {d_id!r} missing 'kind' or 'choice'")
            continue
        if kind == 'conflicting_new_entity_proposals':
            variants = d.get('variants') or []
            valid = {f'use_variant_{i}' for i in range(len(variants))} | {'skip'}
            if choice not in valid:
                pre_errors.append(
                    f"decision {d_id!r} (kind={kind}): invalid choice {choice!r}, "
                    f"must be one of {sorted(valid)}"
                )
            continue
        if kind == 'alias_or_new_entity':
            standalone_variants = d.get('standalone_variants') or []
            valid = (
                {'yes_alias', 'skip'}
                | {f'use_standalone_{i}' for i in range(len(standalone_variants))}
            )
            if choice not in valid:
                pre_errors.append(
                    f"decision {d_id!r} (kind={kind}): invalid choice {choice!r}, "
                    f"must be one of {sorted(valid)}"
                )
            continue
        valid = VALID_CHOICES_BY_KIND.get(kind)
        if valid is None:
            pre_errors.append(f"decision {d_id!r}: unknown kind {kind!r}")
            continue
        if choice not in valid:
            pre_errors.append(
                f"decision {d_id!r} (kind={kind}): invalid choice {choice!r}, "
                f"must be one of {sorted(valid)}"
            )

    if pre_errors:
        sys.stderr.write("error: apply-merge aborting before any state mutation:\n")
        for e in pre_errors:
            sys.stderr.write(f"  - {e}\n")
        sys.exit(2)

    # Now safe to mutate. Process used_term_sources first (purely additive).
    for chunk_id, data in consumed_metas.items():
        for src in data.get('used_term_sources', []):
            term = _find_term_by_surface(glossary, src)
            if term is None:
                continue
            _append_evidence_ref(term, chunk_id)

    # Process attribute_hypotheses (currently: gender). Roadmap rule:
    # unknown → first explicit evidence sets value + promotes confidence;
    # corroborating evidence keeps value + promotes confidence; conflicting
    # evidence resets to unknown and records both sides in notes.
    for chunk_id, data in consumed_metas.items():
        for ah in data.get('attribute_hypotheses', []):
            entity_source = ah.get('entity_source')
            attribute = ah.get('attribute')
            value = ah.get('value')
            evidence = ah.get('evidence', '')
            term = _find_term_by_surface(glossary, entity_source)
            if term is None:
                continue
            if attribute == 'gender':
                if value not in VALID_GENDER_VALUES:
                    # Schema validation accepted any string; we silently no-op
                    # on values the glossary doesn't model.
                    continue
                current = term.get('gender', 'unknown')
                if current == 'unknown':
                    term['gender'] = value
                    _append_evidence_ref(term, chunk_id)
                    _append_note(
                        term,
                        f"[gender] {value} set (chunk {chunk_id}); evidence={evidence!r}"
                    )
                elif current == value:
                    # Corroborating — promote entity confidence.
                    _append_evidence_ref(term, chunk_id)
                else:
                    # Conflict: revert to unknown, record both observations.
                    term['gender'] = 'unknown'
                    _append_note(
                        term,
                        f"[gender] conflict — was {current!r}, observed {value!r} "
                        f"in chunk {chunk_id}; evidence={evidence!r}; reverted to 'unknown'"
                    )
            # Other attributes: log to notes but don't mutate canonical fields.
            else:
                _append_note(
                    term,
                    f"[attr {attribute!r}] observed {value!r} in chunk {chunk_id}; "
                    f"evidence={evidence!r}"
                )

    # Phase 2: auto_apply new entities.
    for entry in auto_apply:
        if entry.get('action') != 'add_entity':
            errors.append(f"unknown auto_apply action: {entry.get('action')!r}")
            continue
        entity = entry['entity']
        evidence_chunks = list(entry.get('evidence_chunks', []))[:EVIDENCE_REFS_CAP]
        new_term = {
            'id': entity['source'],
            'source': entity['source'],
            'target': entity['target_proposal'],
            'category': entity.get('category', ''),
            'aliases': [],
            'gender': 'unknown',
            'confidence': _confidence_for_evidence_count(len(evidence_chunks)),
            'frequency': 0,
            'evidence_refs': evidence_chunks,
            'notes': '',
        }
        glossary['terms'].append(new_term)
        auto_applied += 1

    # Phase 3: process decisions. All choices have been pre-validated above;
    # any error here is a referential failure (entity not in glossary, etc.)
    # and aborts the whole transaction so the orchestrator can re-attempt
    # with a corrected payload.
    for d in decisions:
        d_id = d.get('id')
        kind = d['kind']
        choice = d['choice']

        if kind == 'alias':
            if choice == 'yes_alias':
                variant = d.get('variant')
                candidate_source = d.get('candidate_source')
                term = _find_term_by_surface(glossary, candidate_source)
                if term is None:
                    errors.append(
                        f"decision {d_id!r}: candidate source {candidate_source!r} "
                        f"not in glossary"
                    )
                    continue
                if variant not in term.get('aliases', []):
                    term.setdefault('aliases', []).append(variant)
            decisions_resolved += 1

        elif kind == 'conflict':
            entity_source = d.get('entity_source')
            field = d.get('field')
            current = d.get('current')
            proposed = d.get('proposed')
            evidence = d.get('evidence', '')
            term = _find_term_by_surface(glossary, entity_source)
            if term is None:
                errors.append(
                    f"decision {d_id!r}: entity_source {entity_source!r} not in glossary"
                )
                continue
            if choice == 'record_in_notes':
                _append_note(
                    term,
                    f"[conflict] {field}: current={current!r} "
                    f"observed_better={proposed!r} evidence={evidence!r}",
                )
            elif choice == 'accept_proposed':
                old_value = term.get(field)
                term[field] = proposed
                _append_note(
                    term,
                    f"[updated] {field}: was={old_value!r} now={proposed!r}",
                )
            # keep_current: no-op
            decisions_resolved += 1

        elif kind == 'new_entity_existing_alias':
            if choice == 'promote_to_separate_entity':
                proposed_source = d.get('proposed_source')
                host_id = d.get('currently_alias_of')
                proposed_target = d.get('proposed_target')
                proposed_category = d.get('proposed_category', '')
                evidence = d.get('evidence', '')
                host = _find_term_by_id(glossary, host_id)
                if host is None:
                    errors.append(
                        f"decision {d_id!r}: host term id={host_id!r} not in glossary"
                    )
                    continue
                if proposed_source in host.get('aliases', []):
                    host['aliases'] = [a for a in host['aliases'] if a != proposed_source]
                glossary['terms'].append({
                    'id': proposed_source,
                    'source': proposed_source,
                    'target': proposed_target,
                    'category': proposed_category,
                    'aliases': [],
                    'gender': 'unknown',
                    'confidence': 'low',
                    'frequency': 0,
                    'evidence_refs': [],
                    'notes': f'promoted from alias of {host_id!r}; evidence={evidence!r}',
                })
            decisions_resolved += 1

        elif kind == 'alias_or_new_entity':
            variant = d.get('variant')
            candidate_source = d.get('candidate_source')
            standalone_variants = d.get('standalone_variants') or []
            if choice == 'yes_alias':
                term = _find_term_by_surface(glossary, candidate_source)
                if term is None:
                    errors.append(
                        f"decision {d_id!r}: candidate source {candidate_source!r} "
                        f"not in glossary"
                    )
                    continue
                if variant not in term.get('aliases', []):
                    term.setdefault('aliases', []).append(variant)
            elif choice == 'skip':
                pass
            else:
                # use_standalone_N
                m = re.match(r'^use_standalone_(\d+)$', choice)
                idx = int(m.group(1))
                chosen = standalone_variants[idx]
                # Combine evidence_chunks across ALL standalone variants — every
                # variant attests the surface form exists, even though they
                # disagreed on the right translation.
                combined_chunks = sorted({
                    cid for v in standalone_variants for cid in v.get('evidence_chunks', [])
                })[:EVIDENCE_REFS_CAP]
                glossary['terms'].append({
                    'id': variant,
                    'source': variant,
                    'target': chosen['target_proposal'],
                    'category': chosen.get('category', ''),
                    'aliases': [],
                    'gender': 'unknown',
                    'confidence': _confidence_for_evidence_count(len(combined_chunks)),
                    'frequency': 0,
                    'evidence_refs': combined_chunks,
                    'notes': '',
                })
            decisions_resolved += 1

        elif kind == 'conflicting_new_entity_proposals':
            variants = d.get('variants') or []
            if choice == 'skip':
                decisions_resolved += 1
                continue
            m = re.match(r'^use_variant_(\d+)$', choice)
            idx = int(m.group(1))
            chosen = variants[idx]
            combined_chunks = sorted({
                cid for v in variants for cid in v.get('evidence_chunks', [])
            })[:EVIDENCE_REFS_CAP]
            glossary['terms'].append({
                'id': d['source'],
                'source': d['source'],
                'target': chosen['target_proposal'],
                'category': chosen.get('category', ''),
                'aliases': [],
                'gender': 'unknown',
                'confidence': _confidence_for_evidence_count(len(combined_chunks)),
                'frequency': 0,
                'evidence_refs': combined_chunks,
                'notes': '',
            })
            decisions_resolved += 1

    if errors:
        # Don't write the glossary, don't record any hashes. Orchestrator
        # retries with fixed payload; prepare-merge will surface the same
        # decisions again because no hashes were recorded.
        sys.stderr.write("error: apply-merge failed during decision dispatch; "
                         "aborting transaction (no glossary or hash mutations persisted):\n")
        for e in errors:
            sys.stderr.write(f"  - {e}\n")
        sys.exit(2)

    # Phase 4: record content hashes for all consumed metas. Reached only
    # when every decision succeeded.
    glossary.setdefault('applied_meta_hashes', {})
    for chunk_id, data in consumed_metas.items():
        glossary['applied_meta_hashes'][chunk_id] = meta_mod.meta_content_hash(data)

    # Validate before save (save_glossary will re-validate too — this surfaces
    # surface-uniqueness violations from a bad alias decision before we touch disk).
    try:
        glossary_mod.save_glossary(glossary_path, glossary)
    except ValueError as e:
        sys.stderr.write(f"error: post-merge glossary failed validation; aborting: {e}\n")
        sys.exit(2)

    summary = {
        'auto_applied': auto_applied,
        'decisions_resolved': decisions_resolved,
        'consumed_chunks': len(consumed_metas),
        'errors': errors,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def cmd_status(temp_dir):
    """Read-only summary for the final verification report."""
    glossary_path = _glossary_path(temp_dir)
    if os.path.exists(glossary_path):
        glossary = glossary_mod.load_glossary(glossary_path)
        applied = glossary.get('applied_meta_hashes', {})
    else:
        applied = {}

    translated_paths = sorted(Path(temp_dir).glob(TRANSLATED_GLOB))
    # Strip the .meta.json variants that the glob also catches if any.
    translated_paths = [p for p in translated_paths if not p.name.endswith('.meta.json')]
    translated_chunk_ids = set()
    for p in translated_paths:
        m = re.match(r'^output_(chunk\d+)\.md$', p.name)
        if m:
            translated_chunk_ids.add(m.group(1))

    meta_paths = sorted(Path(temp_dir).glob(META_GLOB))
    found_chunk_ids = set()
    consumed_chunk_ids = set()
    malformed_chunk_ids = []
    unmerged_chunk_ids = []
    for path in meta_paths:
        try:
            chunk_id = meta_mod.chunk_id_from_meta_path(str(path))
        except ValueError:
            continue
        found_chunk_ids.add(chunk_id)
        data, err = _try_load_meta(str(path))
        if err is not None:
            malformed_chunk_ids.append(chunk_id)
            continue
        if applied.get(chunk_id) == meta_mod.meta_content_hash(data):
            consumed_chunk_ids.add(chunk_id)
        else:
            unmerged_chunk_ids.append(chunk_id)

    missing_meta = sorted(translated_chunk_ids - found_chunk_ids)

    out = {
        'translated_chunks': len(translated_chunk_ids),
        'meta_files_found': len(found_chunk_ids),
        'meta_files_consumed': len(consumed_chunk_ids),
        'unmerged_meta_files': len(unmerged_chunk_ids),
        'malformed_meta_files': len(malformed_chunk_ids),
        'missing_meta_chunk_ids': missing_meta[:10],
        'malformed_meta_chunk_ids': sorted(malformed_chunk_ids)[:10],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Merge sub-agent meta files into glossary")
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_prep = sub.add_parser('prepare-merge', help="Scan unmerged metas; emit merge proposal")
    p_prep.add_argument('temp_dir')

    p_app = sub.add_parser('apply-merge', help="Apply merge decisions from stdin")
    p_app.add_argument('temp_dir')

    p_stat = sub.add_parser('status', help="Read-only observability snapshot")
    p_stat.add_argument('temp_dir')

    args = parser.parse_args()

    if args.cmd == 'prepare-merge':
        cmd_prepare_merge(args.temp_dir)
    elif args.cmd == 'apply-merge':
        cmd_apply_merge(args.temp_dir)
    elif args.cmd == 'status':
        cmd_status(args.temp_dir)


if __name__ == '__main__':
    main()
