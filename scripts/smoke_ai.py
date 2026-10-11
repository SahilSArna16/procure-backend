"""Opt-in, live provider smoke test; uses generated demo files and API credits."""
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.ai import extract_quote, plan_question
from app.sample import sample_workspace
from app.sample_files import generate_sample_zip

workspace = sample_workspace()
with zipfile.ZipFile(io.BytesIO(generate_sample_zip())) as archive:
    for name in (sys.argv[1:] or ["apex_quote.xlsx", "packwell_quote.pdf", "greenbox_quote.docx", "metro_email.txt", "shree_pricelist.png"]):
        print(f"Extracting {name}…", flush=True)
        quote = extract_quote(name, archive.read(name), workspace)
        count = sum(line.quoted_price is not None for line in quote.line_items)
        assert quote.qa_passed is None and not quote.commercial_terms_confirmed
        expected = 10 if name.endswith(".txt") else 30
        assert count == expected, f"Expected {expected} numeric rates, got {count}"
        if name.endswith(".pdf"):
            assert quote.freight_per_order == 7500, f"Expected freight 7500, got {quote.freight_per_order}; notes: {quote.notes}"
            assert all(line.discount_percent == 5 for line in quote.line_items), "PDF discount not captured on every SKU"
        if name.endswith(".png"):
            assert all(line.price_unit == "per_kg" for line in quote.line_items)
        print(f"PASS {name}: {count} numeric SKU rates; buyer review enforced", flush=True)
plan = plan_question("Split the order between at most two suppliers who passed QA and exclude Apex Containers.", workspace)
assert plan.kind == "split_award" and plan.qa_only and plan.max_suppliers == 2 and "vendor-1" in plan.excluded_vendor_ids
print("PASS natural-language constraints: QA, supplier exclusion, max two suppliers", flush=True)
