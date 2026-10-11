from decimal import Decimal
from itertools import product

import pytest

from app.engine import calculate, comparison, rate_for
from app.models import LineItem, Quote, ScenarioPlan, SKU, Workspace
from app.sample import sample_workspace


def sku(id="A", weight="0.5", quantity=10):
    return SKU(sku_id=id, description="Carton", weight_kg=weight, quantity=quantity)


def quote(id, rates, freight="0", **kwargs):
    return Quote(vendor_id=id, vendor_name=id, qa_passed=True, commercial_terms_confirmed=True, freight_per_order=freight, line_items=[LineItem(sku_id=k, quoted_price=v, price_unit="per_box") for k, v in rates.items()], **kwargs)


def test_kg_conversion_discount_and_per_box_freight_are_exact():
    q = quote("v", {"A": "42"})
    q.line_items[0].price_unit = "per_kg"
    q.line_items[0].discount_percent = Decimal("5")
    q.line_items[0].freight_per_box = Decimal("1.2")
    rate, reason, _ = rate_for(sku(), q)
    assert rate == Decimal("21.15")
    assert reason is None


def test_fixed_freight_changes_optimal_award_and_is_charged_once():
    w = Workspace(id="test", skus=[sku("A"), sku("B")], quotes=[quote("expensive-freight", {"A": "1", "B": "100"}, "1000"), quote("included", {"A": "2", "B": "2"})])
    best = calculate(w, ScenarioPlan())
    assert best["total"] == "40.00"
    assert [a["vendor_id"] for a in best["awards"]] == ["included"]
    cheapest_rows = calculate(w, ScenarioPlan(kind="lowest_per_sku"))
    assert cheapest_rows["total"] == "1030.00"


def test_split_charges_freight_once_per_used_vendor():
    w = Workspace(id="test", skus=[sku("A"), sku("B"), sku("C")], quotes=[quote("a", {"A": "1", "B": "1", "C": "100"}, "5"), quote("b", {"A": "100", "B": "100", "C": "1"}, "7")])
    result = calculate(w, ScenarioPlan())
    assert result["total"] == "42.00"
    assert sum(Decimal(a["freight"]) for a in result["awards"]) == 12


@pytest.mark.parametrize("field", ["qa", "commercial", "weight", "historical", "currency", "review"])
def test_unresolved_or_unapproved_quotes_do_not_enter_awards(field):
    s, q = sku(), quote("v", {"A": "10"})
    if field == "qa": q.qa_passed = None
    if field == "commercial": q.commercial_terms_confirmed = False
    if field == "weight": s.weight_kg = None; q.line_items[0].price_unit = "per_kg"
    if field == "historical": q.line_items[0].quoted_price = None
    if field == "currency": q.currency = "USD"
    if field == "review": q.line_items[0].needs_review = True
    w = Workspace(id="test", skus=[s], quotes=[q])
    assert calculate(w, ScenarioPlan())["status"] == "infeasible"


def test_tax_and_quantity_scaling_do_not_round_unit_rates():
    w = Workspace(id="test", skus=[sku(quantity=1000)], quotes=[quote("v", {"A": "1.2345"}, "10", tax_percent="18")])
    result = calculate(w, ScenarioPlan(include_tax=True, quantity_multiplier="2"))
    assert result["total"] == "2925.22"
    with pytest.raises(ValueError, match="fractional"):
        calculate(w, ScenarioPlan(quantity_multiplier="0.0001"))


def test_subset_solver_matches_bruteforce_assignments():
    w = Workspace(id="test", skus=[sku("A", quantity=1), sku("B", quantity=1), sku("C", quantity=1)], quotes=[quote("a", {"A": "2", "B": "9", "C": "3"}, "4"), quote("b", {"A": "8", "B": "1", "C": "5"}, "2"), quote("c", {"A": "3", "B": "3", "C": "3"}, "1")])
    for limit in (1, 2, 3):
        totals = []
        for assignment in product(w.quotes, repeat=3):
            used = {q.vendor_id for q in assignment}
            if len(used) <= limit:
                totals.append(sum(rate_for(s, q)[0] for s, q in zip(w.skus, assignment)) + sum(q.freight_per_order for q in w.quotes if q.vendor_id in used))
        assert Decimal(calculate(w, ScenarioPlan(max_suppliers=limit))["total"]) == min(totals)


def test_unknown_freight_and_tax_are_not_silently_zero():
    q = quote("v", {"A": "10"})
    q.line_items[0].freight_per_box = None
    assert rate_for(sku(), q)[0] is None
    q.line_items[0].freight_per_box = Decimal("0")
    q.tax_percent = None
    w = Workspace(id="test", skus=[sku()], quotes=[q])
    assert calculate(w, ScenarioPlan())["total"] == "100.00"
    with pytest.raises(ValueError, match="Tax rates are unknown"):
        calculate(w, ScenarioPlan(include_tax=True))


def test_sample_has_30_skus_five_vendors_and_missing_history():
    c = comparison(sample_workspace())
    assert c["stats"]["sku_count"] == 30
    assert c["stats"]["vendor_count"] == 5
    assert c["stats"]["review_count"] == 20
    assert c["recommended"]["total"] == "4648443.90"
    assert c["recommended"]["savings"] == "71436.10"
    assert all(a["vendor_id"] not in ("vendor-3", "vendor-5") for a in c["recommended"]["awards"])
