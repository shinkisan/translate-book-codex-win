# Baselines

Checked-in full-pipeline test inputs live under `tests/baselines/<book-id>/`.

Each baseline directory should contain:

- the original input book file (`.epub`, `.pdf`, or `.docx`)
- a short `SOURCE.md` describing the source and why the baseline exists

Generated outputs do not belong here. Put them under `tests/.artifacts/`.

## Tiers

| Tier | Baseline | Chunks (target_size=6000) | Inline images | When to run |
|---|---|---|---|---|
| Smoke | `sleepy-hollow` | ~21 | 0 (cover only) | Default CI / quick iteration |
| Gold | `standard-alice` | ~38 | ~45 | Release-gate / image-path coverage |
| Alias-stress (opt-in) | `diligent-dick` | ~57 | 5 | When changing `merge_meta` or `glossary` |

The smoke baseline is the cheapest way to exercise convert → chunk → glossary → merge → build. It deliberately gives up inline-image coverage; use the gold baseline whenever image handling could be affected. The alias-stress baseline is intentionally larger than the gold one because its value lies in `Dick / Richard / Stuart` co-occurring across ~9 consecutive chunks — a contested-variant cross-batch scenario the smaller baselines cannot reproduce.

## When adding a new baseline

A baseline that has never been run is not a baseline — it's just a checked-in book. Before merging:

1. Run the full pipeline on it once (`convert.py` → translate via the skill → `merge_and_build.py`).
2. Capture the real chunk count, image count, and any cross-chunk entity / alias evidence in the new `SOURCE.md`. Replace any estimates you used while picking the book.
3. Note coverage gaps explicitly (e.g. "cover only, no inline images" for sleepy-hollow). Future readers should not have to re-derive what this baseline does and does not exercise.

Numbers in `SOURCE.md` come from a measured run, not from Project Gutenberg metadata or word-count rules of thumb.
