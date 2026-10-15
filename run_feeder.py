"""Hosting capacity and battery-as-non-wires-alternative on the IEEE 33-bus feeder.

    python run_feeder.py
"""
from __future__ import annotations

import copy
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from feeder.model import Costs, Limits, hosting_capacity, min_cost_plan
from feeder.network import ac_power_flow, branch_lengths_km, ieee33
from feeder.plotting import COLORS, style

OUT = Path(__file__).parent / "results"
RAW = Path(__file__).parent / "data" / "raw"
GROWTH = 0.035  # peak load growth per year (EVs, air conditioning, electrification)
HORIZON = 10  # reinforcement sized for this year's load
RATE = 0.10
ALMAGBA_USD_MW_MONTH = 11_619  # AlmaGBA award average, contracted with Edenor/Edesur
CONTRACT_YEARS = 15  # the battery keeps earning after the reinforcement is built

# schematic coordinates for drawing
XY = {**{b: (b - 1, 0) for b in range(1, 19)},
      **{b: (1 + (b - 19), -1.6) for b in range(19, 23)},
      **{b: (2 + (b - 23), 1.6) for b in range(23, 26)},
      **{b: (5 + (b - 26), 3.0) for b in range(26, 34)}}


def peak_day_shape() -> np.ndarray:
    d = pd.read_csv(RAW / "cammesa_sadi_demand_hourly.csv", index_col=0)["demand_mw"]
    d.index = pd.to_datetime(d.index, utc=True).tz_convert("America/Argentina/Buenos_Aires")
    day = d.groupby(d.index.date).max().idxmax()
    s = d[d.index.date == day].to_numpy()[:24]
    return s / s.max()


def loads(f, shape, g):
    p = f.p_nom.reindex(f.buses, fill_value=0.0).to_numpy()
    q = f.q_nom.reindex(f.buses, fill_value=0.0).to_numpy()
    return (pd.DataFrame(np.outer(shape * g, p), columns=f.buses),
            pd.DataFrame(np.outer(shape * g, q), columns=f.buses))


def upgraded(f, upgrades):
    g = copy.deepcopy(f)
    g.branches.loc[upgrades, ["r", "x"]] *= 0.5
    return g


def main() -> None:
    OUT.mkdir(exist_ok=True)
    style()
    f = ieee33(v_sub=1.03)  # OLTC set to 1.03 pu at the substation
    downstream = lambda j: [j] + sum((downstream(k) for k in f.children[j]), [])
    nominal = pd.Series({j: abs(complex(f.p_nom[downstream(j)].sum(), f.q_nom[downstream(j)].sum())) for j in f.branches.index})
    lim = Limits(rating_mva=np.maximum(1.2 * nominal, 2.0))
    costs = Costs()
    lengths = branch_lengths_km(f)
    shape = peak_day_shape()

    # ---- 1. PV hosting capacity, midday at 30% load ----
    p0 = f.p_nom.reindex(f.buses, fill_value=0.0)
    q0 = f.q_nom.reindex(f.buses, fill_value=0.0)
    no_thermal = Limits(v_min=lim.v_min, v_max=lim.v_max, rating_mva=lim.rating_mva * 100)
    hc = {}
    for b in f.buses[1:]:
        h = hosting_capacity(f, lim, b, 0.3 * p0, 0.3 * q0)
        h_v = hosting_capacity(f, no_thermal, b, 0.3 * p0, 0.3 * q0)
        inj = 0.3 * p0.copy()
        inj[b] -= h
        v_ac, _ = ac_power_flow(f, inj, 0.3 * q0)
        hc[b] = {"mw": h, "binding": "thermal" if h_v > h + 1e-3 else "voltage", "ac_vmax": float(v_ac.max())}
    hc = pd.DataFrame(hc).T
    hc.to_csv(OUT / "hosting_capacity.csv")

    # ---- 2. When does the feeder run out? ----
    def limit_growth(limits):
        ok = lambda g: np.isfinite(min_cost_plan(f, limits, costs, *loads(f, shape, g), allow_upgrades=False, allow_bess=False)[0].capex)
        lo, hi = 0.5, 2.0
        for _ in range(14):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if ok(mid) else (lo, mid)
        return lo

    g_star = limit_growth(lim)
    y0 = int(np.floor(np.log(g_star) / np.log(1 + GROWTH))) + 1  # first year with a violation
    print(f"feeder limit at {g_star:.3f} x today's peak -> first violation in year {y0}")

    # AlmaGBA operating criterion (CAMMESA/SACME, Oct 2026): batteries are called when a line
    # or transformer passes 85% of its capacity. When does this feeder first hit that, and what
    # battery keeps every branch at or below 85% up to the year the thermal limit would bind?
    lim85 = Limits(v_min=lim.v_min, v_max=lim.v_max, rating_mva=0.85 * lim.rating_mva)
    g85 = limit_growth(lim85)
    y85 = int(np.floor(np.log(g85) / np.log(1 + GROWTH))) + 1
    plan85, _ = min_cost_plan(f, lim85, costs, *loads(f, shape, (1 + GROWTH) ** (y0 - 1)), allow_upgrades=False, allow_bess=True)
    almagba = {
        "loading_trigger": 0.85, "growth_factor_at_trigger": g85, "first_year_above_85pct": y85,
        "bess_mw": sum(plan85.bess_mw.values()), "bess_mwh": sum(plan85.bess_mwh.values()),
        "sites": list(plan85.bess_mw), "capex_usd": plan85.capex,
    }
    print(f"85% loading first exceeded in year {y85}; battery to hold 85% until year {y0 - 1}: "
          f"{almagba['bess_mw']:.2f} MW / {almagba['bess_mwh']:.2f} MWh at {almagba['sites']}")

    g_h = (1 + GROWTH) ** HORIZON
    reinf, _ = min_cost_plan(f, lim, costs, *loads(f, shape, g_h), allow_upgrades=True, allow_bess=False, lengths_km=lengths)
    print(f"reinforcement for year {HORIZON}: {reinf.capex/1e6:.2f} MUSD on branches {reinf.upgrades}")

    # ---- 3. Battery to defer the reinforcement n years ----
    rows, plans = [], {}
    pv_reinf_now = reinf.capex / (1 + RATE) ** y0
    for n in range(0, 7):
        if n == 0:
            rows.append({"defer_years": 0, "bess_capex": 0.0, "bess_mw": 0.0, "bess_mwh": 0.0, "net_saving": 0.0, "with_almagba": 0.0})
            continue
        g_n = (1 + GROWTH) ** (y0 + n - 1)
        plan, out = min_cost_plan(f, lim, costs, *loads(f, shape, g_n), allow_upgrades=False, allow_bess=True)
        if not np.isfinite(plan.capex):
            break
        plans[n] = (plan, out)
        saving = pv_reinf_now - reinf.capex / (1 + RATE) ** (y0 + n)
        bess_pv = plan.capex / (1 + RATE) ** y0
        mw = sum(plan.bess_mw.values())
        annuity = sum(1 / (1 + RATE) ** (y0 + k) for k in range(1, CONTRACT_YEARS + 1))
        cap_pay = ALMAGBA_USD_MW_MONTH * 12 * mw * annuity
        rows.append({"defer_years": n, "bess_capex": plan.capex, "bess_mw": mw, "bess_mwh": sum(plan.bess_mwh.values()),
                     "sites": list(plan.bess_mw), "net_saving": saving - bess_pv, "with_almagba": saving - bess_pv + cap_pay})
        print(f"defer {n} y: BESS {mw:.2f} MW / {sum(plan.bess_mwh.values()):.2f} MWh at {list(plan.bess_mw)} -> net {(saving - bess_pv)/1e3:,.0f} kUSD")
    defer = pd.DataFrame(rows)
    defer.to_csv(OUT / "deferral.csv", index=False)

    # ---- AC check of the year-(y0+2) plans at the peak hour ----
    n_show = max(plans)  # the largest battery makes the mechanism easiest to see
    g_show = (1 + GROWTH) ** (y0 + n_show - 1)
    lp, lq = loads(f, shape, g_show)
    peak = int(np.argmax(shape))
    bess_plan, bess_out = plans[n_show]
    reinf_show, _ = min_cost_plan(f, lim, costs, lp, lq, allow_upgrades=True, allow_bess=False, lengths_km=lengths)
    v_none, _ = ac_power_flow(f, lp.iloc[peak], lq.iloc[peak])
    v_reinf, _ = ac_power_flow(upgraded(f, reinf_show.upgrades), lp.iloc[peak], lq.iloc[peak])
    inj = lp.iloc[peak].copy()
    for c in bess_plan.bess_mw:
        inj[c] -= bess_out[("bess", c)].iloc[peak]
    v_bess, _ = ac_power_flow(f, inj, lq.iloc[peak])

    # ---------- figures ----------
    # 1. Hosting capacity map
    fig, ax = plt.subplots(figsize=(11, 4.2))
    for j, i in f.parent.items():
        ax.plot(*zip(XY[i], XY[j]), color="#c3cad4", lw=2, zorder=1)
    vals = hc.mw.astype(float)
    sc = ax.scatter([XY[b][0] for b in vals.index], [XY[b][1] for b in vals.index], c=vals, cmap="Blues", s=170, edgecolor="#4b5664", zorder=2, vmin=0)
    ax.scatter(*XY[1], marker="s", s=220, color=COLORS[1], zorder=3)
    ax.annotate("Substation\n1.03 pu", XY[1], textcoords="offset points", xytext=(-8, -36), fontsize=8.5)
    for b in vals.index:
        ax.annotate(f"{vals[b]:.1f}", XY[b], ha="center", va="center", fontsize=7, color="white" if vals[b] > 3 else "#1a1a1a", zorder=4)
    ax.set_axis_off()
    fig.colorbar(sc, ax=ax, shrink=0.7, label="PV hosting capacity, MW")
    ax.set_title("PV hosting capacity per bus at midday minimum load (IEEE 33-bus, 12.66 kV)", loc="left")
    fig.tight_layout()
    fig.savefig(OUT / "fig1_hosting_map.png", dpi=160)
    plt.close(fig)

    # 2. Voltage profiles
    order = list(range(1, 19))
    fig, ax = plt.subplots(figsize=(10, 4.2))
    for v, label, color in [(v_none, "No action", COLORS[3]), (v_reinf, "Reconductoring", COLORS[0]), (v_bess, "Battery", COLORS[2])]:
        ax.plot(order, v[order], "o-", color=color, label=label, ms=4)
    ax.axhline(lim.v_min, color=COLORS[1], ls="--", lw=1)
    ax.text(1, lim.v_min - 0.004, f"limit {lim.v_min} pu", color=COLORS[1], fontsize=8.5, va="top")
    ax.set_xticks(order)
    ax.set_xlabel("Bus along the main feeder")
    ax.set_ylabel("Voltage, pu (AC power flow)")
    ax.set_title(f"Peak hour in year {y0 + n_show - 1}: both options fix the voltage, checked with a full AC power flow", loc="left")
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(OUT / "fig2_voltage_profiles.png", dpi=160)
    plt.close(fig)

    # 3. Battery dispatch on the peak day
    fig, ax = plt.subplots(figsize=(10, 4))
    hours = np.arange(24)
    head = lp.sum(axis=1).to_numpy()
    net = head - bess_out["bess"].sum(axis=1).to_numpy()
    ax.plot(hours, head, color=COLORS[3], lw=1.6, label="Feeder load")
    ax.plot(hours, net, color=COLORS[0], lw=2, label="Seen by the network with battery")
    ax.bar(hours, bess_out["bess"].sum(axis=1), color=COLORS[2], alpha=0.7, label="Battery (+ discharge, − charge)")
    ax.set_xlabel("Hour of the peak day (shape: CAMMESA SADI max-demand day)")
    ax.set_ylabel("MW")
    ax.set_title("The battery shaves the evening peak and recharges overnight", loc="left")
    ax.legend(frameon=False, fontsize=9)
    fig.tight_layout()
    fig.savefig(OUT / "fig3_peak_day.png", dpi=160)
    plt.close(fig)

    # 4. Deferral value
    d = defer[defer.defer_years > 0]
    fig, ax = plt.subplots(figsize=(10, 4))
    w = 0.38
    ax.bar(d.defer_years - w / 2, d.net_saving / 1e3, w, color=COLORS[0], label="Deferral only")
    ax.bar(d.defer_years + w / 2, d.with_almagba / 1e3, w, color=COLORS[2], label="Deferral + 15-year AlmaGBA-type capacity contract")
    ax.axhline(0, color="#999", lw=0.8)
    ax.set_xlabel("Years the reinforcement is deferred")
    ax.set_ylabel("Net present value, kUSD")
    ax.set_title(f"Value of deferring a {reinf.capex/1e6:.1f} MUSD reinforcement with a battery", loc="left")
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(OUT / "fig4_deferral_value.png", dpi=160)
    plt.close(fig)

    summary = {
        "hosting_capacity_mw": {"min": float(vals.min()), "max": float(vals.max()), "bus_min": int(vals.idxmin()),
                                 "max_ac_vmax_at_hc": float(hc.ac_vmax.max())},
        "limit_growth_factor": g_star, "first_violation_year": y0,
        "almagba_85pct_criterion": almagba,
        "reinforcement": {"capex_usd": reinf.capex, "branches": reinf.upgrades, "km": float(lengths[reinf.upgrades].sum())},
        "deferral": defer.to_dict(orient="records"),
        "ac_check_min_voltage": {"none": float(v_none.min()), "reinforced": float(v_reinf.min()), "bess": float(v_bess.min())},
        "assumptions": {"growth": GROWTH, "horizon": HORIZON, "rate": RATE, "limits": {"v_min": lim.v_min, "v_max": lim.v_max},
                        "costs": asdict(costs), "ratings": "max(1.2 x nominal peak flow, 2 MVA)"},
    }
    (OUT / "summary_feeder.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps({k: v for k, v in summary.items() if k != "deferral"}, indent=2, default=str))


if __name__ == "__main__":
    main()
