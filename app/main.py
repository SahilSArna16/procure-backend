import csv
import io
import logging
from hashlib import sha256
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import ValidationError

from . import settings
from .ai import AIError, extract_quote, plan_question
from .engine import calculate, comparison
from .models import SKU, ScenarioRequest, UploadRecord, Workspace
from .matching import extend_master, match_quote
from .parsers import MAX_FILE_BYTES
from .repository import ConflictError, Repository
from .sample import sample_workspace

logger = logging.getLogger("procurement")


@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.repo = Repository()
    yield
    application.state.repo.close()


app = FastAPI(title="Procure · Procurement Assistant", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:5174", "http://127.0.0.1:5174"], allow_methods=["GET", "POST", "PUT"], allow_headers=["Content-Type"])


@app.exception_handler(ConflictError)
async def conflict_handler(_, error):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=409, content={"detail": str(error)})


@app.exception_handler(Exception)
async def unexpected_handler(_, error):
    from fastapi.responses import JSONResponse
    logger.error("Request failed (%s)", type(error).__name__)
    return JSONResponse(status_code=503, content={"detail": "Service unavailable. Check backend connectivity and run migrations if using Neon."})


def load(workspace_id: UUID) -> Workspace:
    workspace = app.state.repo.get(str(workspace_id))
    if workspace is None:
        raise HTTPException(404, "Comparison not found. Load a sample or create a new comparison.")
    return workspace


@app.get("/api/health")
def health():
    return {"status": "ok", "ai_provider": "sarvam", "ai_configured": bool(settings.SARVAM_KEY), "model": settings.SARVAM_MODEL, "storage": settings.STORAGE_BACKEND}


@app.post("/api/workspaces/sample")
def create_sample():
    return comparison(app.state.repo.save(sample_workspace(), new=True))


@app.post("/api/workspaces")
def create_workspace(skus: list[SKU]):
    if not skus:
        raise HTTPException(422, "Provide at least one master SKU, or start with supplier uploads.")
    try:
        workspace = Workspace(id=str(uuid4()), skus=skus)
    except ValidationError:
        raise HTTPException(422, "Provide 1–500 master SKUs with unique IDs, positive quantities, and valid weights.") from None
    return comparison(app.state.repo.save(workspace, new=True))


@app.post("/api/workspaces/draft")
def create_draft():
    return comparison(app.state.repo.save(Workspace(id=str(uuid4()), master_origin="quotations"), new=True))


@app.get("/api/workspaces/{workspace_id}")
def read_workspace(workspace_id: UUID):
    return comparison(load(workspace_id))


@app.put("/api/workspaces/{workspace_id}")
def update_workspace(workspace_id: UUID, workspace: Workspace):
    existing = load(workspace_id)
    if workspace.id != str(workspace_id):
        raise HTTPException(422, "Workspace ID cannot be changed.")
    if workspace.revision != existing.revision:
        raise ConflictError("Comparison changed. Reload before saving.")
    known = {s.sku_id for s in workspace.skus}
    if any(line.sku_id not in known for q in workspace.quotes for line in q.line_items):
        raise HTTPException(422, "Quotes contain SKUs outside the master. Resolve these before saving.")
    return comparison(app.state.repo.save(workspace))


def change_upload(workspace_id, transform):
    # Merge each completed file into the latest revision; preserve unrelated edits.
    for _ in range(3):
        latest = load(workspace_id)
        transform(latest)
        try:
            return app.state.repo.save(latest)
        except ConflictError:
            continue
    raise ConflictError("Comparison changed while saving the upload. Refresh and retry this file.")


@app.post("/api/workspaces/{workspace_id}/quotes")
def upload_quotes(workspace_id: UUID, files: list[UploadFile] = File(...), retry_upload_id: str | None = Form(None)):
    workspace = load(workspace_id)
    if workspace.is_sample:
        raise HTTPException(409, "Create a live comparison from the master before uploading real quotes.")
    if len(files) > 10:
        raise HTTPException(422, "A comparison supports up to 10 supplier quotes.")
    if retry_upload_id and (len(files) != 1 or not any(u.id == retry_upload_id and u.status == "failed" for u in workspace.uploads)):
        raise HTTPException(422, "Retry requires one file and a failed upload record from this comparison.")
    errors, results, added = [], [], 0
    for file in files:
        filename = Path(file.filename or "quote").name[:200]
        existing = next((u for u in workspace.uploads if u.id == retry_upload_id), None)
        if existing and existing.name != filename:
            raise HTTPException(422, f"Select the original file name for retry: {existing.name}")
        data = file.file.read(MAX_FILE_BYTES + 1)
        digest = sha256(data).hexdigest()
        duplicate = next((u for u in workspace.uploads if u.status == "completed" and u.name == filename and u.sha256 == digest and any(q.vendor_id == u.vendor_id for q in workspace.quotes)), None)
        if duplicate and not retry_upload_id:
            results.append(duplicate.model_dump(mode="json"))
            file.file.close()
            continue
        record = UploadRecord(id=retry_upload_id or str(uuid4()), name=filename, format=Path(filename).suffix.lstrip('.').upper() or "UNKNOWN", sha256=digest)
        def start(latest):
            latest.uploads = [u for u in latest.uploads if u.id != record.id][-99:] + [record]
        workspace = change_upload(workspace_id, start)
        extraction_master = [s.model_dump(mode="json") for s in workspace.skus]
        try:
            if len(workspace.quotes) >= 10:
                raise ValueError("Maximum of 10 quotations reached. Remove a supplier before retrying.")
            quote = extract_quote(filename, data, workspace)
            def complete(latest):
                if latest.master_origin == "buyer" and [s.model_dump(mode="json") for s in latest.skus] != extraction_master:
                    raise ValueError("Master specifications changed during extraction. Retry this file against the updated master.")
                if len(latest.quotes) >= 10:
                    raise ValueError("Maximum of 10 quotations reached. Remove a supplier before retrying.")
                extend_master(latest, quote)
                match_quote(quote, latest)
                if not latest.skus:
                    raise ValueError("No explicit item codes available to build a master. Import a master CSV, then retry.")
                latest.quotes.append(quote)
                record.status = "completed"
                record.vendor_id = quote.vendor_id
                record.matched_count = sum(line.match_status in {"sku", "confirmed"} for line in quote.line_items)
                record.uncertain_count = sum(line.match_status not in {"sku", "confirmed"} for line in quote.line_items) + len(quote.unmatched_items)
                record.missing_count = len(latest.skus) - len(quote.line_items)
                latest.uploads = [record if u.id == record.id else u for u in latest.uploads]
                latest.is_sample = False
            workspace = change_upload(workspace_id, complete)
            added += 1
        except (AIError, ValueError) as error:
            record.status, record.error = "failed", str(error)
        except Exception as error:
            # Reveal the failure class without exposing quote text, keys or URLs.
            logger.error("Upload %s failed (%s)", record.id, type(error).__name__)
            record.status, record.error = "failed", f"Unexpected processing error ({type(error).__name__}). The other quotations are preserved. Retry this file after the backend is updated."
        finally:
            file.file.close()
        if record.status == "failed":
            def fail(latest):
                latest.uploads = [record if u.id == record.id else u for u in latest.uploads]
            workspace = change_upload(workspace_id, fail)
            errors.append({"file": filename, "message": record.error})
        results.append(record.model_dump(mode="json"))
    return {"comparison": comparison(workspace), "added": added, "errors": errors, "files": results}


@app.post("/api/workspaces/{workspace_id}/scenario")
def scenario(workspace_id: UUID, body: ScenarioRequest):
    workspace = load(workspace_id)
    try:
        return calculate(workspace, body.plan or plan_question(body.question, workspace))
    except AIError as error:
        raise HTTPException(502, str(error)) from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


def csv_response(rows, filename):
    output = io.StringIO()
    writer = csv.writer(output)
    for row in rows:
        writer.writerow(["'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value for value in row])
    return Response("\ufeff" + output.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/master-template")
def master_template():
    workspace = sample_workspace()
    return csv_response([["sku_id", "description", "factory", "ply", "weight_kg", "quantity"], *[[s.sku_id, s.description, s.factory, s.ply, s.weight_kg, s.quantity] for s in workspace.skus]], "master-sku-template.csv")


@app.post("/api/master-import")
def master_import(file: UploadFile = File(...)):
    try:
        data = file.file.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("Master CSV must be at most 1 MB.")
        records = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
        skus = [SKU.model_validate({**r, "weight_kg": r.get("weight_kg") or None, "quantity": r.get("quantity") or None, "ply": r.get("ply") or None}) for r in records]
        if not skus:
            raise ValueError("Master CSV contains no items.")
        workspace = Workspace(id=str(uuid4()), skus=skus)
    except (ValueError, UnicodeError):
        raise HTTPException(422, "Invalid master CSV. Use the template columns, unique SKU IDs, positive quantities, and positive weights (or blank).") from None
    finally:
        file.file.close()
    return comparison(app.state.repo.save(workspace, new=True))


@app.get("/api/workspaces/{workspace_id}/export")
def export_comparison(workspace_id: UUID):
    workspace = load(workspace_id)
    result = comparison(workspace)
    columns = [label for q in workspace.quotes for label in (q.vendor_name + " quoted price", q.vendor_name + " source unit", q.vendor_name + " landed INR/box", q.vendor_name + " source file", q.vendor_name + " evidence", q.vendor_name + " rebate status")]
    rows = [["SKU", "Description", "Factory", "Weight kg", "Quantity", *columns]]
    for row in result["rows"]:
        values = []
        for q in workspace.quotes:
            cell = row["prices"][q.vendor_id]
            values.extend([cell["quoted_price"], cell["price_unit"], cell["landed_price_per_box"], cell["source_file"], cell["source_reference"], cell["rebate"]["status"]])
        rows.append([row["sku_id"], row["description"], row["factory"], row["weight_kg"], row["quantity"], *values])
    rows.extend([[], ["Vendor", "QA", "Commercial terms confirmed", "Payment terms", "Fixed freight INR/order", "Tax %", "Notes"]])
    for q in workspace.quotes:
        rows.append([q.vendor_name, q.qa_passed, q.commercial_terms_confirmed, q.payment_terms, q.freight_per_order, q.tax_percent, " | ".join(q.notes)])
    rows.extend([[], ["Scenario: approved vendors, confirmed terms, fixed freight included, GST excluded"], ["Vendor", "SKUs", "Quantity", "Subtotal INR", "Freight INR", "Total INR"]])
    for a in result["recommended"]["awards"]:
        rows.append([a["vendor_name"], a["sku_count"], a["quantity"], a["subtotal"], a["freight"], a["total"]])
    rows.extend([["Award total INR", result["recommended"]["total"]], ["Award status", result["recommended"]["status"]]])
    return csv_response(rows, "procurement-comparison.csv")


@app.get("/api/sample-files")
def sample_files():
    from .sample_files import generate_sample_zip
    return Response(generate_sample_zip(), media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="sample-vendor-quotes.zip"'})


@app.get("/api/quotation-template")
def quotation_template():
    from .sample_files import generate_quotation_template
    return Response(generate_quotation_template(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": 'attachment; filename="supplier-quotation-template.xlsx"'})


@app.get("/api/comparison-test-files")
def comparison_test_files():
    import zipfile
    folder = settings.ROOT / "tests" / "fixtures" / "quotations"
    names = ["Apex_Containers.pdf", "PackCorp_India.xlsx"]
    if not all((folder / name).is_file() for name in names):
        raise HTTPException(404, "Test quotations are not included in this installation.")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.write(folder / name, arcname=name)
    return Response(output.getvalue(), media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="quotation-test-pack.zip"'})
