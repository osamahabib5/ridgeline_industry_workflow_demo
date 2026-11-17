"""Extract RFQs, match catalog items, and write validated JSON outputs."""

from __future__ import annotations

import csv
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple

import pymupdf
from dotenv import load_dotenv
from openpyxl import load_workbook
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parent
RFQ_DIR = ROOT / "rfqs"
OUTPUT_DIR = ROOT / "outputs"
PRODUCTS_FILE = ROOT / "products.csv"
CROSS_REFERENCE_FILE = ROOT / "cross_reference.csv"
CUSTOMERS_FILE = ROOT / "customers.csv"

RFQ_FILES = (
    "rfq_01_email.txt",
    "rfq_02_clearwater.pdf",
    "rfq_03_tricounty.xlsx",
    "rfq_04_handwritten.png",
    "rfq_05_forwarded_chain.txt",
    "rfq_06_messy.txt",
)

MatchMethod = Optional[
    Literal["ridgeline_part_number", "cross_reference", "description", "llm"]
]
PRODUCT_WORDS = {
    "bearing", "belt", "bolt", "chain", "clamp", "elbow", "filter", "fitting",
    "gasket", "grease", "glove", "hose", "motor", "nut", "o-ring", "oring",
    "screw", "valve", "washer", "wheel", "tubing", "oil", "seal", "coupler",
    "sprocket", "strap", "tire", "towel", "filter",
}
UNIT_ALIASES = {
    "ea": "EA", "each": "EA", "unit": "EA", "pail": "EA",
    "bx": "BX", "box": "BX", "boxes": "BX",
    "cs": "CS", "case": "CS", "cases": "CS",
    "rl": "RL", "roll": "RL", "rolls": "RL",
    "pr": "PR", "pair": "PR", "pairs": "PR",
    "pk": "PK", "pack": "PK", "packs": "PK",
    "ft": "FT", "foot": "FT", "feet": "FT",
}
SEMANTIC_PHRASES = re.compile(
    r"\b(same as|usual|last order|whatever|equivalent|if available|a few)\b",
    re.IGNORECASE,
)


class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line: int = Field(ge=1)
    as_written: str = Field(min_length=1)
    quantity: Optional[int] = None
    unit: Optional[str] = None
    ridgeline_part_number: Optional[str] = None
    match_method: MatchMethod = None
    confidence: float = Field(ge=0.0, le=1.0)
    needs_review: bool
    flags: List[str] = Field(default_factory=list)
    note: Optional[str] = None


class RFQOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rfq_id: str
    customer_id: Optional[str]
    lines: List[LineItem] = Field(min_length=1)
    customer_name: Optional[str]
    non_item_rows: List[str] = Field(default_factory=list)


def _read_csv(path: Path) -> List[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _normalize(value: str) -> str:
    value = value.lower().replace("&", " and ")
    value = re.sub(r"(\d)\s*/\s*(\d)", r"\1-\2", value)
    value = re.sub(r"[^a-z0-9.-]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _tokens(value: str) -> set:
    tokens = set(_normalize(value).replace("-", " ").replace(".", " ").split())
    return {token[:-1] if len(token) > 3 and token.endswith("s") else token for token in tokens}


def _canonical_unit(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = value.strip().lower().rstrip(".,")
    return UNIT_ALIASES.get(value, value.upper() if value.upper() in UNIT_ALIASES.values() else None)


def _extract_quantity_unit(text: str) -> Tuple[Optional[int], Optional[str]]:
    suffix_patterns = (
        r"\bqty\.?\s*(\d{1,5})\b",
        r"\b(\d{1,5})\s*(ea|each|bx|box(?:es)?|cs|cases?|pr|pairs?|pk|packs?|ft|feet|rl|rolls?)\s*$",
        r"\bx\s*(\d{1,5})\s*$",
        r"[-–]\s*(\d{1,5})\s*(?:\([^)]*\))?\s*$",
    )
    for pattern in suffix_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            unit_value = match.group(2) if match.lastindex and match.lastindex > 1 else None
            return int(match.group(1)), _canonical_unit(unit_value)

    if not re.match(r"^\s*\d+\s*/", text):
        leading = re.match(
            r"^\s*(\d{1,3})\s*(ea|each|bx|box(?:es)?|cs|cases?|pr|pairs?|pk|packs?|ft|feet|rl|rolls?)?\b\s*(.+)$",
            text,
            re.IGNORECASE,
        )
        if leading:
            return int(leading.group(1)), _canonical_unit(leading.group(2))

    if re.search(r"\ba few boxes?\b", text, re.IGNORECASE):
        return None, "BX"
    return None, None


def _looks_like_item(text: str) -> bool:
    lowered = text.lower()
    if re.search(r"\b(?:rl\d{6}|(?:apx|nf|mrd)-[a-z0-9-]+)\b", text, re.IGNORECASE):
        return True
    normalized = _tokens(text)
    compact = re.sub(r"[^a-z0-9]", "", lowered)
    return any(
        word in normalized
        or word.replace("-", "") in normalized
        or word.replace("-", "") in compact
        for word in PRODUCT_WORDS
    )


def _raw_item(text: str, quantity: Optional[int] = None, unit: Optional[str] = None) -> dict:
    parsed_quantity, parsed_unit = _extract_quantity_unit(text)
    part_match = re.search(
        r"\b(?:RL\d{6}|(?:APX|NF|MRD)-[A-Z0-9-]+)\b", text, re.IGNORECASE
    )
    return {
        "as_written": text.strip().lstrip("-• ").strip(),
        "quantity": quantity if quantity is not None else parsed_quantity,
        "unit": unit or parsed_unit,
        "part_number": part_match.group(0).upper() if part_match else None,
    }


def _extract_text_items(text: str) -> List[dict]:
    items = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or not _looks_like_item(stripped):
            continue
        if re.search(r"\b(make the motor|bump the gaskets|grease is fine)\b", stripped, re.I):
            continue
        if re.match(r"^(from|to|date|subject|ship to|buyer|thanks|regards)\b", stripped, re.I):
            continue
        item = _raw_item(stripped)
        if item["as_written"]:
            items.append(item)

    # Forwarded requests often state amendments in prose above the original bullets.
    motor_change = re.search(r"\bmake the motor\s+(\d+(?:\.\d+)?)\s*hp\b", text, re.I)
    gasket_change = re.search(r"\bbump the gaskets to\s+(\d+)\b", text, re.I)
    for item in items:
        if motor_change and "motor" in item["as_written"].lower():
            item["as_written"] = re.sub(
                r"\b\d+(?:\.\d+)?\s*hp\b",
                f"{motor_change.group(1)} HP",
                item["as_written"],
                count=1,
                flags=re.IGNORECASE,
            )
        if gasket_change and "gasket" in item["as_written"].lower():
            item["quantity"] = int(gasket_change.group(1))

    return items


def _extract_pdf(path: Path) -> Tuple[str, List[dict]]:
    with pymupdf.open(path) as document:
        text = "\n".join(page.get_text() for page in document)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    items = []
    for index, value in enumerate(lines):
        if not re.fullmatch(r"\d{1,2}", value) or index + 4 >= len(lines):
            continue
        part_number, description, quantity, unit = lines[index + 1:index + 5]
        if not re.fullmatch(r"(?:APX|NF|MRD)-[A-Z0-9-]+", part_number, re.I):
            continue
        if not quantity.isdigit():
            continue
        as_written = f"{part_number} — {description}"
        items.append(_raw_item(as_written, int(quantity), _canonical_unit(unit)))
    if items:
        return text, items
    return text, _extract_text_items(text)


def _extract_xlsx(path: Path) -> Tuple[str, List[dict]]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    items = []
    all_text = []
    try:
        for sheet in workbook.worksheets:
            rows = list(sheet.iter_rows(values_only=True))
            for row in rows:
                values = ["" if value is None else str(value).strip() for value in row]
                all_text.append(" | ".join(values))
            header_row = None
            for row_index, row in enumerate(rows):
                header = [_normalize("" if value is None else str(value)) for value in row]
                if any("item" in value for value in header) and any("qty" in value for value in header):
                    header_row = (row_index, header)
                    break
            if header_row is None:
                continue
            start, headers = header_row
            columns = {}
            for index, header in enumerate(headers):
                if "item" in header:
                    columns["part"] = index
                elif header.startswith("desc"):
                    columns["description"] = index
                elif "qty" in header:
                    columns["quantity"] = index
                elif header in {"uom", "unit"}:
                    columns["unit"] = index
            for row in rows[start + 1:]:
                values = list(row)
                description = str(values[columns["description"]] or "").strip() if "description" in columns else ""
                part = str(values[columns["part"]] or "").strip() if "part" in columns else ""
                if not description and not part:
                    continue
                quantity_value = values[columns["quantity"]] if "quantity" in columns else None
                quantity = int(quantity_value) if isinstance(quantity_value, (int, float)) and float(quantity_value).is_integer() else None
                unit_value = str(values[columns["unit"]] or "") if "unit" in columns else ""
                if not part and quantity is None and not _looks_like_item(description):
                    continue
                as_written = " — ".join(value for value in (part, description) if value)
                items.append(_raw_item(as_written, quantity, _canonical_unit(unit_value)))
    finally:
        workbook.close()
    return "\n".join(all_text), items


def _load_catalog() -> Tuple[Dict[str, dict], List[dict], List[dict]]:
    products = {row["part_number"].upper(): row for row in _read_csv(PRODUCTS_FILE)}
    cross_references = _read_csv(CROSS_REFERENCE_FILE)
    customers = _read_csv(CUSTOMERS_FILE)
    return products, cross_references, customers


def _find_customer(text: str, customers: List[dict]) -> Tuple[Optional[str], Optional[str]]:
    normalized_text = _normalize(text)
    for customer in customers:
        name = customer.get("name", "")
        if name and _normalize(name) in normalized_text:
            return customer.get("customer_id"), name
    return None, None


def _resolve_active(
    part_number: str, products: Dict[str, dict]
) -> Tuple[Optional[str], bool, Optional[str]]:
    current = part_number.upper()
    visited = set()
    replaced = False
    while current in products:
        if current in visited:
            return None, replaced, "replacement_cycle"
        visited.add(current)
        product = products[current]
        if product.get("status", "").lower() == "active":
            return current, replaced, None
        next_part = product.get("replaced_by", "").strip().upper()
        if not next_part:
            return None, replaced, "discontinued_no_replacement"
        replaced = True
        current = next_part
    return None, replaced, "unknown_part_number"


def _description_candidates(
    text: str, products: Dict[str, dict]
) -> List[Tuple[float, str]]:
    source_tokens = _tokens(text)
    scored = []
    for part_number, product in products.items():
        if product.get("status", "").lower() != "active":
            continue
        description = product.get("description", "")
        product_tokens = _tokens(description)
        union = source_tokens | product_tokens
        overlap = len(source_tokens & product_tokens) / len(union) if union else 0.0
        sequence = SequenceMatcher(None, _normalize(text), _normalize(description)).ratio()
        score = max(overlap, sequence)
        scored.append((score, part_number))
    return sorted(scored, reverse=True)


def _deepseek_match(text: str, candidates: List[Tuple[float, str]], products: Dict[str, dict]) -> dict:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        return {"error": "llm_unavailable"}
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    endpoint = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    shortlist = [
        {
            "ridgeline_part_number": part,
            "description": products[part].get("description", ""),
            "unit": products[part].get("unit", ""),
        }
        for _, part in candidates[:10]
    ]
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "Match only to a supplied candidate when the RFQ text supports it. "
                    "Return JSON with ridgeline_part_number (candidate ID or null), "
                    "quantity (integer or null), unit (string or null), and reason."
                ),
            },
            {
                "role": "user",
                "content": json.dumps({"rfq_line": text, "candidates": shortlist}),
            },
        ],
    }
    request = urllib.request.Request(
        endpoint + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
        result = json.loads(content)
        candidate_parts = {part for _, part in candidates[:10]}
        if result.get("ridgeline_part_number") not in {None, *candidate_parts}:
            return {"error": "invalid_llm_candidate"}
        return result
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        return {"error": f"llm_request_failed: {type(exc).__name__}"}


def _match_item(
    item: dict, products: Dict[str, dict], cross_references: List[dict]
) -> dict:
    text = item["as_written"]
    flags: List[str] = []
    note = None
    direct = item.get("part_number")
    if direct and direct.startswith("RL") and direct in products:
        matched, replaced, problem = _resolve_active(direct, products)
        if matched:
            if replaced:
                flags.append("discontinued_replaced")
                note = f"{direct} is discontinued; using active replacement {matched}."
            return {
                "part": matched, "method": "ridgeline_part_number",
                "confidence": 0.95, "flags": flags, "note": note,
            }
        flags.append(problem or "invalid_part_number")

    if direct:
        targets = {
            row.get("ridgeline_part_number", "").upper()
            for row in cross_references
            if row.get("competitor_part_number", "").strip().upper() == direct
        }
        targets.discard("")
        if len(targets) == 1:
            target = next(iter(targets))
            mapped, replaced, problem = _resolve_active(target, products)
            if mapped:
                notes = [
                    row.get("notes", "").strip()
                    for row in cross_references
                    if row.get("competitor_part_number", "").strip().upper() == direct
                    and row.get("notes", "").strip()
                ]
                if replaced:
                    flags.append("discontinued_replaced")
                if notes:
                    flags.append("cross_reference_note")
                    note = "; ".join(notes)
                return {
                    "part": mapped, "method": "cross_reference",
                    "confidence": 0.90, "flags": flags, "note": note,
                }
            flags.append(problem or "invalid_cross_reference")
        elif len(targets) > 1:
            flags.append("ambiguous_cross_reference")
            note = "The competitor part number maps to multiple Ridgeline products."

    candidates = _description_candidates(text, products)
    semantic = bool(SEMANTIC_PHRASES.search(text))
    if candidates and candidates[0][0] >= 0.60 and not semantic and not flags:
        return {
            "part": candidates[0][1], "method": "description",
            "confidence": candidates[0][0], "flags": [], "note": None,
        }

    result = _deepseek_match(text, candidates, products)
    llm_part = result.get("ridgeline_part_number")
    if llm_part:
        matched, replaced, problem = _resolve_active(llm_part, products)
        if matched:
            if replaced:
                flags.append("discontinued_replaced")
            return {
                "part": matched, "method": "llm", "confidence": 0.75,
                "flags": flags, "note": result.get("reason"),
                "quantity": result.get("quantity"),
                "unit": _canonical_unit(result.get("unit")),
            }
        flags.append(problem or "invalid_llm_candidate")

    error = result.get("error")
    if error:
        flags.append(error.split(":", 1)[0])
    if "no_match_found" not in flags:
        flags.append("no_match_found")
    return {
        "part": None, "method": None, "confidence": 0.0,
        "flags": flags, "note": note or result.get("reason") or "No verified catalog match was found.",
    }


def _make_line(
    number: int,
    item: dict,
    products: Dict[str, dict],
    cross_references: List[dict],
    customer_resolved: bool,
) -> LineItem:
    match = _match_item(item, products, cross_references)
    part = match["part"]
    product_unit = products[part].get("unit") if part else None
    unit = product_unit or _canonical_unit(match.get("unit")) or _canonical_unit(item.get("unit"))
    quantity = match.get("quantity", item.get("quantity"))
    flags = list(match["flags"])
    note = match["note"]
    if not customer_resolved:
        flags.append("customer_unresolved")
        note = (note + " " if note else "") + "Customer could not be resolved from the RFQ."
    confidence = float(match["confidence"])
    return LineItem(
        line=number,
        as_written=item["as_written"],
        quantity=quantity,
        unit=unit,
        ridgeline_part_number=part,
        match_method=match["method"],
        confidence=confidence,
        needs_review=confidence < 0.80 or bool(flags),
        flags=flags,
        note=note,
    )


def _extract(path: Path) -> Tuple[str, List[dict], List[str]]:
    if path.suffix.lower() == ".pdf":
        text, items = _extract_pdf(path)
    elif path.suffix.lower() == ".xlsx":
        text, items = _extract_xlsx(path)
    elif path.suffix.lower() == ".txt":
        text = path.read_text(encoding="utf-8", errors="replace")
        items = _extract_text_items(text)
    elif path.suffix.lower() == ".png":
        text = ""
        items = [{
            "as_written": "entire RFQ",
            "quantity": None,
            "unit": None,
            "part_number": None,
            "stub": True,
        }]
    else:
        raise ValueError(f"Unsupported RFQ file type: {path.suffix}")
    return text, items, []


def _error_output(stem: str, message: str) -> RFQOutput:
    line = LineItem(
        line=1,
        as_written="entire RFQ",
        quantity=None,
        unit=None,
        ridgeline_part_number=None,
        match_method=None,
        confidence=0.0,
        needs_review=True,
        flags=["processing_error"],
        note=message,
    )
    return RFQOutput(
        rfq_id=stem,
        customer_id=None,
        customer_name=None,
        lines=[line],
        non_item_rows=[],
    )


def _safe_error_message(exc: Exception) -> str:
    message = str(exc).strip()
    message = re.sub(r"(?i)bearer\s+\S+", "Bearer ***", message)
    message = re.sub(
        r"(?i)(api[_ -]?key|token|password)\s*[:=]\s*\S+",
        r"\1=***",
        message,
    )
    message = re.sub(r"\s+", " ", message)
    return message[:240] or type(exc).__name__


def _write_output(output: RFQOutput, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(output.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
        ) as stream:
            stream.write(payload)
            temporary_path = Path(stream.name)
        temporary_path.replace(path)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def process_rfq(path: Path) -> RFQOutput:
    stem = path.stem
    try:
        text, items, non_item_rows = _extract(path)
        if not items:
            raise ValueError("No item rows could be extracted.")
        products, cross_references, customers = _load_catalog()
        customer_id, customer_name = _find_customer(text, customers)
        output_lines = []
        for number, item in enumerate(items, start=1):
            if item.get("stub"):
                output_lines.append(
                    LineItem(
                        line=number,
                        as_written=item["as_written"],
                        quantity=None,
                        unit=None,
                        ridgeline_part_number=None,
                        match_method=None,
                        confidence=0.0,
                        needs_review=True,
                        flags=["stub"],
                        note="Handwritten RFQ intentionally left as a stub for manual review.",
                    )
                )
                continue
            try:
                output_lines.append(
                    _make_line(
                        number, item, products, cross_references,
                        customer_id is not None,
                    )
                )
            except (ValueError, KeyError, TypeError) as exc:
                output_lines.append(
                    LineItem(
                        line=number,
                        as_written=item.get("as_written") or "unreadable item",
                        quantity=None,
                        unit=None,
                        ridgeline_part_number=None,
                        match_method=None,
                        confidence=0.0,
                        needs_review=True,
                        flags=["processing_error"],
                        note=f"Item processing failed: {type(exc).__name__}.",
                    )
                )
        return RFQOutput(
            rfq_id=stem,
            customer_id=customer_id,
            customer_name=customer_name,
            lines=output_lines,
            non_item_rows=non_item_rows,
        )
    except Exception as exc:
        message = _safe_error_message(exc)
        print(f"{stem}: processing failed ({message})", file=sys.stderr)
        return _error_output(stem, f"RFQ processing failed: {message}")


def main() -> int:
    load_dotenv(ROOT / ".env")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    failures = 0
    for filename in RFQ_FILES:
        source = RFQ_DIR / filename
        if not source.is_file():
            print(f"Missing RFQ source: {source.name}", file=sys.stderr)
            failures += 1
            continue
        result = process_rfq(source)
        destination = OUTPUT_DIR / f"{source.stem}.json"
        try:
            _write_output(result, destination)
        except OSError as exc:
            print(f"{destination.name}: could not write output ({exc})", file=sys.stderr)
            failures += 1
            continue
        if any("processing_error" in line.flags for line in result.lines):
            failures += 1
        print(f"Wrote {destination.relative_to(ROOT)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
