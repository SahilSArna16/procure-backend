"""SKU-based pricing and exact Decimal invoices, with conditional order rebates."""
from decimal import Decimal, ROUND_HALF_UP
from itertools import combinations

import pandas as pd

from .models import Quote, ScenarioPlan, SKU, Workspace

ZERO = Decimal("0")
ONE = Decimal("1")


def money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def rates_for(sku: SKU, quote: Quote):
    line = next((item for item in quote.line_items if item.sku_id == sku.sku_id), None)
    if line is None or line.quoted_price is None:
        return None, "Missing item or historical rate", line
    if line.needs_review or line.match_status in {"description", "uncertain", "unmatched"}:
        return None, "Item or match needs buyer review", line
    if quote.currency.upper() != "INR" or (line.currency or quote.currency).upper() != "INR":
        return None, "Currency conversion required", line
    if line.price_unit == "unknown":
        return None, "Price unit unresolved", line
    if line.price_unit == "per_kg" and sku.weight_kg is None:
        return None, "Standard SKU weight required", line
    if line.freight_per_box is None:
        return None, "Per-box freight requires confirmation", line
    if line.discount_conditions:
        return None, "Conditional line discount needs buyer review", line
    raw = line.quoted_price
    if line.price_unit == "per_kg":
        raw *= sku.weight_kg
    elif line.price_unit == "per_1000_boxes":
        raw /= 1000
    goods = raw * (ONE - line.discount_percent / 100)
    return (raw, goods, line.freight_per_box), None, line


def rate_for(sku: SKU, quote: Quote):
    rates, issue, line = rates_for(sku, quote)
    return (rates[1] + rates[2] if rates else None), issue, line


def normalized_frame(workspace: Workspace) -> pd.DataFrame:
    return pd.DataFrame([{"sku_id": s.sku_id, "vendor_id": q.vendor_id, "rate": rate_for(s, q)[0]} for s in workspace.skus for q in workspace.quotes], columns=["sku_id", "vendor_id", "rate"])


def rebate_for(quote: Quote, raw_goods: Decimal, net_goods: Decimal, freight: Decimal, awarded_ids: set[str], master_ids: set[str]):
    rebate = quote.rebate
    result = {"status": "none", "applied": False, "percent": "0", "amount": "0.00", "reason": "No order rebate", "source_reference": rebate.source_reference if rebate else ""}
    if rebate is None or rebate.percent == 0:
        return result
    result["percent"] = str(rebate.percent)
    if not rebate.conditions_supported:
        result.update(status="unsupported", reason="Rebate condition is unresolved; no rebate applied")
        return result
    required = set(rebate.required_sku_ids) or master_ids
    if rebate.requires_all_skus and not required.issubset(awarded_ids):
        result.update(status="partial_award", reason="Rebate requires every specified SKU in one supplier order")
        return result
    basis = {"base_goods": raw_goods, "net_goods": net_goods, "goods_and_freight": net_goods + freight}[rebate.threshold_basis]
    result["basis_value"] = money(basis)
    if rebate.minimum_order_value is not None:
        qualifies = basis > rebate.minimum_order_value if rebate.threshold_operator == "gt" else basis >= rebate.minimum_order_value
        if not qualifies:
            result.update(status="below_threshold", reason=f"Order value does not {'exceed' if rebate.threshold_operator == 'gt' else 'reach'} INR {rebate.minimum_order_value}; no rebate applied")
            return result
    amount = (net_goods + freight if rebate.applies_to == "goods_and_freight" else net_goods) * rebate.percent / 100
    result.update(status="applied", applied=True, amount=money(amount), reason="All rebate conditions satisfied for this awarded basket")
    return result


def invoice(quote: Quote, selected: list[SKU], quantities, prices, master_ids, include_tax=False):
    raw_goods = sum((prices[(s.sku_id, quote.vendor_id)][0] * quantities[s.sku_id] for s in selected), ZERO)
    net_goods = sum((prices[(s.sku_id, quote.vendor_id)][1] * quantities[s.sku_id] for s in selected), ZERO)
    per_box_freight = sum((prices[(s.sku_id, quote.vendor_id)][2] * quantities[s.sku_id] for s in selected), ZERO)
    freight = per_box_freight + quote.freight_per_order
    rebate = rebate_for(quote, raw_goods, net_goods, freight, {s.sku_id for s in selected}, master_ids)
    factor = ONE - quote.rebate.percent / 100 if rebate["applied"] else ONE
    freight_factor = factor if rebate["applied"] and quote.rebate.applies_to == "goods_and_freight" else ONE
    tax_factor = ONE + quote.tax_percent / 100 if include_tax else ONE
    final_goods = net_goods * factor * tax_factor
    final_freight = freight * freight_factor * tax_factor
    total = final_goods + final_freight
    lines = []
    for sku in selected:
        _, goods, shipping = prices[(sku.sku_id, quote.vendor_id)]
        rate = (goods * factor + shipping * freight_factor) * tax_factor
        lines.append({"sku_id": sku.sku_id, "vendor_id": quote.vendor_id, "quantity": quantities[sku.sku_id], "price_per_box": str(rate), "line_total": money(rate * quantities[sku.sku_id]), "source_file": quote.source_file})
    return total, {"vendor_id": quote.vendor_id, "vendor_name": quote.vendor_name, "sku_count": len(selected), "quantity": sum(quantities[s.sku_id] for s in selected), "subtotal": money(final_goods), "freight": money(final_freight), "total": money(total), "rebate": rebate, "source_file": quote.source_file}, lines


def comparison(workspace: Workspace) -> dict:
    frame = normalized_frame(workspace)
    matrix = frame.pivot(index="sku_id", columns="vendor_id", values="rate") if not frame.empty else pd.DataFrame()
    master_ids = {s.sku_id for s in workspace.skus}
    contexts = {}
    for quote in workspace.quotes:
        prices = {(s.sku_id, quote.vendor_id): rates_for(s, quote)[0] for s in workspace.skus}
        complete = bool(workspace.skus) and all(s.quantity is not None and prices[(s.sku_id, quote.vendor_id)] is not None for s in workspace.skus) and quote.freight_per_order is not None
        if complete:
            quantities = {s.sku_id: s.quantity for s in workspace.skus}
            _, group, _ = invoice(quote, workspace.skus, quantities, prices, master_ids)
            total_boxes = sum(quantities.values())
            rebate = group["rebate"]
            factor = ONE - quote.rebate.percent / 100 if rebate["applied"] else ONE
            freight_factor = factor if rebate["applied"] and quote.rebate.applies_to == "goods_and_freight" else ONE
            contexts[quote.vendor_id] = {"complete": True, "rebate": rebate, "goods_factor": factor, "freight_factor": freight_factor, "fixed_per_box": quote.freight_per_order / total_boxes, "total": group["total"]}
        else:
            contexts[quote.vendor_id] = {"complete": False, "rebate": {"status": "unresolved", "applied": False, "reason": "Complete quantities and comparable rates required to evaluate the order rebate"}, "total": None}
    rows, issues = [], []
    for sku in workspace.skus:
        cells, comparable = {}, []
        for quote in workspace.quotes:
            values, issue, line = rates_for(sku, quote)
            context = contexts[quote.vendor_id]
            landed = None
            if values is not None and context["complete"]:
                _, goods, freight = values
                landed = goods * context["goods_factor"] + (freight + context["fixed_per_box"]) * context["freight_factor"]
            elif values is not None and quote.freight_per_order == 0 and quote.rebate is None:
                landed = values[1] + values[2]
            elif values is not None:
                issue = "Complete basket needed for freight allocation or rebate eligibility" if quote.freight_per_order is not None else "Fixed freight requires confirmation"
            if issue:
                issues.append({"sku_id": sku.sku_id, "vendor_id": quote.vendor_id, "message": issue})
            raw_normalized = values[0] if values else None
            cell = {"price_per_box": str(matrix.at[sku.sku_id, quote.vendor_id]) if values else None, "quoted_price": str(line.quoted_price) if line and line.quoted_price is not None else None, "normalized_quoted_price": str(raw_normalized) if raw_normalized is not None else None, "landed_price_per_box": str(landed) if landed is not None else None, "price_unit": line.price_unit if line else None, "currency": (line.currency or quote.currency) if line else quote.currency, "quoted_quantity": str(line.quoted_quantity) if line and line.quoted_quantity is not None else None, "description": line.description if line else "", "discount_percent": str(line.discount_percent) if line else "0", "notes": line.notes_footnotes if line else "", "source_file": quote.source_file, "source_reference": line.source_reference if line else "", "source_evidence": line.source_evidence if line else {}, "source_sku_id": line.source_sku_id if line else None, "match_status": line.match_status if line else "missing", "rebate": context["rebate"], "issue": issue}
            cells[quote.vendor_id] = cell
            if landed is not None:
                comparable.append((landed, quote.vendor_id))
        lowest = min((rate for rate, _ in comparable), default=None)
        rows.append({**sku.model_dump(mode="json"), "prices": cells, "lowest_vendor_ids": [vendor for rate, vendor in comparable if rate == lowest]})
    for quote in workspace.quotes:
        issues.extend({"sku_id": line.source_sku_id or line.sku_id, "vendor_id": quote.vendor_id, "message": "Source item has no confirmed unique master match"} for line in quote.unmatched_items)
    return {"workspace": workspace.model_dump(mode="json"), "rows": rows, "issues": issues, "supplier_contexts": {identifier: {"complete": context["complete"], "total": context["total"], "rebate": context["rebate"]} for identifier, context in contexts.items()}, "single_vendor": calculate(workspace, ScenarioPlan(kind="compare_vendors")), "recommended": calculate(workspace, ScenarioPlan()), "stats": {"sku_count": len(workspace.skus), "vendor_count": len(workspace.quotes), "approved_vendor_count": sum(q.qa_passed is True for q in workspace.quotes), "total_quantity": sum(s.quantity or 0 for s in workspace.skus), "review_count": len(issues), "matched_item_count": sum(all(r["prices"][q.vendor_id]["match_status"] in {"sku", "confirmed"} for q in workspace.quotes) for r in rows) if workspace.quotes else 0}}


def calculate(workspace: Workspace, plan: ScenarioPlan) -> dict:
    def unavailable(message, status="infeasible", missing=None):
        return {"status": status, "summary": message, "missing_skus": missing or [], "awards": [], "total": None, "plan": plan.model_dump(mode="json")}
    known = {q.vendor_id for q in workspace.quotes}
    if any(v not in known for v in plan.excluded_vendor_ids) or (plan.vendor_id and plan.vendor_id not in known):
        raise ValueError("Unknown vendor in the scenario.")
    if plan.kind == "clarify":
        return unavailable(plan.explanation or "Ask for split award or supplier comparison.", "clarify")
    if not workspace.skus or any(s.quantity is None for s in workspace.skus):
        return unavailable("Confirm the master order quantities before calculating totals.", missing=[s.sku_id for s in workspace.skus if s.quantity is None])
    preview = plan.kind == "compare_vendors" and not plan.qa_only
    eligible = [q for q in workspace.quotes if q.vendor_id not in plan.excluded_vendor_ids and (not plan.qa_only or q.qa_passed is True) and (q.commercial_terms_confirmed or preview) and q.freight_per_order is not None]
    if plan.include_tax and any(q.tax_percent is None for q in eligible):
        raise ValueError("Tax rates are unknown for an eligible supplier. Confirm tax percentages before a tax-included calculation.")
    quantities = {}
    for sku in workspace.skus:
        scaled = Decimal(sku.quantity) * plan.quantity_multiplier
        if scaled != scaled.to_integral_value():
            raise ValueError(f"Quantity multiplier gives fractional boxes for {sku.sku_id}.")
        quantities[sku.sku_id] = int(scaled)
    prices = {}
    for quote in eligible:
        for sku in workspace.skus:
            values, _, _ = rates_for(sku, quote)
            if values is not None:
                prices[(sku.sku_id, quote.vendor_id)] = values
    master_ids = {s.sku_id for s in workspace.skus}

    def assignment(vendors, chosen=None):
        selected = {q.vendor_id: [] for q in vendors}
        for sku in workspace.skus:
            if chosen is None:
                options = []
                for q in vendors:
                    values = prices.get((sku.sku_id, q.vendor_id))
                    if values is None:
                        continue
                    rate = values[1] + values[2]
                    r = q.rebate
                    if r and r.conditions_supported and r.minimum_order_value is None and not r.requires_all_skus:
                        rate = values[1] * (ONE - r.percent / 100) + values[2] * (ONE - r.percent / 100 if r.applies_to == "goods_and_freight" else ONE)
                    if plan.include_tax:
                        rate *= ONE + q.tax_percent / 100
                    options.append((rate, q.vendor_id))
                if not options:
                    return None
                _, vendor_id = min(options)
            else:
                vendor_id = chosen[sku.sku_id]
                if (sku.sku_id, vendor_id) not in prices:
                    return None
            selected[vendor_id].append(sku)
        groups, lines, total = [], [], ZERO
        for quote in vendors:
            if not selected[quote.vendor_id]:
                continue
            amount, group, vendor_lines = invoice(quote, selected[quote.vendor_id], quantities, prices, master_ids, plan.include_tax)
            total += amount
            groups.append(group)
            lines.extend(vendor_lines)
        return total, groups, lines

    singles = {q.vendor_id: assignment([q]) for q in eligible}
    if plan.kind == "compare_vendors":
        results = [{"vendor_id": q.vendor_id, "vendor_name": q.vendor_name, "total": money(singles[q.vendor_id][0]) if singles[q.vendor_id] else None, "coverage": sum((s.sku_id, q.vendor_id) in prices for s in workspace.skus), "eligible": True, "provisional": not q.commercial_terms_confirmed, "rebate": singles[q.vendor_id][1][0]["rebate"] if singles[q.vendor_id] else None, "source_file": q.source_file} for q in eligible]
        results.sort(key=lambda r: (r["total"] is None, Decimal(r["total"] or "0")))
        return {"status": "ok", "summary": "Full-order landed totals, including freight and only eligible rebates. " + ("Unconfirmed quotes are provisional and require review before an award." if preview else "QA-approved, confirmed quotes only."), "vendors": results, "awards": [], "total": None, "plan": plan.model_dump(mode="json")}
    if plan.kind == "single_vendor":
        if not plan.vendor_id:
            raise ValueError("Choose a vendor for the single-vendor scenario.")
        subsets = [[q for q in eligible if q.vendor_id == plan.vendor_id]]
    elif plan.kind == "lowest_per_sku":
        if plan.max_suppliers != 10:
            raise ValueError("Supplier limits require the split-award optimizer.")
        subsets = [eligible]
    else:
        subsets = (subset for size in range(1, min(len(eligible), plan.max_suppliers) + 1) for subset in combinations(eligible, size))
    best = None
    for subset in subsets:
        candidate = assignment(subset)
        if candidate and (best is None or (candidate[0], len(candidate[1])) < (best[0], len(best[1]))):
            best = candidate
    if best is None:
        return unavailable("No complete award under these constraints. Confirm QA/terms and resolve missing prices, conversions or matches.", missing=[s.sku_id for s in workspace.skus if not any((s.sku_id, q.vendor_id) in prices for q in eligible)])

    # All-SKU-only rebates are covered exactly by single-vendor candidates. General
    # partial-order rebates need an exact assignment search, not a row-min heuristic.
    partial_rebates = False
    for q in eligible:
        r = q.rebate
        full_master_only = r and r.requires_all_skus and (set(r.required_sku_ids) or master_ids) == master_ids
        if not r or not r.conditions_supported or r.percent == 0 or full_master_only or (r.minimum_order_value is None and not r.requires_all_skus):
            continue
        covered = [s for s in workspace.skus if (s.sku_id, q.vendor_id) in prices]
        if covered and invoice(q, covered, quantities, prices, master_ids)[1]["rebate"]["applied"]:
            partial_rebates = True
    if partial_rebates and plan.kind == "split_award":
        options, lower = [], []
        for sku in workspace.skus:
            choices = []
            for q in eligible:
                values = prices.get((sku.sku_id, q.vendor_id))
                if values is None:
                    continue
                factor = ONE - q.rebate.percent / 100 if q.rebate and q.rebate.conditions_supported else ONE
                shipping_factor = factor if q.rebate and q.rebate.applies_to == "goods_and_freight" else ONE
                cost = (values[1] * factor + values[2] * shipping_factor) * quantities[sku.sku_id]
                if plan.include_tax:
                    cost *= ONE + q.tax_percent / 100
                choices.append((cost, q.vendor_id))
            choices.sort()
            options.append(choices)
            lower.append(choices[0][0])
        suffix = [ZERO] * (len(lower) + 1)
        for i in range(len(lower) - 1, -1, -1):
            suffix[i] = suffix[i + 1] + lower[i]
        nodes = 0
        chosen = {}
        def search(index, optimistic, used):
            nonlocal nodes, best
            nodes += 1
            if nodes > 100_000:
                raise RuntimeError("exact-search-limit")
            if optimistic + suffix[index] > best[0]:
                return
            if index == len(workspace.skus):
                result = assignment(eligible, chosen)
                if result and (result[0], len(result[1])) < (best[0], len(best[1])):
                    best = result
                return
            for cost, vendor_id in options[index]:
                next_used = used | {vendor_id}
                if len(next_used) <= plan.max_suppliers:
                    chosen[workspace.skus[index].sku_id] = vendor_id
                    search(index + 1, optimistic + cost, next_used)
            chosen.pop(workspace.skus[index].sku_id, None)
        try:
            search(0, ZERO, set())
        except RuntimeError:
            return unavailable("The conditional partial-order rebate requires a larger exact search. Narrow eligible suppliers/items or compare single-supplier awards; no unproven optimum is reported.", "clarify")
    total, groups, lines = best
    baseline = min((result[0] for result in singles.values() if result), default=None)
    return {"status": "ok", "summary": f"Complete award across {len(groups)} supplier(s), {'QA-approved only' if plan.qa_only else 'including unapproved suppliers'}. Freight included; rebates evaluated on each actual awarded order. {'Tax included.' if plan.include_tax else 'GST excluded.'}", "total": money(total), "baseline_total": money(baseline) if baseline is not None else None, "savings": money(baseline - total) if baseline is not None else None, "awards": groups, "lines": lines, "plan": plan.model_dump(mode="json")}
