"""Pending regression coverage for the actual supplied PDF/XLSX pair.

These tests were added without executing them, per the user's manual-test request.
Provider terms are mocked; native file reads, matching, pricing and persistence
use the production code. The optional live script covers the provider boundary.
"""
from decimal import Decimal
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app import settings
from app.engine import calculate, comparison, rebate_for
from app.main import app
from app.matching import match_quote
from app.models import ExtractedQuote, LineItem, Quote, Rebate, SKU, ScenarioPlan, Workspace
from app.table_extraction import table_lines, unit
from app.parsers import parse_document
from app.ai import AIError, structured

FIXTURES = Path(__file__).parent / "fixtures" / "quotations"
APEX_RATES = ["11.8", "12.2", "12.6", "13", "13.4", "13.8", "14.2", "14.6", "15", "15.4", "27.9", "28.4", "28.9", "29.4", "29.9", "30.4", "30.9", "31.4", "31.9", "32.4", "61.5", "62.4", "63.3", "64.2", "65.1", "66", "66.9", "67.8", "68.7", "69.6"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "sqlite")
    monkeypatch.setattr(settings, "SQLITE_PATH", str(tmp_path / "comparison.db"))
    def provider(_prompt, payload, _schema):
        apex = payload["source_file"] == "Apex_Containers.pdf"
        rows = [LineItem.model_validate(row) for row in payload["native_table_rows"]]
        for row in rows:
            row.freight_per_box = Decimal("0")
            # Deliberately wrong blanket discount: the explicit footnote must
            # replace it with an actual order-level conditional rule.
            row.discount_percent = Decimal("5") if apex else Decimal("0")
        return Quote(vendor_id="pending", vendor_name="Apex Containers" if apex else "PackCorp India", currency="INR", payment_terms="Net 60 days", freight_per_order="0", tax_percent=None, certifications=["ISO 9001", "FSC"], line_items=rows)
    monkeypatch.setattr("app.ai.structured", provider)
    with TestClient(app) as result:
        yield result


def upload_pair(client):
    created = client.post("/api/workspaces/draft").json()
    path = "/api/workspaces/" + created["workspace"]["id"]
    files = [("files", (name, (FIXTURES / name).read_bytes(), mime)) for name, mime in (("Apex_Containers.pdf", "application/pdf"), ("PackCorp_India.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))]
    response = client.post(path + "/quotes", files=files)
    assert response.status_code == 200
    return path, response.json()


def test_pdf_excel_all_30_items_join_by_code_and_rebate_stays_off(client):
    path, result = upload_pair(client)
    assert result["added"] == 2
    assert result["errors"] == []
    data = result["comparison"]
    assert data["stats"]["matched_item_count"] == 30
    assert len(data["rows"]) == 30
    apex, pack = data["workspace"]["quotes"]
    assert all(row["prices"][q["vendor_id"]]["match_status"] == "sku" for row in data["rows"] for q in (apex, pack))
    for index, row in enumerate(data["rows"]):
        assert row["sku_id"] == f"SKU-{index+1:03}"
        assert Decimal(row["prices"][apex["vendor_id"]]["quoted_price"]) == Decimal(APEX_RATES[index])
        assert Decimal(row["prices"][apex["vendor_id"]]["landed_price_per_box"]) == Decimal(APEX_RATES[index]) + Decimal("1.8")
    # Excel starts with SKU-011, not SKU-001: these assertions catch row joins.
    sku1, sku11 = data["rows"][0], data["rows"][10]
    assert Decimal(sku1["prices"][pack["vendor_id"]]["quoted_price"]) == 12
    assert Decimal(sku11["prices"][pack["vendor_id"]]["quoted_price"]) == Decimal("28.5")
    assert data["supplier_contexts"][apex["vendor_id"]]["rebate"]["status"] == "below_threshold"
    assert data["supplier_contexts"][apex["vendor_id"]]["total"] == "22422500.00"
    assert data["supplier_contexts"][pack["vendor_id"]]["total"] == "21977500.00"
    assert all(line["discount_percent"] == "0" for line in apex["line_items"])
    assert apex["rebate"]["minimum_order_value"] == "25000000"
    assert apex["rebate"]["threshold_operator"] == "gt"
    assert apex["rebate"]["requires_all_skus"] is True
    assert apex["rebate"]["applies_to"] == "goods_and_freight"
    assert sku1["lowest_vendor_ids"] == [pack["vendor_id"]]
    assert data["rows"][20]["lowest_vendor_ids"] == [apex["vendor_id"]]
    for record in result["files"]:
        assert record["status"] == "completed"
        assert record["matched_count"] == 30
    assert client.get(path).json()["stats"]["matched_item_count"] == 30


def test_award_and_chat_use_both_quotes_with_exact_freight(client, monkeypatch):
    path, result = upload_pair(client)
    workspace = result["comparison"]["workspace"]
    for quote in workspace["quotes"]:
        quote["qa_passed"] = True
        quote["commercial_terms_confirmed"] = True
    assert client.put(path, json=workspace).status_code == 200
    monkeypatch.setattr("app.main.plan_question", lambda _question, _workspace: ScenarioPlan(kind="split_award"))
    monkeypatch.setattr("app.rfx.explain_analysis", lambda *_args: {"answer": "Computed award is shown in the table.", "observations": [], "caveats": []})
    award = client.post(path + "/scenario", json={"question": "Lowest-cost split award among suppliers that passed quality checks?"}).json()
    assert award["status"] == "ok"
    assert award["total"] == "21742500.00"
    assert len(award["awards"]) == 2
    apex = next(q for q in workspace["quotes"] if q["source_file"] == "Apex_Containers.pdf")
    selected = [line for line in award["lines"] if line["vendor_id"] == apex["vendor_id"]]
    apex_award = next(group for group in award["awards"] if group["vendor_id"] == apex["vendor_id"])
    assert Decimal(apex_award["freight"]) == sum(Decimal("1.8") * line["quantity"] for line in selected)
    assert apex_award["rebate"]["applied"] is False
    assert apex_award["rebate"]["status"] == "partial_award"
    assert Decimal(award["total"]) == sum(Decimal(group["total"]) for group in award["awards"])
    exported = client.get(path + "/export").text
    assert "landed INR/box" in exported and "Apex_Containers.pdf" in exported


def test_one_failure_preserves_success_and_retry_reuses_failed_record(client, monkeypatch):
    original = __import__("app.main", fromlist=["extract_quote"]).extract_quote
    fail = True
    def extract(filename, data, workspace):
        if filename.endswith(".pdf") and fail:
            raise ValueError("Unreadable PDF")
        return original(filename, data, workspace)
    monkeypatch.setattr("app.main.extract_quote", extract)
    path, result = upload_pair(client)
    assert result["added"] == 1
    assert result["files"][0]["status"] == "failed"
    assert result["files"][1]["status"] == "completed"
    failed_id = result["files"][0]["id"]
    retained_vendor = result["comparison"]["workspace"]["quotes"][0]["vendor_id"]
    fail = False
    retry = client.post(path + "/quotes", data={"retry_upload_id": failed_id}, files={"files": ("Apex_Containers.pdf", (FIXTURES / "Apex_Containers.pdf").read_bytes(), "application/pdf")}).json()
    assert retry["added"] == 1
    assert retry["files"][0]["id"] == failed_id
    assert retry["files"][0]["status"] == "completed"
    assert retained_vendor in {q["vendor_id"] for q in retry["comparison"]["workspace"]["quotes"]}
    assert len(retry["comparison"]["workspace"]["uploads"]) == 2


def test_same_successful_file_is_not_duplicated_on_retry(client):
    path, _ = upload_pair(client)
    repeated = client.post(path + "/quotes", files={"files": ("PackCorp_India.xlsx", (FIXTURES / "PackCorp_India.xlsx").read_bytes())}).json()
    assert repeated["added"] == 0
    assert len(repeated["comparison"]["workspace"]["quotes"]) == 2


def test_strict_threshold_and_full_sku_requirement():
    quote = Quote(vendor_id="apex", vendor_name="Apex", line_items=[], rebate=Rebate(percent="5", minimum_order_value="25000000", threshold_operator="gt", applies_to="goods_and_freight", requires_all_skus=True, required_sku_ids=["A", "B"], conditions_supported=True))
    exact = rebate_for(quote, Decimal("25000000"), Decimal("25000000"), Decimal("100"), {"A", "B"}, {"A", "B"})
    assert exact["applied"] is False
    above = rebate_for(quote, Decimal("25000001"), Decimal("25000001"), Decimal("100"), {"A", "B"}, {"A", "B"})
    assert above["applied"] is True
    assert Decimal(above["amount"]) == Decimal("1250005.05")
    partial = rebate_for(quote, Decimal("30000000"), Decimal("30000000"), Decimal("100"), {"A"}, {"A", "B"})
    assert partial["status"] == "partial_award"


def test_ambiguous_matches_missing_items_and_units_are_flagged():
    workspace = Workspace(id="test", skus=[SKU(sku_id="SKU-001", description="Small box", quantity=10), SKU(sku_id="SKU_001", description="Large box", quantity=10)])
    quote = Quote(vendor_id="q", vendor_name="Supplier", line_items=[LineItem(sku_id="sku 001", quoted_price="42", price_unit="per_kg")])
    match_quote(quote, workspace)
    assert quote.line_items == []
    assert len(quote.unmatched_items) == 1
    assert quote.unmatched_items[0].needs_review
    assert set(quote.unmatched_items[0].match_candidates) == {"SKU-001", "SKU_001"}
    workspace.quotes = [quote]
    result = comparison(workspace)
    assert all(row["prices"]["q"]["landed_price_per_box"] is None for row in result["rows"])


def test_duplicate_source_prices_are_preserved_for_explicit_review():
    workspace = Workspace(id="test", skus=[SKU(sku_id="SKU-001", description="Box", quantity=10)])
    quote = ExtractedQuote(vendor_id="q", vendor_name="Supplier", line_items=[LineItem(sku_id="SKU-001", quoted_price=price, price_unit="per_box") for price in ("10", "12", "14")])
    match_quote(quote, workspace)
    assert quote.line_items == []
    assert len(quote.unmatched_items) == 3
    assert all(line.needs_review for line in quote.unmatched_items)


def test_partial_rebate_allocation_is_not_a_greedy_row_minimum():
    skus = [SKU(sku_id=identifier, description=identifier, quantity=1) for identifier in ("A", "B", "C")]
    def vendor(identifier, rates, rebate=None):
        return Quote(vendor_id=identifier, vendor_name=identifier, qa_passed=True, commercial_terms_confirmed=True, rebate=rebate, line_items=[LineItem(sku_id=sku, quoted_price=price, price_unit="per_box") for sku, price in rates.items()])
    a = vendor("a", {"A": "6", "B": "6", "C": "20"}, Rebate(percent="50", minimum_order_value="12", threshold_operator="gte", conditions_supported=True))
    b = vendor("b", {"A": "5", "B": "8", "C": "1"})
    result = calculate(Workspace(id="test", skus=skus, quotes=[a, b]), ScenarioPlan())
    assert result["total"] == "7.00"
    assert next(award for award in result["awards"] if award["vendor_id"] == "a")["rebate"]["applied"]


@pytest.mark.parametrize(("original", "normalized"), [("INR/Box", "per_box"), ("per_kg", "per_kg"), ("INR per 1,000 boxes", "per_1000_boxes"), ("per_pallet", "unknown")])
def test_price_unit_aliases_preserve_unsupported_conversions(original, normalized):
    assert unit(original) == normalized


def test_dimensionless_supplied_workbook_retains_all_rows():
    data = (FIXTURES / "PackCorp_India.xlsx").read_bytes()
    text = parse_document("PackCorp_India.xlsx", data)
    rows = table_lines("PackCorp_India.xlsx", data, text)
    assert len(rows) == 30
    assert len({row.sku_id for row in rows}) == 30
    assert rows[0].sku_id == "SKU-011"
    assert next(row.quoted_price for row in rows if row.sku_id == "SKU-001") == Decimal("12")


def test_dimensionless_workbook_still_enforces_actual_row_limit():
    book = Workbook(write_only=True)
    sheet = book.create_sheet()
    for index in range(5001):
        sheet.append([f"SKU-{index}"])
    output = io.BytesIO()
    book.save(output)
    with pytest.raises(ValueError, match="5,000 rows"):
        parse_document("too-large.xlsx", output.getvalue())


def test_source_freight_is_applied_before_model_schema_validation(monkeypatch):
    raw = {
        "vendor_id": "pending", "vendor_name": "Apex",
        "freight_per_order": {"amount": "1.80", "unit": "per_box"},
        "source_evidence": {},
        "line_items": [{"sku_id": "SKU-001", "quoted_price": "11.80", "price_unit": "per_box", "freight_per_box": "INR 1.80 per box", "source_evidence": {}}],
    }
    monkeypatch.setattr(settings, "SARVAM_KEY", "test-key")
    monkeypatch.setattr("app.ai.request", lambda *_args, **_kwargs: {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(raw)}}]})
    text = "Freight of INR 1.80 per box is charged separately for every awarded box."
    quote = structured("Extract source values", {"document": text}, ExtractedQuote)
    assert quote.freight_per_order == Decimal("0")
    assert quote.line_items[0].freight_per_box == Decimal("1.80")
    assert "1.80 per box" in quote.source_evidence["freight_per_order"]
    # An invalid model amount with no proving source clause must still fail.
    with pytest.raises(AIError, match="freight_per_order"):
        structured("Extract source values", {"document": "Freight not stated."}, ExtractedQuote)
    # Fixing freight does not bypass validation for other extracted fields.
    raw["line_items"][0]["quoted_price"] = "not a price"
    with pytest.raises(AIError, match="quoted_price"):
        structured("Extract source values", {"document": text}, ExtractedQuote)
