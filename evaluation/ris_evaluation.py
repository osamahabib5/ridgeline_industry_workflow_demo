"""Evaluate the three labeled Ridgeline RFQs against existing pipeline outputs."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
KEY_DIR = ROOT / "answer_key"
OUTPUT_DIR = ROOT / "outputs"
RESULTS_PATH = ROOT / "evaluation" / "results.json"
LABELED_RFQS = ("rfq_01_email", "rfq_02_clearwater", "rfq_03_tricounty")
EXTRACTION_FIELDS = ("as_written", "quantity", "unit")
MATCH_THRESHOLD = 0.80
_INVALID = object()


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _line_map(data: dict, path: Path, is_answer_key: bool) -> Dict[int, dict]:
    lines = data.get("lines")
    if not isinstance(lines, list):
        raise ValueError(f"{path} must contain a lines array")

    by_number: Dict[int, dict] = {}
    for index, line in enumerate(lines):
        if not isinstance(line, dict):
            raise ValueError(f"{path}: lines[{index}] must be an object")
        number = line.get("line")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ValueError(f"{path}: lines[{index}].line must be a positive integer")
        if number in by_number:
            raise ValueError(f"{path}: duplicate line number {number}")

        if is_answer_key:
            missing = [field for field in (*EXTRACTION_FIELDS, "expected_part_number") if field not in line]
            if missing:
                raise ValueError(f"{path}: line {number} is missing {', '.join(missing)}")
            expected_part = line["expected_part_number"]
            if expected_part is not None and not isinstance(expected_part, str):
                raise ValueError(f"{path}: line {number}.expected_part_number must be a string or null")
            if not isinstance(line["as_written"], str):
                raise ValueError(f"{path}: line {number}.as_written must be a string")
        else:
            needs_review = line.get("needs_review")
            if not isinstance(needs_review, bool):
                raise ValueError(f"{path}: line {number}.needs_review must be a boolean")
            if "ridgeline_part_number" not in line or (
                line["ridgeline_part_number"] is not None
                and not isinstance(line["ridgeline_part_number"], str)
            ):
                raise ValueError(f"{path}: line {number}.ridgeline_part_number must be a string or null")
        by_number[number] = line
    return by_number


def _normalize_text(value: object) -> object:
    if value is None:
        return None
    if not isinstance(value, str):
        return _INVALID
    return re.sub(r"\s+", " ", value).strip().casefold()


def _normalize_quantity(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, bool):
        return _INVALID
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return _INVALID
    return number if number.is_finite() else _INVALID


def _field_matches(field: str, expected: object, actual: object) -> bool:
    if field == "quantity":
        normalized_expected = _normalize_quantity(expected)
        normalized_actual = _normalize_quantity(actual)
    else:
        normalized_expected = _normalize_text(expected)
        normalized_actual = _normalize_text(actual)
    return normalized_expected is not _INVALID and normalized_expected == normalized_actual


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _line_diagnostic(
    stem: str,
    number: int,
    expected: Optional[dict],
    actual: Optional[dict],
) -> Optional[dict]:
    issues = []
    mismatched_fields = []
    missing_fields = []

    if expected is None:
        issues.append("unexpected_output_line")
    elif actual is None:
        issues.append("missing_output_line")
        mismatched_fields.extend(EXTRACTION_FIELDS)
    else:
        for field in EXTRACTION_FIELDS:
            if field not in actual:
                missing_fields.append(field)
                mismatched_fields.append(field)
            elif not _field_matches(field, expected[field], actual[field]):
                mismatched_fields.append(field)
        if missing_fields:
            issues.append("missing_output_field")
        if any(field not in missing_fields for field in mismatched_fields):
            issues.append("extraction_mismatch")

        expected_part = expected["expected_part_number"]
        actual_part = actual["ridgeline_part_number"]
        if expected_part is None and actual_part is not None:
            issues.append("unexpected_part_number")
        elif expected_part is not None and actual_part is None:
            issues.append("missing_part_number")
        elif expected_part is not None and actual_part != expected_part:
            issues.append("incorrect_part_number")
        elif expected_part is not None and actual["needs_review"]:
            issues.append("correct_part_number_needs_review")
        elif expected_part is None and not actual["needs_review"]:
            issues.append("auto_approved_without_expected_match")

    if not issues:
        return None

    expected_item = None
    if expected is not None:
        expected_item = {
            "as_written": expected["as_written"],
            "quantity": expected["quantity"],
            "unit": expected["unit"],
            "ridgeline_part_number": expected["expected_part_number"],
        }

    actual_item = None
    if actual is not None:
        actual_item = {
            "as_written": actual.get("as_written"),
            "quantity": actual.get("quantity"),
            "unit": actual.get("unit"),
            "ridgeline_part_number": actual["ridgeline_part_number"],
            "needs_review": actual["needs_review"],
        }

    return {
        "rfq_id": stem,
        "line": number,
        "issues": issues,
        "mismatched_extraction_fields": mismatched_fields,
        "missing_output_fields": missing_fields,
        "expected": expected_item,
        "actual": actual_item,
    }


def _evaluate_rfq(stem: str) -> dict:
    key_path = KEY_DIR / f"{stem}.json"
    output_path = OUTPUT_DIR / f"{stem}.json"
    expected = _line_map(_read_json(key_path), key_path, is_answer_key=True)
    predicted = _line_map(_read_json(output_path), output_path, is_answer_key=False)

    field_correct = {field: 0 for field in EXTRACTION_FIELDS}
    field_total = {field: 0 for field in EXTRACTION_FIELDS}
    true_positives = 0
    auto_approved = 0
    sent_for_review = 0

    for number, answer_line in expected.items():
        prediction = predicted.get(number)
        for field in EXTRACTION_FIELDS:
            field_total[field] += 1
            if prediction is not None and field in prediction and _field_matches(
                field, answer_line[field], prediction[field]
            ):
                field_correct[field] += 1

        expected_part = answer_line["expected_part_number"]
        if expected_part is not None:
            if (
                prediction is not None
                and prediction["needs_review"] is False
                and prediction["ridgeline_part_number"] == expected_part
            ):
                true_positives += 1

    for prediction in predicted.values():
        if prediction["needs_review"] is False:
            auto_approved += 1
        else:
            sent_for_review += 1

    missing_lines = len(expected.keys() - predicted.keys())
    extra_lines = len(predicted.keys() - expected.keys())
    false_positives = auto_approved - true_positives
    expected_matches = sum(
        line["expected_part_number"] is not None for line in expected.values()
    )
    false_negatives = expected_matches - true_positives
    extraction_correct = sum(field_correct.values())
    extraction_total = sum(field_total.values())
    item_mismatches = []
    for number in sorted(expected):
        diagnostic = _line_diagnostic(
            stem, number, expected[number], predicted.get(number)
        )
        if diagnostic is not None:
            item_mismatches.append(diagnostic)
    for number in sorted(predicted.keys() - expected.keys()):
        diagnostic = _line_diagnostic(stem, number, None, predicted[number])
        if diagnostic is not None:
            item_mismatches.append(diagnostic)

    return {
        "extraction_correct": extraction_correct,
        "extraction_total": extraction_total,
        "field_correct": field_correct,
        "field_total": field_total,
        "true_positives": true_positives,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "auto_approved": auto_approved,
        "sent_for_review": sent_for_review,
        "missing_output_lines": missing_lines,
        "extra_output_lines": extra_lines,
        "item_mismatches": item_mismatches,
        "item_mismatch_count": len(item_mismatches),
        "extraction_accuracy": _ratio(extraction_correct, extraction_total),
        "match_precision": _ratio(true_positives, true_positives + false_positives),
        "match_recall": _ratio(true_positives, true_positives + false_negatives),
    }


def calculate_metrics() -> dict:
    per_rfq = {stem: _evaluate_rfq(stem) for stem in LABELED_RFQS}
    totals = {
        key: sum(rfq[key] for rfq in per_rfq.values())
        for key in (
            "extraction_correct",
            "extraction_total",
            "true_positives",
            "false_positives",
            "false_negatives",
            "auto_approved",
            "sent_for_review",
            "missing_output_lines",
            "extra_output_lines",
        )
    }
    per_field_correct = {
        field: sum(rfq["field_correct"][field] for rfq in per_rfq.values())
        for field in EXTRACTION_FIELDS
    }
    per_field_total = {
        field: sum(rfq["field_total"][field] for rfq in per_rfq.values())
        for field in EXTRACTION_FIELDS
    }

    return {
        "threshold": MATCH_THRESHOLD,
        "extraction_accuracy": _ratio(totals["extraction_correct"], totals["extraction_total"]),
        "match_precision": _ratio(
            totals["true_positives"],
            totals["true_positives"] + totals["false_positives"],
        ),
        "match_recall": _ratio(
            totals["true_positives"],
            totals["true_positives"] + totals["false_negatives"],
        ),
        "additional_metrics": {
            "labeled_rfqs": len(LABELED_RFQS),
            "true_positives": totals["true_positives"],
            "false_positives": totals["false_positives"],
            "false_negatives": totals["false_negatives"],
            "auto_approved": totals["auto_approved"],
            "sent_for_review": totals["sent_for_review"],
            "missing_output_lines": totals["missing_output_lines"],
            "extra_output_lines": totals["extra_output_lines"],
            "item_mismatch_count": sum(
                len(rfq["item_mismatches"]) for rfq in per_rfq.values()
            ),
            "item_mismatches": [
                diagnostic
                for stem in LABELED_RFQS
                for diagnostic in per_rfq[stem]["item_mismatches"]
            ],
            "extraction_fields_compared": totals["extraction_total"],
            "per_field_extraction_accuracy": {
                field: _ratio(per_field_correct[field], per_field_total[field])
                for field in EXTRACTION_FIELDS
            },
            "per_rfq": {
                stem: {
                    key: metrics[key]
                    for key in (
                        "extraction_accuracy",
                        "match_precision",
                        "match_recall",
                        "true_positives",
                        "false_positives",
                        "false_negatives",
                        "missing_output_lines",
                        "extra_output_lines",
                        "item_mismatch_count",
                    )
                }
                for stem, metrics in per_rfq.items()
            },
        },
    }


def _write_results(results: dict) -> None:
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=RESULTS_PATH.parent,
            prefix="results.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(results, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, RESULTS_PATH)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def main() -> int:
    try:
        results = calculate_metrics()
        _write_results(results)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Evaluation failed: {exc}", file=sys.stderr)
        return 1

    print(
        "Evaluation complete: "
        f"precision={results['match_precision']:.3f} "
        f"recall={results['match_recall']:.3f} "
        f"extraction_accuracy={results['extraction_accuracy']:.3f} "
        f"(TP={results['additional_metrics']['true_positives']}, "
        f"FP={results['additional_metrics']['false_positives']}, "
        f"FN={results['additional_metrics']['false_negatives']}, "
        f"item_mismatches={results['additional_metrics']['item_mismatch_count']})"
    )
    print(f"Wrote {RESULTS_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
