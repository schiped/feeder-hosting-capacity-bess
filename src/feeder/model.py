"""LinDistFlow optimisation on a radial feeder (Pyomo + HiGHS).

Two questions on the same network model:
  hosting_capacity  - largest PV a bus can take before voltage or thermal limits bind
  min_cost_plan     - cheapest way to serve a grown peak day: reconductor branches
                      (binary per branch) and/or batteries at up to N sites (binary per bus)

LinDistFlow (Baran & Wu) drops losses from the flow balance and keeps the linear
voltage drop  v_j = v_i - 2 (r P_ij + x Q_ij)  with v the squared voltage. Thermal
limits use an octagon around the apparent-power circle. A reconductored branch has
half the impedance and twice the rating; the product of the upgrade binary and the
flow is linearised exactly with big-M.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import pyomo.environ as pyo

from .network import Feeder

SQ2 = np.sqrt(2)


@dataclass
class Limits:
    v_min: float = 0.93
    v_max: float = 1.07
    planning_margin: float = 0.005  # covers LinDistFlow's neglected losses in planning runs
    rating_mva: pd.Series | None = None  # per branch (indexed by receiving bus)


@dataclass
class Costs:
    reconductor_usd_per_km: float = 140_000
    bess_usd_per_kwh: float = 300.0
    bess_usd_per_kw: float = 150.0
    bess_site_usd: float = 80_000
    max_bess_sites: int = 2
    eta: float = 0.92  # one-way


@dataclass
class Plan:
    upgrades: list[int] = field(default_factory=list)
    bess_mw: dict[int, float] = field(default_factory=dict)
    bess_mwh: dict[int, float] = field(default_factory=dict)
    capex: float = 0.0


def _base_model(f: Feeder, lim: Limits, load_p: pd.DataFrame, load_q: pd.DataFrame, margin: float = 0.0):
    """Variables and constraints shared by every question. load_*: hours x buses (MW, Mvar)."""
    m = pyo.ConcreteModel()
    m.T = pyo.RangeSet(0, len(load_p) - 1)
    m.B = pyo.Set(initialize=list(f.branches.index))  # branch = receiving bus
    m.N = pyo.Set(initialize=f.buses)
    m.P = pyo.Var(m.B, m.T)
    m.Q = pyo.Var(m.B, m.T)
    m.v = pyo.Var(m.N, m.T, bounds=((lim.v_min + margin) ** 2, (lim.v_max - margin) ** 2))
    m.inj = pyo.Var(m.N, m.T)  # extra active injection at a bus (PV or battery), MW
    for t in m.T:
        m.v[1, t].fix(f.v_sub**2)
    m.lp = load_p.reset_index(drop=True)
    m.lq = load_q.reset_index(drop=True)
    return m


def _flows(m, f: Feeder, upgrade_vars=None, big_m: float = 20.0):
    br = f.branches

    def p_bal(m, j, t):
        return m.P[j, t] == m.lp.at[t, j] - m.inj[j, t] + sum(m.P[k, t] for k in f.children[j])

    def q_bal(m, j, t):
        return m.Q[j, t] == m.lq.at[t, j] + sum(m.Q[k, t] for k in f.children[j])

    m.pbal = pyo.Constraint(m.B, m.T, rule=p_bal)
    m.qbal = pyo.Constraint(m.B, m.T, rule=q_bal)

    if upgrade_vars is None:
        def vdrop(m, j, t):
            r, x = br.at[j, "r"], br.at[j, "x"]
            return m.v[j, t] == m.v[f.parent[j], t] - 2 * (r * m.P[j, t] + x * m.Q[j, t])
        m.vdrop = pyo.Constraint(m.B, m.T, rule=vdrop)
        return

    y = upgrade_vars
    m.wP = pyo.Var(m.B, m.T)
    m.wQ = pyo.Var(m.B, m.T)
    m.lin = pyo.ConstraintList()
    for j in m.B:
        for t in m.T:
            for w, flow in ((m.wP[j, t], m.P[j, t]), (m.wQ[j, t], m.Q[j, t])):
                m.lin.add(w <= big_m * y[j])
                m.lin.add(w >= -big_m * y[j])
                m.lin.add(w <= flow + big_m * (1 - y[j]))
                m.lin.add(w >= flow - big_m * (1 - y[j]))

    def vdrop(m, j, t):
        r, x = br.at[j, "r"], br.at[j, "x"]
        return m.v[j, t] == m.v[f.parent[j], t] - 2 * (
            r * (m.P[j, t] - 0.5 * m.wP[j, t]) + x * (m.Q[j, t] - 0.5 * m.wQ[j, t])
        )
    m.vdrop = pyo.Constraint(m.B, m.T, rule=vdrop)


def _thermal(m, rating: pd.Series, y=None):
    m.th = pyo.ConstraintList()
    for j in m.B:
        for t in m.T:
            s = rating[j] * (1 + y[j]) if y is not None else rating[j]
            P, Q = m.P[j, t], m.Q[j, t]
            for a, b in ((1, 0), (0, 1)):
                m.th.add(a * P + b * Q <= s)
                m.th.add(a * P + b * Q >= -s)
            for a, b in ((1, 1), (1, -1)):
                m.th.add(a * P + b * Q <= SQ2 * s)
                m.th.add(a * P + b * Q >= -SQ2 * s)


def _solve(m, mip_gap: float = 1e-4) -> bool:
    opt = pyo.SolverFactory("appsi_highs")
    opt.options["mip_rel_gap"] = mip_gap
    res = opt.solve(m, load_solutions=False)
    if res.solver.termination_condition != pyo.TerminationCondition.optimal:
        return False
    m.solutions.load_from(res)
    return True


def hosting_capacity(f: Feeder, lim: Limits, bus: int, p_load: pd.Series, q_load: pd.Series) -> float:
    """Max PV (MW) at one bus for a single snapshot (typically midday minimum load)."""
    m = _base_model(f, lim, p_load.to_frame().T, q_load.to_frame().T)
    m.pv = pyo.Var(bounds=(0, 50))
    for b in f.buses:
        if b != bus:
            m.inj[b, 0].fix(0)
    m.link = pyo.Constraint(expr=m.inj[bus, 0] == m.pv)
    _flows(m, f)
    _thermal(m, lim.rating_mva)
    m.obj = pyo.Objective(expr=m.pv, sense=pyo.maximize)
    _solve(m)
    return float(pyo.value(m.pv))


def min_cost_plan(
    f: Feeder, lim: Limits, costs: Costs, load_p: pd.DataFrame, load_q: pd.DataFrame,
    allow_upgrades: bool = True, allow_bess: bool = True, lengths_km: pd.Series | None = None,
) -> tuple[Plan, pd.DataFrame | None]:
    m = _base_model(f, lim, load_p, load_q, margin=lim.planning_margin)
    cand = [b for b in f.buses if b != 1]
    m.C = pyo.Set(initialize=cand)

    # upgrades
    m.y = pyo.Var(m.B, domain=pyo.Binary)
    if not allow_upgrades:
        for j in m.B:
            m.y[j].fix(0)
    # batteries
    m.site = pyo.Var(m.C, domain=pyo.Binary)
    m.pw = pyo.Var(m.C, bounds=(0, 5))
    m.en = pyo.Var(m.C, bounds=(0, 30))
    m.ch = pyo.Var(m.C, m.T, within=pyo.NonNegativeReals)
    m.dis = pyo.Var(m.C, m.T, within=pyo.NonNegativeReals)
    m.soc = pyo.Var(m.C, m.T, within=pyo.NonNegativeReals)
    m.bc = pyo.ConstraintList()
    if not allow_bess:
        for c in cand:
            m.site[c].fix(0)
    m.bc.add(sum(m.site[c] for c in m.C) <= costs.max_bess_sites)
    n = len(load_p)
    for c in cand:
        m.bc.add(m.pw[c] <= 5 * m.site[c])
        m.bc.add(m.en[c] <= 30 * m.site[c])
        for t in m.T:
            m.bc.add(m.ch[c, t] <= m.pw[c])
            m.bc.add(m.dis[c, t] <= m.pw[c])
            m.bc.add(m.soc[c, t] <= m.en[c])
            prev = m.soc[c, n - 1] if t == 0 else m.soc[c, t - 1]  # daily cycle
            m.bc.add(m.soc[c, t] == prev + costs.eta * m.ch[c, t] - m.dis[c, t] / costs.eta)
    for b in f.buses:
        for t in m.T:
            if b in cand:
                m.bc.add(m.inj[b, t] == m.dis[b, t] - m.ch[b, t])
            else:
                m.inj[b, t].fix(0)

    _flows(m, f, upgrade_vars=m.y)
    _thermal(m, lim.rating_mva, y=m.y)
    lengths_km = lengths_km if lengths_km is not None else pd.Series(1.0, index=f.branches.index)
    capex = (
        sum(costs.reconductor_usd_per_km * lengths_km[j] * m.y[j] for j in m.B)
        + sum(costs.bess_site_usd * m.site[c] + costs.bess_usd_per_kw * 1e3 * m.pw[c] + costs.bess_usd_per_kwh * 1e3 * m.en[c] for c in m.C)
    )
    # tiny throughput penalty keeps the battery from cycling for no reason
    m.obj = pyo.Objective(expr=capex + 1e-3 * sum(m.dis[c, t] for c in m.C for t in m.T), sense=pyo.minimize)
    if not _solve(m, mip_gap=1e-3):
        return Plan(capex=float("inf")), None
    plan = Plan(
        upgrades=[j for j in m.B if pyo.value(m.y[j]) > 0.5],
        bess_mw={c: float(pyo.value(m.pw[c])) for c in m.C if pyo.value(m.site[c]) > 0.5},
        bess_mwh={c: float(pyo.value(m.en[c])) for c in m.C if pyo.value(m.site[c]) > 0.5},
        capex=float(pyo.value(capex)),
    )
    volts = pd.DataFrame({b: [np.sqrt(pyo.value(m.v[b, t])) for t in m.T] for b in f.buses})
    disp = pd.DataFrame({c: [pyo.value(m.dis[c, t] - m.ch[c, t]) for t in m.T] for c in plan.bess_mw})
    return plan, pd.concat({"v": volts, "bess": disp}, axis=1)
