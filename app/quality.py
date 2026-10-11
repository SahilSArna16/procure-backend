"""Buyer-approved questionnaire rules, independent of supplier claims."""
from decimal import Decimal, InvalidOperation

from .models import Quote, Workspace


def qualification(workspace: Workspace, quote: Quote) -> dict:
    if not workspace.rfx:
        return {"passed": quote.qa_passed, "status": "passed" if quote.qa_passed is True else "failed" if quote.qa_passed is False else "pending", "basis": "Manual buyer decision", "checks": []}
    if not workspace.rfx.quality_rules_approved:
        return {"passed": None, "status": "pending", "basis": "Buyer must approve the RFx qualification rules", "checks": []}
    answers = {a.question_id: a for a in quote.questionnaire_answers}
    checks = []
    for question in workspace.rfx.questionnaire:
        answer = answers.get(question.id)
        status, reason = "pending", "No sourced answer provided"
        if answer and answer.answer is not None and not answer.needs_review and answer.source_reference.strip():
            value = answer.answer.strip()
            status, reason = "passed", "Meets buyer rule"
            if question.answer_type == "yes_no":
                values = {"yes": True, "true": True, "passed": True, "no": False, "false": False, "failed": False}
                parsed = values.get(value.casefold())
                expected = values.get((question.expected_answer or "yes").casefold())
                if parsed is None or expected is None:
                    status, reason = "pending", "Answer requires clarification"
                elif parsed != expected:
                    status, reason = "failed", "Does not meet the buyer's required answer"
            elif question.answer_type == "number":
                try:
                    number = Decimal(value.replace(",", ""))
                    if not number.is_finite():
                        raise InvalidOperation
                    if question.minimum is not None and number < question.minimum or question.maximum is not None and number > question.maximum:
                        status, reason = "failed", "Outside the buyer's required range"
                except InvalidOperation:
                    status, reason = "pending", "Numeric answer requires clarification"
            elif question.expected_answer and value.casefold() != question.expected_answer.casefold():
                status, reason = "failed", "Answer differs from buyer requirement"
            if status == "passed" and question.requires_attachment:
                documents = [d for d in workspace.documents if d.vendor_id == quote.vendor_id and d.role == "supporting" and question.id in d.question_ids]
                if not any(d.reviewed for d in documents):
                    status, reason = "pending", "Supporting document must be attached and reviewed by the buyer"
        checks.append({"question_id": question.id, "prompt": question.prompt, "answer": answer.answer if answer else None, "source_reference": answer.source_reference if answer else "", "required": question.required_for_qa, "status": status, "reason": reason})
    required = [check for check in checks if check["required"]]
    if not required:
        return {"passed": None, "status": "pending", "basis": "No required qualification criteria configured", "checks": checks}
    state = "failed" if any(c["status"] == "failed" for c in required) else "pending" if any(c["status"] == "pending" for c in required) else "passed"
    return {"passed": True if state == "passed" else False if state == "failed" else None, "status": state, "basis": "Buyer-approved questionnaire rules and reviewed evidence", "checks": checks}
