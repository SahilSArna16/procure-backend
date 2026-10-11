import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import settings
from app.main import app
from app.parsers import parse_document
from app.sample_files import generate_sample_zip


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "sqlite")
    monkeypatch.setattr(settings, "SQLITE_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as client:
        yield client


def test_sample_round_trip_scenario_update_export_and_conflict(client):
    response = client.post("/api/workspaces/sample")
    assert response.status_code == 200
    data = response.json()
    w = data["workspace"]
    path = "/api/workspaces/" + w["id"]
    assert client.get(path).json()["recommended"]["total"] == "4648443.90"
    assert client.post(path + "/scenario", json={"plan": {"kind": "split_award", "max_suppliers": 1}}).json()["total"] == "4719880.00"
    original = dict(w)
    w["skus"][0]["quantity"] = 10000
    saved = client.put(path, json=w)
    assert saved.status_code == 200
    assert saved.json()["workspace"]["revision"] == 2
    assert client.put(path, json=original).status_code == 409
    exported = client.get(path + "/export")
    assert exported.status_code == 200
    assert "Award total INR" in exported.text
    assert "Apex Containers" in exported.text


def test_master_import_and_empty_workspace(client):
    template = client.get("/api/master-template")
    result = client.post("/api/master-import", files={"file": ("master.csv", template.content, "text/csv")})
    assert result.status_code == 200
    assert len(result.json()["rows"]) == 30
    assert result.json()["workspace"]["quotes"] == []
    assert result.json()["recommended"]["status"] == "infeasible"
    assert client.post("/api/workspaces", json=[]).status_code == 422


def test_invalid_workspace_unknown_vendor_and_sample_upload(client):
    data = client.post("/api/workspaces/sample").json()
    path = "/api/workspaces/" + data["workspace"]["id"]
    assert client.get("/api/workspaces/not-a-uuid").status_code == 422
    assert client.post(path + "/scenario", json={"plan": {"kind": "single_vendor", "vendor_id": "invented"}}).status_code == 422
    assert client.post(path + "/quotes", files={"files": ("quote.txt", b"hello", "text/plain")}).status_code == 409


def test_upload_partial_success_and_manual_qa_required(client, monkeypatch):
    from app.sample import sample_workspace
    w = sample_workspace()
    data = client.post("/api/workspaces", json=[s.model_dump(mode="json") for s in w.skus]).json()
    path = "/api/workspaces/" + data["workspace"]["id"]
    def extract(filename, _data, _workspace):
        if filename.endswith(".pdf"):
            raise ValueError("Encrypted PDF")
        q = w.quotes[0].model_copy(deep=True)
        q.qa_passed = None
        q.commercial_terms_confirmed = False
        return q
    monkeypatch.setattr("app.main.extract_quote", extract)
    result = client.post(path + "/quotes", files=[("files", ("valid.txt", b"quote", "text/plain")), ("files", ("bad.pdf", b"bad", "application/pdf"))]).json()
    assert result["added"] == 1
    assert result["errors"][0]["message"] == "Encrypted PDF"
    assert result["comparison"]["recommended"]["status"] == "infeasible"
    assert len(client.get(path).json()["workspace"]["quotes"]) == 1


def test_real_demo_files_parse_and_pdf_footnote_is_retained():
    with zipfile.ZipFile(io.BytesIO(generate_sample_zip())) as archive:
        excel = parse_document("apex_quote.xlsx", archive.read("apex_quote.xlsx"))
        pdf = parse_document("packwell_quote.pdf", archive.read("packwell_quote.pdf"))
        word = parse_document("greenbox_quote.docx", archive.read("greenbox_quote.docx"))
        assert "Material ref" in excel and "SKU-030" in excel
        assert "5%" in pdf and "7,500" in pdf and "SKU-030" in pdf
        assert "SKU-030" in word
        assert parse_document("shree_pricelist.png", archive.read("shree_pricelist.png")) is None
