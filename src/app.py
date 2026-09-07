"""Streamlit UI.

Calls the API over HTTP. It does NOT load the model — one process owns the
artifact, so the UI cannot drift from the API's transform, and the UI container
stays small enough to fit beside the API on a single t4g.small node.

Run:  streamlit run src/app.py --server.port 8501
"""
from __future__ import annotations

import io
import os

import pandas as pd
import requests
import streamlit as st

API_URL = os.getenv("API_URL", "http://localhost:8000")
REQUIRED = ["brand", "zone", "date", "value_lakhs", "numeric_dist_pct", "wtd_dist_val",
            "num_stores", "ms_value", "volume_tonnes", "sah", "str_days", "pdo_rs"]

st.set_page_config(page_title="Chocolate Sales Forecast", layout="wide")
st.title("Brand-wise / Zone-wise Chocolate Sales Forecast")


@st.cache_data(ttl=30)
def api_health():
    try:
        r = requests.get(f"{API_URL}/health", timeout=5)
        return r.status_code, r.json()
    except Exception as exc:                      # noqa: BLE001
        return 0, {"error": str(exc)}


status, health = api_health()
cols = st.columns(4)
if status == 200:
    cols[0].metric("API", "ready")
    cols[1].metric("Model version", health.get("model_version", "?"))
    cols[2].metric("Estimator", health.get("estimator", "?"))
    cols[3].metric("Contract", f"v{health.get('contract_version', '?')}")
else:
    st.error(f"API not ready (HTTP {status}). Predictions are unavailable.")
    st.json(health)

st.divider()

with st.sidebar:
    st.header("Input")
    st.caption(
        "The payload must be **zone-complete**: every brand present in every zone. "
        "Two features are derived from the zone's category total, so a partial "
        "brand set produces a wrong denominator and is rejected with a 422."
    )
    uploaded = st.file_uploader("Monthly panel CSV", type=["csv"])
    include_ai = st.checkbox("Include All-India roll-up", value=True)
    allow_fb = st.checkbox("Allow naive fallback if the model is down", value=False)
    st.caption("Fallback results are tagged `source=fallback` and must not be "
               "mistaken for a model prediction.")

if uploaded is None:
    st.info(
        "Upload the monthly panel to forecast the next month.\n\n"
        f"Required columns: `{', '.join(REQUIRED)}`\n\n"
        "At least 7 months of history per brand x zone."
    )
    st.stop()

panel = pd.read_csv(uploaded)
missing = [c for c in REQUIRED if c not in panel.columns]
if missing:
    st.error(f"Missing required columns: {missing}")
    st.stop()

panel["date"] = pd.to_datetime(panel["date"]).dt.strftime("%Y-%m-%d")
c1, c2, c3 = st.columns(3)
c1.metric("Rows", f"{len(panel):,}")
c2.metric("Series", panel.groupby(["brand", "zone"]).ngroups)
c3.metric("Latest month", panel["date"].max())

zones = sorted(panel["zone"].unique())
chosen = st.multiselect("Zones to forecast", zones, default=zones)
subset = panel[panel["zone"].isin(chosen)]

if st.button("Forecast next month", type="primary", disabled=(status != 200 and not allow_fb)):
    payload = {"history": subset[REQUIRED].to_dict("records"),
               "include_all_india": include_ai}
    with st.spinner("Calling the API..."):
        try:
            r = requests.post(f"{API_URL}/v1/predict",
                              params={"allow_fallback": str(allow_fb).lower()},
                              json=payload, timeout=60)
        except Exception as exc:                  # noqa: BLE001
            st.error(f"Request failed: {exc}")
            st.stop()

    if r.status_code == 422:
        st.error("Contract violation — the payload was rejected rather than served.")
        st.json(r.json())
        st.stop()
    if r.status_code != 200:
        st.error(f"HTTP {r.status_code}")
        st.json(r.json())
        st.stop()

    body = r.json()
    out = pd.DataFrame(body["predictions"])

    if body["source"] == "fallback":
        st.warning("**source = fallback.** These are naive persistence values "
                   "(next month = this month), NOT model predictions.")
    else:
        st.success(f"source = model | version `{body['model_version']}` "
                   f"| target month {body['target_month']}")

    ai = out[out["zone"] == "All India (U+R)"]
    zn = out[out["zone"] != "All India (U+R)"]

    if len(ai):
        st.subheader(f"All-India forecast — {body['target_month']}")
        k1, k2, k3 = st.columns(3)
        k1.metric("Category total (Rs Lakhs)", f"{ai['prediction'].sum():,.0f}",
                  f"{100 * (ai['prediction'].sum() / ai['last_actual'].sum() - 1):+.1f}%")
        k2.metric("Brands", len(ai))
        k3.metric("Largest brand", ai.loc[ai["prediction"].idxmax(), "brand"])
        st.dataframe(
            ai[["brand", "last_actual", "prediction", "pct_change"]]
              .sort_values("prediction", ascending=False)
              .style.format({"last_actual": "{:,.1f}", "prediction": "{:,.1f}",
                             "pct_change": "{:+.1f}%"}),
            width="stretch")

    st.subheader("Brand x zone detail")
    st.dataframe(
        zn[["brand", "zone", "last_actual", "prediction", "pct_change"]]
          .sort_values(["zone", "prediction"], ascending=[True, False])
          .style.format({"last_actual": "{:,.1f}", "prediction": "{:,.1f}",
                         "pct_change": "{:+.1f}%"}),
        width="stretch", height=420)

    buf = io.StringIO()
    out.to_csv(buf, index=False)
    st.download_button("Download forecast CSV", buf.getvalue(),
                       file_name=f"forecast_{body['target_month']}.csv", mime="text/csv")

    with st.expander("Response metadata"):
        st.json({k: v for k, v in body.items() if k != "predictions"})
