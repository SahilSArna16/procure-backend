"""Optional Python-only UI using the same API and calculation engine as React."""
import json
import os

import httpx
import pandas as pd
import streamlit as st

API = os.getenv("PROCUREMENT_API_URL", "http://127.0.0.1:8001")
st.set_page_config(page_title="Procure · Procurement Assistant", page_icon="📦", layout="wide")


def call(path, method="GET", **kwargs):
    try:
        response = httpx.request(method, API + "/api" + path, timeout=600, **kwargs)
        if response.is_error:
            detail = response.json().get("detail", "Request failed")
            st.error(detail if isinstance(detail, str) else "Invalid input. Check master SKU values and quote JSON.")
            return None
        return response.json()
    except (httpx.HTTPError, ValueError):
        st.error("Unable to reach the backend. Start uvicorn on port 8000 first.")
        return None


def accept(result):
    if result:
        st.session_state.comparison = result
        st.session_state.pop("scenario", None)


st.title("Procurement, with clarity.")
st.caption("Messy supplier responses. One clean comparison. Exact award calculations.")
left, right = st.columns(2)
if left.button("Load Sample Dataset", type="primary"):
    accept(call("/workspaces/sample", "POST"))
right.link_button("Download supplier quotation template", API + "/api/quotation-template")
st.link_button("Download master SKU CSV template", API + "/api/master-template")
master_csv = st.file_uploader("Import your master SKU list to start a real comparison", type=["csv"])
if master_csv and st.button("Create comparison from master CSV"):
    accept(call("/master-import", "POST", files={"file": (master_csv.name, master_csv.getvalue(), "text/csv")}))

data = st.session_state.get("comparison")
if not data:
    st.info("Load the 30-SKU demo or import a master list to get started.")
    st.stop()
workspace = data["workspace"]
wid = workspace["id"]
if workspace["is_sample"]:
    st.warning("Illustrative sample data. Live uploads start a separate comparison derived from the source quotations; demo master values are not reused.")
stats = data["stats"]
cols = st.columns(4)
cols[0].metric("Master SKUs", stats["sku_count"])
cols[1].metric("Supplier quotes", stats["vendor_count"])
cols[2].metric("QA-approved suppliers", stats["approved_vendor_count"])
cols[3].metric("Recommended total INR", data["recommended"]["total"] or "Unresolved")

uploads = st.file_uploader("Upload supplier responses · 15 MB/file · maximum 10 suppliers", type=["xlsx", "pdf", "docx", "png", "jpg", "jpeg", "txt"], accept_multiple_files=True)
if uploads and st.button("Extract uploaded quotes"):
    with st.spinner("Extracting quotes; scanned documents may take several minutes…"):
        current = workspace
        if current["is_sample"]:
            fresh = call("/workspaces/draft", "POST")
            if not fresh:
                st.stop()
            accept(fresh)
            current = fresh["workspace"]
        result = call(f'/workspaces/{current["id"]}/quotes', "POST", files=[("files", (f.name, f.getvalue(), f.type or "application/octet-stream")) for f in uploads])
        if result:
            accept(result["comparison"])
            st.session_state.upload_errors = result["errors"]
            st.rerun()
for error in st.session_state.get("upload_errors", []):
    st.error(error["file"] + ": " + error["message"])
for record in workspace.get("uploads", []):
    st.write(f"{record['name']} · {record['format']} · {record['status']} · {record['matched_count']} matched items")
    if record.get("error"):
        st.error(record["error"])
    if record["status"] == "failed":
        replacement = st.file_uploader("Select original file to retry", type=["xlsx", "pdf", "docx", "png", "jpg", "jpeg", "txt"], key="retry-file-" + record["id"])
        if replacement and st.button("Retry this file", key="retry-" + record["id"]):
            result = call(f"/workspaces/{wid}/quotes", "POST", data={"retry_upload_id": record["id"]}, files={"files": (replacement.name, replacement.getvalue(), replacement.type or "application/octet-stream")})
            if result:
                accept(result["comparison"])
                st.rerun()

comparison_tab, suppliers_tab, master_tab, scenario_tab = st.tabs(["Price comparison", "Supplier review", "Master SKUs", "Award scenarios"])
with comparison_tab:
    qa_only = st.checkbox("Show only QA-approved suppliers", value=False)
    quotes = [q for q in workspace["quotes"] if not qa_only or q["qa_passed"] is True]
    records = []
    for row in data["rows"]:
        record = {"SKU": row["sku_id"], "Description": row["description"], "Quantity": row["quantity"]}
        for q in quotes:
            cell = row["prices"][q["vendor_id"]]
            record[q["vendor_name"] + " quoted"] = f"{cell['quoted_price']} {cell['currency']} {cell['price_unit']}" if cell["quoted_price"] is not None else "Missing item"
            record[q["vendor_name"] + " landed INR/box"] = float(cell["landed_price_per_box"]) if cell["landed_price_per_box"] is not None else None
        records.append(record)
    frame = pd.DataFrame(records)
    approved_names = [q["vendor_name"] + " landed INR/box" for q in quotes]

    def highlight(row):
        candidates = [row[name] for name in approved_names if pd.notna(row[name])]
        minimum = min(candidates) if candidates else None
        return ["background-color: #e9f3df; color: #446c36" if name in approved_names and minimum is not None and row[name] == minimum else "" for name in row.index]

    st.dataframe(frame.style.apply(highlight, axis=1), hide_index=True, width="stretch")
    st.caption("Landed INR/box includes per-box freight, fixed freight allocated over the full basket, and only rebates meeting their complete conditions. GST excluded. Actual awards recalculate freight and rebate eligibility per supplier order.")
    for q in quotes:
        if q["payment_terms"] != "Net 60":
            st.warning(f'{q["vendor_name"]}: nonstandard payment terms — {q["payment_terms"]}')
    st.link_button("Export comparison CSV", f"{API}/api/workspaces/{wid}/export")

with suppliers_tab:
    for quote in workspace["quotes"]:
        with st.expander(quote["vendor_name"] + " · " + quote["source_file"]):
            st.caption("Update QA, commercial terms, rate/unit, discounts, freight and review flags. Keep unresolved historical prices null. Confirm terms only after checking the original document.")
            edited = st.text_area("Extracted quote JSON", json.dumps(quote, indent=2, ensure_ascii=False), height=360, key="quote-json-" + quote["vendor_id"])
            if st.button("Save quote and recalculate", key="save-" + quote["vendor_id"]):
                try:
                    updated = json.loads(edited)
                    next_workspace = {**workspace, "quotes": [updated if q["vendor_id"] == quote["vendor_id"] else q for q in workspace["quotes"]]}
                    result = call(f"/workspaces/{wid}", "PUT", json=next_workspace)
                    if result:
                        accept(result)
                        st.rerun()
                except json.JSONDecodeError:
                    st.error("Quote JSON is invalid. Fix the syntax and retry.")

with master_tab:
    edited_master = st.data_editor(pd.DataFrame(workspace["skus"]), hide_index=True, disabled=["sku_id"], key="master-" + wid)
    if st.button("Save master specifications"):
        records = json.loads(edited_master.to_json(orient="records"))
        result = call(f"/workspaces/{wid}", "PUT", json={**workspace, "skus": records})
        if result:
            accept(result)
            st.rerun()

with scenario_tab:
    a, b = st.columns(2)
    if a.button("Best QA-approved split award"):
        result = call(f"/workspaces/{wid}/scenario", "POST", json={"plan": {"kind": "split_award"}})
        if result:
            st.session_state.scenario = result
    if b.button("Compare single-supplier awards"):
        result = call(f"/workspaces/{wid}/scenario", "POST", json={"plan": {"kind": "compare_vendors"}})
        if result:
            st.session_state.scenario = result
    query = st.chat_input("What if we award to at most two QA-approved suppliers?")
    if query:
        with st.spinner("Planning and calculating…"):
            result = call(f"/workspaces/{wid}/scenario", "POST", json={"question": query})
            if result:
                st.session_state.scenario = result
    result = st.session_state.get("scenario", data["recommended"])
    st.write(result["summary"])
    if result.get("total"):
        st.metric("Exact award total (INR)", result["total"])
    if result.get("awards"):
        st.dataframe(pd.DataFrame(result["awards"]), hide_index=True)
    if result.get("vendors"):
        st.dataframe(pd.DataFrame(result["vendors"]), hide_index=True)
