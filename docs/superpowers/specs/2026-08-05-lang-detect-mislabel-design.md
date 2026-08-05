# Language detection mislabels English mail — design (#255)

**Status:** accepted, 2026-08-05
**Issue:** [#255](https://github.com/hherb/localmail/issues/255)
**Predecessor:** [#251](https://github.com/hherb/localmail/issues/251) —
[2026-08-05-lang-detect-starvation-design.md](2026-08-05-lang-detect-starvation-design.md)

## Problem

`messages.body_lang` assigns confident, wrong languages to English mail. On the
live Mac archive **17,129 of 100,922 labels (17%)** name a language with no
plausible presence in the archive. Yoruba alone is the second most common
language at 7,593 rows; `fi` (910), `eo` (831), `et` (745), `cy` (605),
`la` (491) and `az` (476) are the same failure.

The errors are **correlated, not random**: the affected mail is overwhelmingly
marketing and newsletter traffic, so `lang:en` silently excludes ~7,600 English
newsletters. Excluding your own mailing-list mail from an English query is the
exact inverse of what the filter is for.

This is pre-existing detector quality, not a regression. It was invisible until
#251 unwedged detection and the archive went from 7,744 to 100,922 labelled
rows.

### Root cause

`LinguaDetector.detect` passes `body_text` to lingua essentially raw. Marketing
bodies are dominated by tracking URLs whose path segments are long runs of
high-entropy alphanumerics. Lingua in `low_accuracy` mode scores that soup
above `body_lang_min_confidence = 0.65` and lands on whichever low-resource
language its trigram model likes.

## Measurements

All numbers below were taken against the live Mac archive
(127,230 messages), not synthetic fixtures. Buckets are 300 randomly-sampled
rows each. "bad rows" = rows whose current label is outside
`{en, de, fr, es, it, nl, sv, da, no, pt}`.

### The 2×2

| accuracy | detector input | bad rows still implausible | `en` rows kept | throughput |
|---|---|---|---|---|
| low | raw *(current policy)* | 300 / 300 | 100% | 75 rows/s |
| low | URL-stripped | 94 | 89% | 108 rows/s |
| full | raw | 155 | 96% | 119 rows/s |
| **full** | **URL-stripped** | **3** | **96%** | **176 rows/s** |

**Neither change alone suffices.** URL-stripping alone resolves 69%,
full-accuracy alone 48%; together **99%**. This is the central finding — the
issue proposed the two as alternatives.

### Three assumptions in the issue that the measurements overturn

1. **"Full accuracy is ~1 GB resident vs ~100 MB."** Measured peak RSS after
   800 detections in a fresh process: **227 MB full, 239 MB low.** Full is
   marginally *cheaper* and 2.3× faster. Lingua 2.2.0 loads per-language models
   lazily, so the accuracy mode barely moves resident size. The config comment
   asserting the 100 MB / 1 GB figures is simply wrong and is corrected here.

2. **"Raising `body_lang_min_confidence` is the cheapest lever."** Moving the
   floor 0.65 → 0.90 reduced implausible labels from 64 to 62 out of 500.
   Low-accuracy lingua is *confidently* wrong, so a confidence floor cannot
   discriminate. This direction is rejected on evidence.

3. **"Invisible preheader padding (U+034F) is a primary cause."** An ablation
   over normalisation steps shows URL stripping does all of the work:

   | normalisation | bad rows resolved | `en` kept |
   |---|---|---|
   | none | 48% | 96% |
   | invisible chars only | 48% | 96% |
   | **URLs only** | **99%** | **96%** |
   | URLs + emails + invisible | 99% | 96% |
   | URLs + emails + invisible + HTML tags | 99% | 95% |
   | + separator rules | 99% | 95% |

   (This ablation holds the detector at **full accuracy** throughout, so its
   "none" row is the 2×2's `full / raw` row — it isolates the normalisation
   variable alone.)

   Invisible-character, email-address, HTML-tag and separator-rule stripping
   each add **zero** measurable benefit once URLs are removed. They are
   therefore **not implemented** — untested-in-practice normalisation is
   liability, not insurance.

### The one apparent regression, verified

Under the new policy only 64% of currently-`de` rows keep that label. This is a
**correction, not damage**. Inspecting 20 of the 81 `de → en` flips by hand:
19 are unambiguously English mail wrongly labelled `de` ("Dear Dr Horst Herb,
Enclosed you will find your current invoice…", "Please be advised of the
following vacancy…"). One (message 115475) is a genuine German thread carrying
a large English quoted-reply block — a genuinely mixed-language body where
either answer is defensible.

Rows currently labelled `en` are the control: **96% keep the label, 0% move to
another language**, the remainder declining. The change does not invent
non-English labels for English mail.

## Design

### 1. `search/lang_text.py` — a new pure module

```python
normalize_for_detection(text: str) -> str
```

Strips URLs (`http://`, `https://`, `www.` forms) and collapses whitespace.
Nothing else, per the ablation above.

It is a separate pure module rather than a private helper for the same reason
`pgtext.py`, `attachment_kind.py`, `ocr_policy.py` and `account_names.py` are:
it is a rule, it is unit-testable without a database or a model download, and
having exactly one authority for "what the detector sees" is what stops a
second normalisation rule appearing somewhere else later.

**Blast radius is deliberately minimal.** It is applied *only* inside
`LinguaDetector.detect`. `messages.body_text`, the FTS `tsvector`, chunking and
embeddings all continue to see the original body. This change alters what the
detector reads, never what the archive stores.

### 2. Ordering: normalize → floor → detect

`detect()` currently strips whitespace, applies the `min_text_chars` floor, then
detects. The floor now measures the **normalised** text.

This is load-bearing, not incidental. A 500-character body consisting entirely
of tracking URLs has no linguistic content; measured against the raw length it
clears the 20-character floor comfortably and receives a confident garbage
label. Measured after normalisation it is empty and is correctly **declined**.
The increase in declines observed in the measurements is this effect, and it is
the desired behaviour — `body_lang` must keep meaning "detected language, else
unknown".

### 3. Config

`body_lang_low_accuracy` default flips `True → False`. The knob is retained for
a genuinely memory-constrained host, documented with the measured figures
rather than the current incorrect ones.

Retaining it is a deliberate hedge: both deployments differ in architecture
(Mac arm64 native; DGX aarch64 under Docker) and the measurements were taken on
one of them. If the DGX behaves differently, the escape hatch is a config edit
rather than a release.

### 4. Re-labelling the archive

The existing 100,922 labels were produced by the old policy and stay wrong
unless re-run. `retry_declined` does **not** reach them — by construction it
re-opens only rows with no label.

`lang_detect.reopen_all(conn) -> int` clears `body_lang` **and**
`body_lang_attempted_at` for every row with a body — `RELABELABLE_WHERE_SQL =
"body_text IS NOT NULL"`, a module constant joining the existing
`CLAIMABLE_WHERE_SQL` / `DECLINED_WHERE_SQL` pair. One authority per predicate,
and the partition test covers all three: claimable and declined are disjoint
subsets of relabelable.

Exposed as `localmail lang-backfill --relabel`, **gated behind an interactive
confirmation** (`--yes` to skip, for scripted use). This is the only
destructive operation in the change — it discards every existing label — and
the project's convention elsewhere (`remove-account --force`,
`retry-failed-fetches --forget`) is to make destructive intent explicit.

Cost: ~100k rows at 176 rows/s ≈ **10 minutes per host**.

### 5. No migration

No schema change. The latest migration remains
`0035_messages_body_lang_attempted_at.sql`; next free slot `0036_*.sql`.

## Testing

Written before the implementation, per project convention.

- **`tests/test_lang_text.py`** — pure, no DB, no model download.
  URL forms (`http`, `https`, `www.`, query strings, angle-bracketed
  `<http://…>`, markdown-link `[text](https://…)`); text containing no URL
  passes through unchanged; URL-only input normalises to empty; idempotence;
  empty input.
- **`tests/test_lang_detect.py`** — a URL-only body is declined rather than
  labelled; the length floor applies to the normalised text; `reopen_all`
  clears both columns; the three WHERE predicates remain a disjoint,
  jointly-exhaustive partition.
- **`tests/test_cli_lang_backfill.py`** — `--relabel` re-opens then drains;
  the confirmation gate refuses without `--yes`.
- A config-default assertion pinning `body_lang_low_accuracy is False`, so the
  flip cannot silently revert.

## Acceptance

- Marketing/newsletter mail is labelled `en` or declined — never assigned a
  language with no plausible presence in the archive.
- Post-relabel, rows labelled outside the plausible set fall from **17,129** to
  under ~200 on the Mac archive.
- Rows currently labelled `en` retain that label at ≥95%.
- Both deployments re-labelled and drained (`body_lang_pending` at 0).

## Rejected alternatives

- **Raise `body_lang_min_confidence`** — measured near-useless (§Measurements).
- **Restrict the language set via config** (issue direction 4) — would fix the
  symptom by fiat while giving a guaranteed-wrong answer for genuinely
  out-of-set mail, and adds an operator knob whose correct value is unknowable
  in advance. Full-accuracy mode achieves the same outcome without the knob.
- **Strip invisible characters / HTML / separators** — measured zero benefit.
- **A sentinel `body_lang` value for "detected but implausible"** — repeats the
  one-way-door mistake #251 explicitly rejected and #216's `type-skipped`
  documents.
