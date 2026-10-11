"""Source-code joins; source order never determines master item identity."""
import re
import unicodedata
from difflib import SequenceMatcher

from .models import Quote, SKU, Workspace


def code(value: str) -> str:
    return re.sub(r"[\s_-]", "", unicodedata.normalize("NFKC", value).upper())


def description(value: str) -> str:
    return " ".join(re.sub(r"[^\w]", " ", unicodedata.normalize("NFKC", value).casefold()).split())


def extend_master(workspace: Workspace, quote: Quote):
    if workspace.master_origin != "quotations":
        return
    known = {code(s.sku_id) for s in workspace.skus}
    had_master = bool(workspace.skus)
    for line in quote.line_items:
        original = line.source_sku_id or line.sku_id
        if not original or original.upper().startswith(("UNKNOWN", "UNMATCHED", "PENDING")):
            continue
        if code(original) in known:
            existing = next(s for s in workspace.skus if code(s.sku_id) == code(original))
            if existing.description == existing.sku_id and line.description:
                existing.description = line.description
            known_ply = re.search(r"(\d+)\s*[- ]?ply", line.description, re.I)
            if existing.ply is None and known_ply:
                existing.ply = int(known_ply.group(1))
            if existing.quantity is None and line.quantity_unit == "boxes" and line.quoted_quantity is not None and line.quoted_quantity == line.quoted_quantity.to_integral_value():
                existing.quantity = int(line.quoted_quantity)
            continue
        quantity = line.quoted_quantity
        boxes = int(quantity) if quantity is not None and quantity == quantity.to_integral_value() and line.quantity_unit == "boxes" else None
        ply = re.search(r"(\d+)\s*[- ]?ply", line.description, re.I)
        workspace.skus.append(SKU(sku_id=original, description=line.description or original, quantity=boxes, ply=int(ply.group(1)) if ply else None))
        if had_master:
            line.match_status = "uncertain"
            line.needs_review = True
        known.add(code(original))
    if len(workspace.skus) > 500:
        raise ValueError("The combined master exceeds 500 items.")


def match_quote(quote: Quote, workspace: Workspace) -> Quote:
    exact = {s.sku_id: s for s in workspace.skus}
    normalized = {}
    for sku in workspace.skus:
        normalized.setdefault(code(sku.sku_id), []).append(sku.sku_id)
    matched, unmatched, blocked = {}, [], set()
    for line in quote.line_items:
        original = line.source_sku_id or line.sku_id
        line.source_sku_id = original
        candidates = [original] if original in exact else normalized.get(code(original), [])
        status = "uncertain" if line.match_status == "uncertain" else "sku"
        if not candidates and line.description:
            candidates = [s.sku_id for s in workspace.skus if description(s.description) == description(line.description)]
            status = "description"
            if not candidates:
                scored = sorted(((SequenceMatcher(None, description(s.description), description(line.description)).ratio(), s.sku_id) for s in workspace.skus), reverse=True)
                candidates = [identifier for score, identifier in scored[:5] if score >= .8]
                status = "uncertain"
        line.match_candidates = candidates[:20]
        if len(candidates) != 1:
            line.match_status = "unmatched" if not candidates else "uncertain"
            line.needs_review = True
            unmatched.append(line)
            continue
        target = candidates[0]
        line.sku_id = target
        line.match_status = status
        if status != "sku":
            line.needs_review = True
        if target in blocked:
            line.needs_review = True
            line.match_status = "uncertain"
            unmatched.append(line)
        elif target in matched:
            # Preserve both conflicting source rows rather than choosing one price.
            previous = matched.pop(target)
            previous.needs_review = line.needs_review = True
            previous.match_status = line.match_status = "uncertain"
            unmatched.extend([previous, line])
            blocked.add(target)
        else:
            matched[target] = line
    quote.line_items = list(matched.values())
    quote.unmatched_items = unmatched
    if quote.rebate and quote.rebate.requires_all_skus and not quote.rebate.required_sku_ids:
        if unmatched:
            quote.rebate.conditions_supported = False
        else:
            quote.rebate.required_sku_ids = [line.sku_id for line in quote.line_items]
    return quote
