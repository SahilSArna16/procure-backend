from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SKU(Model):
    sku_id: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300)
    factory: str = Field(default="All factories", max_length=100)
    ply: int | None = Field(default=None, ge=1, le=20)
    weight_kg: Decimal | None = Field(default=None, gt=0, le=1000)
    quantity: int | None = Field(default=None, gt=0, le=100_000_000)
    pieces_per_box: Decimal | None = Field(default=None, gt=0, le=10000)


class QualityQuestion(Model):
    id: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=500)
    answer_type: Literal["yes_no", "number", "text"] = "yes_no"
    required_for_qa: bool = True
    expected_answer: str | None = None
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    requires_attachment: bool = False


class VendorInvitation(Model):
    name: str = Field(min_length=1, max_length=150)
    email: str = Field(min_length=3, max_length=200)


class RFx(Model):
    title: str = Field(min_length=1, max_length=200)
    scope: str = Field(min_length=1, max_length=5000)
    skus: list[SKU] = Field(min_length=1, max_length=500)
    questionnaire: list[QualityQuestion] = Field(min_length=1, max_length=30)
    payment_terms: str = "Net 60 days"
    delivery_terms: str = "Delivered to the stated buyer factories"
    submission_instructions: str = "Reply in any format; include item codes, prices, units, freight and questionnaire evidence."
    vendors: list[VendorInvitation] = Field(default_factory=list, max_length=10)
    assumptions: list[str] = Field(default_factory=list, max_length=30)
    quality_rules_approved: bool = False
    status: Literal["draft", "issued"] = "draft"

    @model_validator(mode="after")
    def unique_fields(self):
        for values in ([s.sku_id for s in self.skus], [q.id for q in self.questionnaire]):
            if len(values) != len(set(values)):
                raise ValueError("RFx item codes and question IDs must be unique.")
        return self


class QuestionnaireAnswer(Model):
    question_id: str
    answer: str | None = None
    source_reference: str = ""
    needs_review: bool = False


class CurrencyConversion(Model):
    currency: str = Field(min_length=3, max_length=3)
    inr_per_unit: Decimal = Field(gt=0, le=1_000_000)
    source: str = Field(min_length=1, max_length=300)
    confirmed: bool = False


class AttachedDocument(Model):
    id: str
    name: str
    content_type: str
    content_base64: str = ""
    vendor_id: str | None = None
    role: Literal["quotation", "supporting"] = "quotation"
    question_ids: list[str] = Field(default_factory=list)
    reviewed: bool = False
    extracted_text: str = ""


class OutboxMessage(Model):
    id: str
    vendor_name: str
    recipient: str
    subject: str
    body: str
    status: Literal["simulated"] = "simulated"


class Rebate(Model):
    percent: Decimal = Field(ge=0, le=100)
    minimum_order_value: Decimal | None = Field(default=None, ge=0, le=1_000_000_000_000)
    threshold_operator: Literal["gt", "gte"] = "gte"
    threshold_basis: Literal["base_goods", "net_goods", "goods_and_freight"] = "base_goods"
    applies_to: Literal["goods", "goods_and_freight"] = "goods"
    requires_all_skus: bool = False
    required_sku_ids: list[str] = Field(default_factory=list, max_length=500)
    conditions_supported: bool = False
    condition_text: str = Field(default="", max_length=4000)
    source_reference: str = Field(default="", max_length=4000)


class LineItem(Model):
    sku_id: str = Field(min_length=1, max_length=80)
    source_sku_id: str = Field(default="", max_length=80)
    description: str = Field(default="", max_length=300)
    quoted_quantity: Decimal | None = Field(default=None, gt=0, le=100_000_000)
    quantity_unit: Literal["boxes", "kg", "unknown"] = "unknown"
    currency: str | None = Field(default=None, max_length=10)
    match_status: Literal["sku", "description", "uncertain", "unmatched", "confirmed"] = "sku"
    match_candidates: list[str] = Field(default_factory=list, max_length=20)
    source_evidence: dict[str, str] = Field(default_factory=dict)
    quoted_price: Decimal | None = Field(default=None, ge=0, le=1_000_000_000)
    price_unit: Literal["per_box", "per_kg", "per_100_boxes", "per_100_pieces", "per_1000_boxes", "unknown"] = "unknown"
    notes_footnotes: str = Field(default="", max_length=4000)
    source_reference: str = Field(default="", max_length=4000)
    discount_percent: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    discount_conditions: str = Field(default="", max_length=4000)
    freight_per_box: Decimal | None = Field(default=Decimal("0"), ge=0, le=1_000_000)
    needs_review: bool = False


class Quote(Model):
    vendor_id: str = Field(min_length=1, max_length=80)
    vendor_name: str = Field(min_length=1, max_length=150)
    source_file: str = Field(default="", max_length=200)
    source_text: str = Field(default="", max_length=120_000)
    payment_terms: str = Field(default="Not stated", max_length=300)
    qa_passed: bool | None = None
    currency: str = Field(default="INR", max_length=10)
    freight_per_order: Decimal | None = Field(default=Decimal("0"), ge=0, le=1_000_000_000)
    tax_percent: Decimal | None = Field(default=Decimal("0"), ge=0, le=100)
    commercial_terms_confirmed: bool = False
    certifications: list[str] = Field(default_factory=list, max_length=100)
    questionnaire_answers: list[QuestionnaireAnswer] = Field(default_factory=list, max_length=30)
    source_evidence: dict[str, str] = Field(default_factory=dict)
    rebate: Rebate | None = None
    unmatched_items: list[LineItem] = Field(default_factory=list, max_length=500)
    notes: list[str] = Field(default_factory=list, max_length=100)
    line_items: list[LineItem] = Field(max_length=500)

    @model_validator(mode="after")
    def unique_lines(self):
        ids = [item.sku_id for item in self.line_items]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate SKU rates: consolidate or resolve alternatives first.")
        if self.commercial_terms_confirmed and self.freight_per_order is None:
            raise ValueError("Confirm the fixed freight amount (zero if included) before confirming commercial terms.")
        return self


class UploadRecord(Model):
    id: str
    name: str
    format: str
    status: Literal["processing", "completed", "failed"] = "processing"
    error: str | None = None
    vendor_id: str | None = None
    sha256: str = ""
    matched_count: int = 0
    uncertain_count: int = 0
    missing_count: int = 0


class ExtractedQuote(Quote):
    """Raw supplier alternatives may share a code; matching quarantines them."""
    @model_validator(mode="after")
    def unique_lines(self):
        return self


class Workspace(Model):
    id: str
    name: str = Field(default="Corrugated packaging sourcing", max_length=200)
    is_sample: bool = False
    revision: int = Field(default=1, ge=1)
    skus: list[SKU] = Field(default_factory=list, max_length=500)
    quotes: list[Quote] = Field(default_factory=list, max_length=10)
    uploads: list[UploadRecord] = Field(default_factory=list, max_length=100)
    master_origin: Literal["buyer", "quotations", "sample"] = "buyer"
    demo_mode: bool = False
    rfx: RFx | None = None
    documents: list[AttachedDocument] = Field(default_factory=list, max_length=40)
    outbox: list[OutboxMessage] = Field(default_factory=list, max_length=10)
    fx_rates: list[CurrencyConversion] = Field(default_factory=list, max_length=10)
    analysis_history: list[dict] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def unique_ids(self):
        for values in ([s.sku_id for s in self.skus], [q.vendor_id for q in self.quotes]):
            if len(values) != len(set(values)):
                raise ValueError("SKU and vendor IDs must be unique.")
        return self


class ScenarioPlan(Model):
    kind: Literal["split_award", "lowest_per_sku", "single_vendor", "compare_vendors", "quality_review", "coverage", "clarify"] = "split_award"
    qa_only: bool = True
    include_tax: bool = False
    excluded_vendor_ids: list[str] = Field(default_factory=list, max_length=10)
    vendor_id: str | None = None
    max_suppliers: int = Field(default=10, ge=1, le=10)
    quantity_multiplier: Decimal = Field(default=Decimal("1"), gt=0, le=100)
    explanation: str = Field(default="", max_length=1000)


class ScenarioRequest(Model):
    question: str = Field(default="", max_length=2000)
    plan: ScenarioPlan | None = None
