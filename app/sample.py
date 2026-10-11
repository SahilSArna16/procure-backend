from decimal import Decimal
from uuid import uuid4

from .models import LineItem, Quote, SKU, Workspace


def sample_workspace() -> Workspace:
    sizes = ["200 × 150 × 120", "250 × 200 × 150", "300 × 250 × 200", "350 × 250 × 250", "400 × 300 × 250", "450 × 350 × 300", "500 × 400 × 350", "550 × 400 × 400", "600 × 450 × 400", "650 × 500 × 450"]
    skus = [SKU(sku_id=f"SKU-{i+1:03}", description=f"{3 if i < 10 else 5 if i < 20 else 7}-ply carton · {sizes[i % 10]} mm", factory=["Pune", "Ahmedabad", "Chennai"][i // 10], ply=3 if i < 10 else 5 if i < 20 else 7, weight_kg=Decimal("0.24") + Decimal(i) * Decimal("0.025"), quantity=4000 + (i % 5) * 1000) for i in range(30)]
    names = ["Apex Containers", "Packwell Industries", "GreenBox Packaging", "Shree Corrugators", "Metro Cartons"]
    files = ["apex_quote.xlsx", "packwell_quote.pdf", "greenbox_quote.docx", "shree_pricelist.png", "metro_email.txt"]
    quotes = []
    for v, name in enumerate(names):
        lines = []
        for i, sku in enumerate(skus):
            base = Decimal("13.5") + Decimal(i) * Decimal("0.87")
            rate = (base + Decimal(str(((i * 7 + v * 11) % 13) / 4)) - (Decimal("1.2") if i % 3 == v % 3 else 0)).quantize(Decimal("0.01"))
            if v == 4 and sku.ply != 5:
                lines.append(LineItem(sku_id=sku.sku_id, price_unit="unknown", notes_footnotes="Rest same as last year. Historical rate not supplied.", source_reference="Email, line 2", needs_review=True))
                continue
            per_kg = v in (3, 4)
            quoted = Decimal("42") if v == 4 else (rate / sku.weight_kg).quantize(Decimal("0.01")) if per_kg else rate
            lines.append(LineItem(sku_id=sku.sku_id, quoted_price=quoted, price_unit="per_kg" if per_kg else "per_box", discount_percent=Decimal("5") if v == 1 else Decimal("0"), notes_footnotes="Unconditional 5% rebate on all listed rates." if v == 1 else "Rate quoted per kg; use buyer's standard carton weight." if per_kg else "", source_reference=f"{'Photo' if v == 3 else 'Quote'}, row {i+2}"))
        quotes.append(Quote(vendor_id=f"vendor-{v+1}", vendor_name=name, source_file=files[v], payment_terms=["Net 60", "Net 45", "Net 60", "Net 30", "Not stated"][v], qa_passed=[True, True, False, True, None][v], freight_per_order=Decimal("7500") if v == 1 else Decimal("0"), commercial_terms_confirmed=v != 4, notes=["5% unconditional discount; INR 7,500 freight per awarded order. GST excluded."] if v == 1 else ["QA failed in this illustrative dataset."] if v == 2 else ["Prior-year rates required for 20 SKUs; QA and payment terms pending."] if v == 4 else ["Freight included; GST excluded."], line_items=lines))
    return Workspace(id=str(uuid4()), is_sample=True, skus=skus, quotes=quotes)
