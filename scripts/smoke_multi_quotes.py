"""Opt-in live regression for the supplied pair. ADDED, NOT RUN.

Uses Sarvam credits and writes a synthetic comparison to the selected API's DB.
Run manually only when ready: venv/bin/python scripts/smoke_multi_quotes.py
"""
import os
from decimal import Decimal
from pathlib import Path

import httpx

base = os.getenv("PROCUREMENT_API_URL", "http://127.0.0.1:8001") + "/api"
fixtures = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "quotations"
with httpx.Client(timeout=720) as client:
    created = client.post(base + "/workspaces/draft")
    created.raise_for_status()
    path = base + "/workspaces/" + created.json()["workspace"]["id"]
    names = ["Apex_Containers.pdf", "PackCorp_India.xlsx"]
    files = [("files", (name, (fixtures / name).read_bytes())) for name in names]
    response = client.post(path + "/quotes", files=files)
    response.raise_for_status()
    result = response.json()
    assert result["added"] == 2 and result["errors"] == [], result["errors"]
    data = result["comparison"]
    assert len(data["rows"]) == 30 and data["stats"]["matched_item_count"] == 30
    workspace = data["workspace"]
    apex = next(q for q in workspace["quotes"] if q["source_file"] == names[0])
    pack = next(q for q in workspace["quotes"] if q["source_file"] == names[1])
    rows = {row["sku_id"]: row for row in data["rows"]}
    assert Decimal(rows["SKU-001"]["prices"][apex["vendor_id"]]["landed_price_per_box"]) == Decimal("13.60")
    assert Decimal(rows["SKU-011"]["prices"][pack["vendor_id"]]["quoted_price"]) == Decimal("28.5")
    assert data["supplier_contexts"][apex["vendor_id"]]["rebate"]["status"] == "below_threshold"
    print("PASS uploaded pair: 30 SKU joins, reordered Excel, PDF per-box freight, conditional rebate excluded below threshold")
    # Synthetic fixtures only: simulate the explicit buyer QA/terms decisions.
    for quote in workspace["quotes"]:
        quote["qa_passed"] = True
        quote["commercial_terms_confirmed"] = True
    saved = client.put(path, json=workspace)
    saved.raise_for_status()
    scenario = client.post(path + "/scenario", json={"question": "What is the lowest-cost split award among suppliers that passed quality checks?"})
    scenario.raise_for_status()
    award = scenario.json()
    assert award["status"] == "ok" and len(award["awards"]) == 2
    assert all(not group["rebate"]["applied"] for group in award["awards"])
    print("PASS deterministic chat award:", award["total"], "INR; all freight included, partial-award rebate excluded")
