from decimal import Decimal

from app.assessment_demo import demo_workspace, draft_seed
from app.engine import rates_for
from app.models import AttachedDocument, CurrencyConversion, LineItem, QuestionnaireAnswer, Quote, SKU, Workspace
from app.quality import qualification
from app.rfx import simulated_distribution


def test_quality_requires_buyer_rules_answers_and_reviewed_evidence():
    workspace = demo_workspace()
    workspace.rfx = draft_seed(workspace)
    quote = Quote(vendor_id="supplier", vendor_name="Supplier", line_items=[], qa_passed=True, questionnaire_answers=[
        QuestionnaireAnswer(question_id="iso9001", answer="yes", source_reference="Quote page 1"),
        QuestionnaireAnswer(question_id="fsc", answer="yes", source_reference="Quote page 1"),
        QuestionnaireAnswer(question_id="burst_factor", answer="24", source_reference="Quote page 1"),
        QuestionnaireAnswer(question_id="lead_time", answer="7", source_reference="Quote page 1"),
    ])
    assert qualification(workspace, quote)["passed"] is None
    workspace.rfx.quality_rules_approved = True
    assert qualification(workspace, quote)["passed"] is None
    workspace.documents.append(AttachedDocument(id="document", name="certificate.pdf", content_type="application/pdf", vendor_id=quote.vendor_id, role="supporting", question_ids=["iso9001"], reviewed=True))
    assert qualification(workspace, quote)["passed"] is True
    quote.questionnaire_answers[2].answer = "20"
    assert qualification(workspace, quote)["passed"] is False
    quote.questionnaire_answers[2].answer = None
    assert qualification(workspace, quote)["passed"] is None


def test_usd_per_100_pieces_needs_confirmed_fx_and_piece_spec():
    sku = SKU(sku_id="A", description="Carton", quantity=10)
    quote = Quote(vendor_id="q", vendor_name="Supplier", currency="USD", line_items=[LineItem(sku_id="A", quoted_price="100", price_unit="per_100_pieces")])
    workspace = Workspace(id="test", skus=[sku], quotes=[quote], fx_rates=[CurrencyConversion(currency="USD", inr_per_unit="83", source="Test fixture", confirmed=False)])
    assert rates_for(sku, quote, workspace)[0] is None
    workspace.fx_rates[0].confirmed = True
    assert "pieces per box" in rates_for(sku, quote, workspace)[1]
    sku.pieces_per_box = Decimal("1")
    assert rates_for(sku, quote, workspace)[0][0] == Decimal("83")


def test_outbox_is_explicitly_simulated_and_includes_questionnaire():
    workspace = demo_workspace()
    workspace.rfx = draft_seed(workspace)
    workspace.rfx.quality_rules_approved = True
    simulated_distribution(workspace)
    assert len(workspace.outbox) == 5
    assert all(message.status == "simulated" for message in workspace.outbox)
    assert all("SKU-030" in message.body and "iso9001" in message.body for message in workspace.outbox)
