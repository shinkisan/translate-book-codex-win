# Roadmap

This roadmap addresses [issue #7](https://github.com/deusyu/translate-book/issues/7): name/term inconsistency and pronoun/gender errors across chunks.

## Background

translate-book splits each book into ~6000-character chunks and translates each via an isolated sub-agent. This isolation prevents context truncation but creates two recurring problems:

1. **Cross-chunk inconsistency** — the same proper noun gets translated multiple ways across the book (Tai / Taig / Taighi / Taiger).
2. **Local-context loss** — pronouns and gendered language fail when a single chunk carries no antecedent (he/she flips arbitrarily for the same character).

Commit `464861a` shipped a v1 glossary feature that addresses **high-frequency main entities** — the protagonist, primary places, recurring domain terms. It does not address low-frequency entities (secondary characters), spelling variants, or pronoun resolution.

The plan below is structured as four independently shippable phases.

## Design principles

These hold across all phases.

- **Scripts do bookkeeping; LLMs do semantic merge.** State management, schema validation, deduplication, hashing, IO are deterministic Python. Naming, gender attribution, alias judgment, conflict resolution are LLM calls.
- **Single writer for shared state.** The main agent is the only writer of `glossary.json` and `run_state.json`. Sub-agents only read shared state and write per-chunk meta files. This eliminates concurrent-write races without locking.
- **Conservative merge.** Hallucinations at the canonical layer pollute every subsequent chunk. New entities require evidence; alias merges require LLM judgment, not string similarity alone; gender starts at `unknown` and only moves to high confidence under explicit or multi-source evidence; canonical values are not silently overwritten on conflict.
- **Three-layer state, three separate files.** `glossary.json` holds canonical knowledge consumed by sub-agents. `output_chunkNNNN.meta.json` holds raw per-chunk observations from sub-agents (not authoritative). `run_state.json` holds orchestration state (what's been translated, with which glossary version, etc.). Each file has a different consumer and lifecycle — keep them separate.

## Phase 1 — Sub-agent feedback + glossary merge

**Status: not started**
**Estimated scope: 1 commit, ~400 lines + tests**

The single largest leverage point. Today, sub-agents read the glossary but cannot write back what they discover during translation. This phase closes the loop.

### Changes

- **`glossary.json` schema → v2**
  - Add per-entity fields: `id` (stable across renames), `aliases` (list of variant spellings), `gender` (`male` / `female` / `nonbinary` / `unknown`), `confidence` (`low` / `medium` / `high`), `evidence_refs` (list of chunk ids), `notes` (free-form, used for conflicts)
  - `load_glossary` accepts both v1 and v2 with one-shot upgrade

- **New: `output_chunkNNNN.meta.json` schema**
  - `new_entities` — proper nouns / domain terms the sub-agent saw that aren't in the injected glossary
  - `alias_hypotheses` — sub-agent's guess that source X is a variant of an existing entity
  - `attribute_hypotheses` — gender, role, or other attributes the sub-agent inferred, each with `confidence` and `evidence` (short quote)
  - `used_entity_ids` — which glossary entries the sub-agent actually relied on (powers Phase 3)
  - `conflicts` — places the sub-agent disagrees with the injected glossary

- **New: `scripts/merge_meta.py`**
  - CLI: `merge_meta.py merge-batch <temp_dir> <chunk_id_list>`
  - Reads the listed chunks' `.meta.json` files
  - Atomic merge into `glossary.json` per the conservative rules above
  - LLM call for semantic decisions (alias yes/no, conflict resolution) — not string heuristics

- **`SKILL.md` Step 4**
  - Sub-agent prompt template extended: in addition to the translated `output_chunkNNNN.md`, also write `output_chunkNNNN.meta.json` with the schema above
  - Step 4.5 (new): after each batch completes, main agent invokes `merge_meta.py merge-batch` for the just-completed chunk ids
  - Subsequent batches read the now-enriched glossary

### Open questions

- **What does an LLM-judged alias merge look like in code?** The script collects alias hypotheses; the merge step needs an LLM call to confirm. How is that LLM call invoked from a Python script — via the same sub-agent mechanism, or a separate call from the main agent in the SKILL.md flow? (Likely: orchestrate from SKILL.md, not from `merge_meta.py`. The script just structures the data.)

## Phase 2 — Neighbor context for pronouns

**Status: not started**
**Estimated scope: 0.5 commit, mostly SKILL.md**
**Depends on: nothing — can ship independently of Phase 1**

Phase 1 addresses entity knowledge. Pronouns are a separate failure mode: even a perfect glossary doesn't tell the sub-agent that *this* "she" three lines into chunk 42 refers to a character last named in chunk 41.

### Changes

- **`SKILL.md` Step 4 prompt assembly**
  - Per chunk, also extract `prev_excerpt` (last ~300 chars of the previous source chunk) and `next_excerpt` (first ~300 chars of the next source chunk)
  - Inject as `{PREV_EXCERPT}` / `{NEXT_EXCERPT}` in the sub-agent prompt
  - Mark explicitly: "for context only — do not translate or include in output"

- **No new state files.** This is purely a prompt-assembly change.

### Open questions

- Excerpt size — 300 chars is a guess. May need to be language- and structure-aware (a paragraph boundary is more useful than a hard char cap).

## Phase 3 — Selective re-translation

**Status: not started, gated on Phase 1 data**
**Estimated scope: 2 commits, ~400 lines + tests**
**Depends on: Phase 1 (`used_entity_ids`)**

Phase 1's batch feedback only improves *forward* — chunks translated before an entity gets discovered keep their inferior translations. Selective rerun closes the *backward* loop.

This is the previously-deferred plan-document Commit 3, refined to integrate with Phase 1's per-entity tracking.

### Changes

- **New: `scripts/run_state.py`** — schema, atomic load/save, per-rule decision logic for "which chunks need re-translation this run"
- **`run_state.json` schema** — per-chunk: `status`, `attempts`, `glossary_version_used`, `entity_ids_used` (from Phase 1), `output_hash`, `translated_at`
- **Decision rules**:
  1. Manifest source hash changed → all chunks (re-split happened)
  2. `output_chunkNNNN.md` missing → translate
  3. Source chunk hash changed → translate
  4. Translation prompt content changed (tracked via prompt-version markers in SKILL.md) → all chunks
  5. Any entity in `entity_ids_used` had its canonical translation upgraded since this chunk was translated → translate this chunk only
- **`SKILL.md` Step 3 rewrite** — replace plain Glob discovery with `run_state.py decide-chunks`

### Open questions

- **UX for confirm-before-rerun.** A glossary edit triggering 30 chunk re-translations costs real money. Should the skill prompt the user before kicking off, or just go? Different users want different defaults.
- **What counts as a "canonical upgrade"?** Adding an alias is probably no-op (translation unchanged). Changing the `target` is definitely an upgrade. Changing `gender` may or may not require re-translation depending on whether pronouns appeared in that chunk. Need clear rules.

## Phase 4 — Bootstrap warm-up

**Status: experimental, gated on Phase 1 data**
**Estimated scope: 0.5 commit, mostly SKILL.md tuning**
**Depends on: Phase 1**

Phase 1 lets the glossary grow batch-by-batch. But the *first* batch sees the smallest glossary, so it has the highest drift risk. Warm-up addresses this.

### Possible approaches (decide after seeing Phase 1 data)

- **Option A** — Sequential bootstrap: translate the first chunk (or first few high-information chunks like first chapter + last chapter) one at a time before opening up to concurrency=8. Cleanest.
- **Option B** — Variable concurrency: first 2 batches at concurrency=2, then ramp to 8. Adds a control surface; harder to tune.
- **Option C** — Skip the warm-up entirely if Phase 1 data shows early-batch drift is small.

The decision belongs to whoever has run the system on real books. Pre-deciding is speculation.

## Cross-cutting open decisions

- **Schema version bump strategy.** Phase 1 takes `glossary.json` to v2. `load_glossary` should one-shot upgrade v1 → v2 (add empty `aliases`, set `gender = unknown`, etc.) so existing temp dirs survive. v2 → v3 (Phase 3) similarly. Document the migration in CLAUDE.md.
- **Sub-agent contract change.** Today: "Output only the translated content — no commentary." Phase 1 changes this to "Output translation to file A; output meta to file B." Sub-agent runtime must support writing two files. SKILL.md must spell this out so it works on Claude Code, OpenClaw, and Codex equivalents.
- **Cost ceiling.** Phase 1 per-batch LLM merge calls add ~N calls per book (where N = batch count). Phase 3 selective rerun adds variable cost depending on glossary edit frequency. The skill should not silently 10× a translation budget — at minimum, log the marginal cost so users see what they're paying for.

## What this roadmap does *not* commit to

- Specific dates. These phases ship when the design holds up under real usage, not on a schedule.
- That all four phases will ship. Phase 4 is gated on data; Phase 3 may be re-scoped or dropped if Phase 1 alone proves "good enough" for most users.
- Backwards compatibility forever. Schema v1 will be supported via auto-upgrade for at least one minor version after v2 lands; users with stuck temp dirs can re-run `convert.py` to regenerate from source.

## Tracking

- Issue #7: name/term and pronoun consistency — addressed by Phase 1 + Phase 2.
- Future commits implementing each phase will reference this document and the phase number in the commit message.
- Status updates to this document happen at the start of each phase (move "not started" → "in progress") and at merge (→ "shipped" with commit hash).
