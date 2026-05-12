# ground_truth_draft/

JSON drafts produced by Claude Opus reading each image with vision. They
are **not** ground truth yet — they exist so the human review step is
"correct what is wrong" instead of "type from scratch".

## Workflow

1. For each `.json` here, open the matching image in `inputs/originais/`.
2. Eyeball field by field. Pay extra attention to:
   - `MODELO` and `LOTE`: handwriting variation, easy to misread digits/letters.
   - `OV` / `OF`: leading/trailing digits drop or duplicate easily.
   - `CONI`: `OCT` vs `OCT.` vs `12` vs `T` vs `TORRES` — capture exactly what's written.
   - `ESP`: comma decimal (`2,6` not `2.6`) is the Portuguese convention.
3. When a draft is correct (or you've corrected it), copy it to
   `ground_truth/<same-name>.json`.

The benchmark only reads from `ground_truth/`. As long as drafts stay in
this folder, baseline numbers are safe from contamination.

## Where to start the review

Before reading every draft top-to-bottom, run:

    .\.venv\Scripts\python.exe scripts/cross_check_drafts.py

It compares every row that shares an OF and flags divergent
`cliente` / `modelo` / `comp_mm` / `larg_mm`. Output:
`reports/draft_cross_check.md`. Most likely transcription errors land
in that report — single-letter modelo mismatches (E ↔ F),
implausibly small dimensions, etc.

Then for the rest of the drafts: open each `.json`, open the matching
image in `inputs/originais/`, eyeball field by field.

## Sanity check

After moving files into `ground_truth/`, run:

    .\.venv\Scripts\python.exe scripts/annotate_cli.py --validate-only

It re-parses every `ground_truth/*.json` against the schema and reports
any structural issues (missing keys, type mismatches, invalid date
separators).

## Why drafts and not direct annotation

I'm a more capable VLM than Qwen2.5-VL-7B (the model the pipeline will
serve). If the benchmark used drafts I produced as-is, the numbers
would measure agreement between two AI models — not extraction
accuracy. Drafts shortcut the typing; the human verification step is
what makes them ground truth.
