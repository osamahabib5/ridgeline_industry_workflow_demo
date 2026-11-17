Implement the Ridgeline Quote Desk pipeline in this repository.

## First: inspect safely

- Inspect the project files and RFQ/catalog inputs outside `answer_key/`, including the existing submission checker and README, to understand the requirements and input formats.
- Do not open, inspect, search, or copy any file under `answer_key/`. The pipeline must never read that directory. A separate evaluation script may read it only when explicitly run; do not run that evaluator as part of this task.
- Do not read `.env`, expose credentials, or make network/API calls. Use environment variables for secrets and placeholders in examples.
- Do not execute `pipeline.py` or generate quotes while implementing. Do not run `check_submission.py`, which creates a submission zip.

## Deliverables

Create or update:

1. `pipeline.py` — a command-line pipeline that processes all six source RFQs.
2. `outputs/` — one valid JSON file per RFQ, named after the source filename without its extension:
   - `rfq_01_email.json`
   - `rfq_02_clearwater.json`
   - `rfq_03_tricounty.json`
   - `rfq_04_handwritten.json`
   - `rfq_05_forwarded_chain.json`
   - `rfq_06_messy.json`
3. `evaluation/evaluate.py` and `evaluation/results.json` — a separate evaluator and its results format. The evaluator may read `answer_key/` when intentionally run; the pipeline must not import or call it.
4. `.env.example` — show how to configure the DeepSeek API key using a clearly fake placeholder. Keep `.env` ignored and never include a real key.
5. `README.md` — follow the exact heading and content requirements below.
6. `RIS_workflow.png` — a readable workflow diagram saved at the repository root.

Preserve unrelated files and existing project behavior. Keep the implementation compatible with Python 3.9+ and document any added dependencies.

## JSON contract

Use validated typed models, preferably Pydantic models named `LineItem` and `RFQOutput`. Keep the JSON compatible with the repository’s `check_submission.py` contract.

Each output must be a JSON object with these top-level fields and no others:

- `rfq_id`: source filename stem, exactly.
- `customer_id`: customer ID or `null`.
- `customer_name`: customer name or `null`.
- `lines`: a non-empty array of item-line objects.
- `non_item_rows`: an array of strings for relevant non-product rows.

Each line object must contain:

- `line`: one-based, sequential item-line number.
- `as_written`: the original item text as it appears in the RFQ.
- `quantity`: an integer or `null`; do not round or invent quantities.
- `unit`: an uppercase unit code or `null`. Use the matched product’s `products.csv` unit when available; map common synonyms only when unambiguous. Catalog examples include `EA`, `BX`, `CS`, `RL`, `PR`, `PK`, and `FT`.
- `ridgeline_part_number`: a valid Ridgeline part number or `null`.
- `match_method`: `ridgeline_part_number`, `cross_reference`, `description`, `llm`, or `null`.
- `confidence`: a numeric value from 0.0 to 1.0.
- `needs_review`: a boolean.
- `flags`: an array of strings.
- `note`: a concise explanation or `null`. Use `note` (singular), not `notes`.

Use a `Literal` type for `match_method`, allowing `None`, and validate confidence as between 0 and 1. Use `null` for unknown values. Keep `non_item_rows` outside `lines`; do not add a top-level `needs_review` or other fields that the checker does not permit.

Set each line’s `needs_review` to true if its confidence is below 0.80 or it has any flags; otherwise false. Preserve item order and number item rows consecutively. Keep addresses, signatures, and other non-product content out of `lines`.

## Extraction and customer resolution

- Support the supplied text, PDF, spreadsheet, and image RFQs using appropriate parsers.
- Resolve customers against `customers.csv` using reliable evidence from the RFQ. Do not guess an ID or name. If customer resolution is uncertain, use `null` and flag the affected lines for review.
- For forwarded email chains, use the latest explicit request when it supersedes earlier content.
- Resolve phrases such as “same as above” only when the referenced item is clear in the RFQ. Do not invent prior-order history. If “usual” or similar wording cannot be grounded in the available context, route it through the matching fallback and flag unresolved results for review.
- Fully process `rfq_01`–`rfq_03` and run the pipeline on `rfq_05` and `rfq_06`. `rfq_04_handwritten` may be a stub. It must still have an output file containing exactly one line with `as_written` set to a short description such as `"entire RFQ"`, `needs_review: true`, `flags: ["stub"]`, and a non-empty `note` explaining the stub. No other RFQ may be stubbed.

## Matching sequence

Use `products.csv` and `cross_reference.csv` as the catalog sources. Treat the actual part-number formats in the CSVs as authoritative.

1. **Exact Ridgeline part number:** Check an exact Ridgeline part-number match first. The catalog uses `RL` part numbers. Verify the product exists and is active. For a valid active direct match, use `match_method: "ridgeline_part_number"` and confidence `0.95`.
2. **Discontinued items:** If a matched product is discontinued, follow `replaced_by` only to a verified active product. Guard against missing replacements and replacement cycles. Flag the substitution, explain it in `note`, and require review.
3. **Cross-reference:** Try an exact competitor-part-number lookup in `cross_reference.csv`. Verify the mapped Ridgeline product exists and is active, following a valid replacement if needed. A clean verified match uses `match_method: "cross_reference"` and confidence `0.90`. Treat ambiguous duplicate mappings or cautionary cross-reference notes (for example, package differences or close substitutions) as review cases; do not choose arbitrarily.
4. **Description similarity:** Rank catalog products using a clear, deterministic text-similarity method. If the best score is at least `0.60`, use that score as confidence and set `match_method: "description"`. Verify the candidate’s status before returning it.
5. **DeepSeek fallback:** If description similarity is below `0.60`, send the RFQ line and up to the ten highest-ranked valid candidates to DeepSeek for extraction or matching. Use only the configured API key from `DEEPSEEK_API_KEY`; never log or print it. If DeepSeek returns a valid match, use `match_method: "llm"` and confidence `0.75`, then verify the product and status. If it cannot confidently match, return a null part number and `match_method: null`, confidence `0.0`, flag `no_match_found`, and explain the result in `note`.

Do not let an LLM override a verified exact part-number or cross-reference match. Treat low confidence, substitutions, ambiguous mappings, and any other flagged condition as requiring review.

## Error handling and output reliability

Always write valid JSON for every RFQ, even when parsing or matching fails. Handle recoverable item errors per line. If an RFQ cannot be processed at all, emit a schema-valid output with one error line: preserve available source text (or use `"entire RFQ"`), set unknown values to `null`, confidence to `0.0`, `match_method` to `null`, `needs_review` to true, include a `processing_error` flag, and put a concise, non-sensitive explanation in `note`. Do not add a top-level error field. Report errors explicitly without exposing secrets or stack traces, and write files atomically so a failure cannot leave truncated JSON.

## DeepSeek configuration

Read the API key from `DEEPSEEK_API_KEY` in the environment, optionally loaded from a local `.env` that remains ignored by Git. Add only placeholders to `.env.example`. If the key is absent or the service is unavailable, preserve valid output and mark affected lines for review rather than fabricating a match.

## Evaluation

Implement `evaluation/evaluate.py` as a separate tool. It may read the labeled files in `answer_key/` only when intentionally invoked; never use those files in `pipeline.py` or in matching logic. Evaluate only the RFQs with available ground truth and document the comparison rules.

Write `evaluation/results.json` with these numeric fields in the range 0–1: `threshold`, `extraction_accuracy`, `match_precision`, and `match_recall`; also include an `additional_metrics` object. Use `0.60` as the description-match threshold. Define extraction accuracy and the denominators for precision and recall clearly and consistently in the evaluator and README. Do not run the evaluator for this task.

## README

Use exactly these headings, in this order, with no additional top-level or second-level headings:

# Ridgeline Quote Desk
## Repository
## How to Run
## Approach
## Evaluation
## Production

Include:

- **Repository:** the public GitHub repository URL; use the known repository URL only if it is confirmed public.
- **How to Run:** commands for the pipeline and evaluator.
- **Approach:** extraction and matching flow, the most consequential design decision, and the DeepSeek model/provider and actual configured region or endpoint. Do not invent a region; say when one is not specified.
- **Evaluation:** metric definitions, threshold and rationale, small labeled-set limitations, and how to expand the evaluation set.
- **Production:** a Mermaid code block plus concise bullets covering each Part 4 point supplied in the assignment. Do not invent missing Part 4 requirements.

## Diagram

Create `RIS_workflow.png` as a readable diagram showing input formats, extraction, customer lookup, the ordered matching fallbacks, product-status/replacement validation, review/error handling, JSON outputs, and separate evaluation. Keep the README Mermaid diagram consistent with the PNG.

When finished, report the files changed and the commands documented for running the pipeline and evaluator. Do not execute them.