import os
from pathlib import Path

from streamlit.testing.v1 import AppTest

os.environ.setdefault("PROCUREMENT_API_URL", "http://127.0.0.1:8001")
app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "streamlit_app.py"), default_timeout=30).run()
assert not app.exception, [e.message for e in app.exception]
app.button[0].click().run()
assert not app.exception, [e.message for e in app.exception]
assert len(app.metric) >= 4
assert app.metric[0].value == "30"
assert app.metric[1].value == "5"
assert len(app.dataframe) >= 2
print("PASS Streamlit: startup, live sample API request, 30 SKUs, five vendors, styled matrix and award summary")
