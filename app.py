"""Deferral economics explorer.  streamlit run app.py

Battery sizes and the reinforcement scope come from run_feeder.py; the app re-prices them.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).parent / "results"
summary = json.loads((ROOT / "summary_feeder.json").read_text())
defer = pd.read_csv(ROOT / "deferral.csv")
hc = pd.read_csv(ROOT / "hosting_capacity.csv", index_col=0)

st.set_page_config(page_title="Feeder deferral", layout="wide")
st.title("Battery or reconductoring? IEEE 33-bus feeder")
st.caption("Sizes from the LinDistFlow MILP in run_feeder.py, re-priced live. Changing costs can change the optimal sizes; re-run the script for exact plans.")

with st.sidebar:
    rate = st.slider("Discount rate", 0.05, 0.15, 0.10, 0.005)
    km_cost = st.slider("Reconductoring, kUSD/km", 60, 250, 140, 5)
    kwh = st.slider("Battery energy, USD/kWh", 150, 450, 300, 10)
    kw = st.slider("Battery power, USD/kW", 50, 300, 150, 10)
    site = st.slider("Fixed cost per site, kUSD", 0, 200, 80, 10)
    cap = st.slider("Capacity payment, USD/MW-month", 0, 14_000, 11_619, 100)
    years = st.slider("Capacity contract, years", 0, 20, 15)

y0 = summary["first_violation_year"]
reinf = km_cost * 1e3 * summary["reinforcement"]["km"]
rows = []
for _, r in defer[defer.defer_years > 0].iterrows():
    n = int(r.defer_years)
    sites = len(ast.literal_eval(r.sites)) if isinstance(r.sites, str) else 0
    capex = kwh * 1e3 * r.bess_mwh + kw * 1e3 * r.bess_mw + site * 1e3 * sites
    saving = reinf / (1 + rate) ** y0 - reinf / (1 + rate) ** (y0 + n)
    pay = cap * 12 * r.bess_mw * sum(1 / (1 + rate) ** (y0 + k) for k in range(1, years + 1))
    rows.append({"Years deferred": n, "Battery MW": r.bess_mw, "Battery MWh": r.bess_mwh,
                 "Deferral only, kUSD": (saving - capex / (1 + rate) ** y0) / 1e3,
                 "With capacity contract, kUSD": (saving - capex / (1 + rate) ** y0 + pay) / 1e3})
df = pd.DataFrame(rows).set_index("Years deferred")

c = st.columns(4)
c[0].metric("First violation", f"year {y0}")
c[1].metric("Reinforcement", f"{reinf/1e6:.2f} MUSD")
c[2].metric("Best deferral only", f"{df['Deferral only, kUSD'].max():,.0f} kUSD")
c[3].metric("Best with contract", f"{df['With capacity contract, kUSD'].max():,.0f} kUSD")

st.subheader("Net present value of deferring")
st.bar_chart(df[["Deferral only, kUSD", "With capacity contract, kUSD"]])
st.dataframe(df.round(2))

st.subheader("PV hosting capacity per bus")
st.image(str(ROOT / "fig1_hosting_map.png"))
st.dataframe(hc.round(3))
