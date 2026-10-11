"""Read explicit tabular values without asking a model to align reordered rows."""
import io
import re
from decimal import Decimal, InvalidOperation

from openpyxl import load_workbook

from .models import LineItem, Rebate
from .parsers import bounded_sheet_rows


def header(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def numeric(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value).replace(",", "").strip())
        return result if result.is_finite() and result >= 0 else None
    except InvalidOperation:
        return None


def unit(value):
    text = str(value or "").lower().replace("_", " ")
    if re.search(r"(?:/|per\s*|^)(?:1,?000|1000)\s*box", text):
        return "per_1000_boxes"
    if re.search(r"(?:/|per\s*|^)\s*100\s*(?:pieces|pcs)", text):
        return "per_100_pieces"
    if re.search(r"(?:/|per\s*|^)\s*100\s*box", text):
        return "per_100_boxes"
    if re.search(r"(?:/|per\s*)\s*(?:kg|kilogram)", text) or text.strip() in {"kg", "kilogram", "kilograms"}:
        return "per_kg"
    if re.search(r"(?:/|per\s*)\s*(?:box|carton)", text) or text.strip() in {"box", "boxes", "carton", "cartons"}:
        return "per_box"
    return "unknown"


def currency(value):
    text = str(value or "").upper()
    if "INR" in text or "₹" in text:
        return "INR"
    for name in ("USD", "EUR", "GBP"):
        if name in text:
            return name
    return None


def table_lines(filename, data, text):
    result = []
    if filename.lower().endswith(".xlsx"):
        book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            for sheet in book:
                columns = None
                for row_index, row in bounded_sheet_rows(sheet):
                    names = [header(v) for v in row]
                    sku_index = next((i for i, name in enumerate(names) if name in {"skuid", "sku", "skuitemcode", "itemcode", "itemid", "materialcode", "materialref", "productcode", "itemnumber"}), None)
                    price_index = next((i for i, name in enumerate(names) if name.startswith(("unitprice", "unitrate", "priceper", "rateper", "ourofferedrate")) or name in {"price", "rate", "quotedprice", "basicrate"}), None)
                    if sku_index is not None and price_index is not None:
                        columns = {"sku": sku_index, "price": price_index, "price_header": row[price_index]}
                        for field, aliases in {"description": {"description", "itemdescription", "materialdescription", "productdescription"}, "quantity": {"quantity", "qty", "orderbatchsize", "orderquantity", "orderqty", "quantityboxes"}, "unit": {"unit", "basis", "priceunit", "uom", "rateunit"}, "currency": {"currency", "curr"}}.items():
                            columns[field] = next((i for i, name in enumerate(names) if name in aliases), None)
                        columns["quantity_boxes"] = columns["quantity"] is not None and names[columns["quantity"]] == "quantityboxes"
                        continue
                    if columns is None:
                        continue
                    def value(key):
                        index = columns.get(key)
                        return row[index] if index is not None and index < len(row) else None
                    identifier = value("sku")
                    price = numeric(value("price"))
                    if identifier is None or price is None:
                        continue
                    basis = unit(value("unit")) if value("unit") is not None else unit(columns["price_header"])
                    qty = numeric(value("quantity"))
                    reference = f"Sheet {sheet.title}, row {row_index}"
                    result.append(LineItem(sku_id=str(identifier), source_sku_id=str(identifier), description=str(value("description") or ""), quoted_quantity=qty if qty and qty > 0 else None, quantity_unit="boxes" if columns["quantity_boxes"] or basis in {"per_box", "per_1000_boxes"} else "unknown", quoted_price=price, price_unit=basis, currency=currency(value("currency")) or currency(columns["price_header"]), source_reference=reference, source_evidence={"sku_id": f"{reference}: {identifier}", "quoted_price": f"{reference}: {columns['price_header']} = {value('price')}", "quoted_quantity": f"{reference}: {value('quantity')}", "price_unit": f"{reference}: {value('unit') or columns['price_header']}"}))
        finally:
            book.close()
    elif filename.lower().endswith(".pdf"):
        pattern = re.compile(r"^(SKU[-_]\d+)\s+(.+?)\s+([\d,]+)\s+([\d,]+\.\d+)\s*$", re.I)
        page = "1"
        for row in text.splitlines():
            marker = re.match(r"Page (\d+)$", row.strip())
            if marker:
                page = marker.group(1)
            match = pattern.match(row.strip())
            if not match:
                continue
            identifier, desc, qty, price = match.groups()
            # Use an explicit table heading only; otherwise retain the model's unit.
            basis = "per_box" if re.search(r"INR\s*/\s*box", text, re.I) else "unknown"
            reference = f"Page {page}: {row.strip()}"
            result.append(LineItem(sku_id=identifier, source_sku_id=identifier, description=desc, quoted_quantity=numeric(qty), quantity_unit="boxes" if basis == "per_box" else "unknown", quoted_price=numeric(price), price_unit=basis, currency="INR" if basis == "per_box" else None, source_reference=reference, source_evidence={"sku_id": reference, "description": reference, "quoted_quantity": reference, "quoted_price": reference, "price_unit": "Table heading: INR / box" if basis == "per_box" else reference}))
    return result


def explicit_freight_terms(text):
    """Recognize source clauses before any model-generated amounts are validated."""
    flattened = " ".join(text.split())
    included = re.search(r"\bFreight\s+(?:is\s+)?included(?:\s+up\s+to\s+facility\s+doorstep)?\s*[.;](?!\s*(?:if|only|when|for orders))", flattened, re.I)
    freight = re.search(r"Freight of INR\s+([\d,.]+)\s+per box is charged separately for every awarded box", flattened, re.I)
    fixed = re.search(r"\bFreight\s+(?:of\s+)?INR\s+([\d,.]+)\s+per\s+(?:awarded\s+)?order\b", flattened, re.I)
    no_variable = re.search(r"No per[- ]box freight charge|Fixed freight only", flattened, re.I)
    if freight:
        amount = numeric(freight.group(1))
        if amount is not None:
            return {"per_order": str(numeric(fixed.group(1))) if fixed else "0", "per_box": str(amount), "source_reference": freight.group(0)}
    if fixed and numeric(fixed.group(1)) is not None:
        return {"per_order": str(numeric(fixed.group(1))), "per_box": "0" if no_variable else None, "source_reference": fixed.group(0) + ("; " + no_variable.group(0) if no_variable else "")}
    if included:
        return {"per_order": "0", "per_box": "0", "source_reference": included.group(0)}
    return None


def apply_source_freight_to_output(raw, text):
    """Replace only fields proven by an explicit source clause, never guess."""
    freight = explicit_freight_terms(text)
    if freight is None or not isinstance(raw, dict):
        return raw
    raw["freight_per_order"] = freight["per_order"]
    if isinstance(raw.get("source_evidence"), dict):
        raw["source_evidence"]["freight_per_order"] = freight["source_reference"]
    items = raw.get("line_items")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                if freight["per_box"] is not None:
                    item["freight_per_box"] = freight["per_box"]
                if isinstance(item.get("source_evidence"), dict):
                    item["source_evidence"]["freight_per_box"] = freight["source_reference"]
    return raw


def protect_explicit_terms(quote, text):
    """Recognize the supplied PDF's full rebate clause, not just its 5% number."""
    flattened = " ".join(text.split())
    stated_currency = re.search(r"\bCurrency:\s*(INR|USD|EUR|GBP)\b", flattened, re.I)
    if stated_currency:
        quote.currency = stated_currency.group(1).upper()
        quote.source_evidence["currency"] = stated_currency.group(0)
    freight = explicit_freight_terms(text)
    if freight:
        quote.freight_per_order = Decimal(freight["per_order"])
        quote.source_evidence["freight_per_order"] = freight["source_reference"]
        for line in quote.line_items:
            if freight["per_box"] is not None:
                line.freight_per_box = Decimal(freight["per_box"])
            line.source_evidence["freight_per_box"] = freight["source_reference"]
    clause = re.search(r"A\s+([\d.]+)% overall bill discount applies only if the base goods value across all\s+(\d+)\s+SKUs exceeds INR\s+([\d,]+)\s+in a single purchase order awarding all\s+\2\s+SKUs.*?discount applies to goods plus freight, before GST.*?No rebate applies to a partial award or at the exact threshold\.", flattened, re.I)
    if clause:
        quote.rebate = Rebate(percent=numeric(clause.group(1)), minimum_order_value=numeric(clause.group(3)), threshold_operator="gt", threshold_basis="base_goods", applies_to="goods_and_freight", requires_all_skus=True, conditions_supported=len(quote.line_items) == int(clause.group(2)), condition_text=clause.group(0), source_reference="Commercial footnote [3]: " + clause.group(0))
        for line in quote.line_items:
            # The footnote is an order rebate, never a blanket unconditional rate cut.
            line.discount_percent = Decimal("0")
            line.discount_conditions = ""
        quote.source_evidence["rebate"] = quote.rebate.source_reference
    goods_clause = re.search(r"([\d.]+)% goods rebate only if base goods value exceeds INR ([\d,]+) and all (\d+) SKUs are awarded in a single order\. No rebate for partial awards or exact threshold\. Freight not discounted\.", flattened, re.I)
    if goods_clause:
        quote.rebate = Rebate(percent=numeric(goods_clause.group(1)), minimum_order_value=numeric(goods_clause.group(2)), threshold_operator="gt", threshold_basis="base_goods", applies_to="goods", requires_all_skus=True, required_sku_ids=[line.sku_id for line in quote.line_items], conditions_supported=len(quote.line_items) == int(goods_clause.group(3)), condition_text=goods_clause.group(0), source_reference=goods_clause.group(0))
        for line in quote.line_items:
            line.discount_percent = Decimal("0")
            line.discount_conditions = ""
        quote.source_evidence["rebate"] = quote.rebate.source_reference
    return quote
