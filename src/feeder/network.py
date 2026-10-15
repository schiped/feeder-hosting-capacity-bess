"""IEEE 33-bus radial distribution feeder (Baran & Wu, 1989) and an AC power flow to check it.

12.66 kV, 3.715 MW + 2.300 Mvar nominal load. Buses are numbered 1..33, bus 1 is
the substation. Impedances in ohm, loads in kW / kvar at the receiving bus.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

KV = 12.66
S_BASE_MVA = 1.0
Z_BASE = KV**2 / S_BASE_MVA

#        from to   R ohm    X ohm   P kW  Q kvar
_BRANCHES = [
    (1, 2, 0.0922, 0.0470, 100, 60), (2, 3, 0.4930, 0.2511, 90, 40),
    (3, 4, 0.3660, 0.1864, 120, 80), (4, 5, 0.3811, 0.1941, 60, 30),
    (5, 6, 0.8190, 0.7070, 60, 20), (6, 7, 0.1872, 0.6188, 200, 100),
    (7, 8, 0.7114, 0.2351, 200, 100), (8, 9, 1.0300, 0.7400, 60, 20),
    (9, 10, 1.0440, 0.7400, 60, 20), (10, 11, 0.1966, 0.0650, 45, 30),
    (11, 12, 0.3744, 0.1238, 60, 35), (12, 13, 1.4680, 1.1550, 60, 35),
    (13, 14, 0.5416, 0.7129, 120, 80), (14, 15, 0.5910, 0.5260, 60, 10),
    (15, 16, 0.7463, 0.5450, 60, 20), (16, 17, 1.2890, 1.7210, 60, 20),
    (17, 18, 0.7320, 0.5740, 90, 40), (2, 19, 0.1640, 0.1565, 90, 40),
    (19, 20, 1.5042, 1.3554, 90, 40), (20, 21, 0.4095, 0.4784, 90, 40),
    (21, 22, 0.7089, 0.9373, 90, 40), (3, 23, 0.4512, 0.3083, 90, 50),
    (23, 24, 0.8980, 0.7091, 420, 200), (24, 25, 0.8960, 0.7011, 420, 200),
    (6, 26, 0.2030, 0.1034, 60, 25), (26, 27, 0.2842, 0.1447, 60, 25),
    (27, 28, 1.0590, 0.9337, 60, 20), (28, 29, 0.8042, 0.7006, 120, 70),
    (29, 30, 0.5075, 0.2585, 200, 600), (30, 31, 0.9744, 0.9630, 150, 70),
    (31, 32, 0.3105, 0.3619, 210, 100), (32, 33, 0.3410, 0.5302, 60, 40),
]


@dataclass
class Feeder:
    branches: pd.DataFrame  # index = receiving bus j; columns: from, r, x (pu), p, q (MW, Mvar nominal)
    buses: list[int]
    parent: dict[int, int]
    children: dict[int, list[int]]
    v_sub: float = 1.0

    @property
    def p_nom(self) -> pd.Series:
        return self.branches.p

    @property
    def q_nom(self) -> pd.Series:
        return self.branches.q


def ieee33(v_sub: float = 1.0) -> Feeder:
    df = pd.DataFrame(_BRANCHES, columns=["from", "to", "r_ohm", "x_ohm", "p_kw", "q_kvar"]).set_index("to")
    br = pd.DataFrame({
        "from": df["from"],
        "r": df.r_ohm / Z_BASE,
        "x": df.x_ohm / Z_BASE,
        "r_ohm": df.r_ohm,
        "p": df.p_kw / 1e3,
        "q": df.q_kvar / 1e3,
    })
    buses = [1] + list(br.index)
    parent = br["from"].to_dict()
    children = {b: [j for j, i in parent.items() if i == b] for b in buses}
    return Feeder(br, buses, parent, children, v_sub)


def ac_power_flow(f: Feeder, p: pd.Series, q: pd.Series, tol: float = 1e-9) -> tuple[pd.Series, float]:
    """Backward-forward sweep. p, q: net load per bus in MW/Mvar (negative = injection).

    Returns bus voltage magnitudes (pu) and total losses (MW).
    """
    order = list(f.branches.index)  # parents always precede children in the table
    v = {b: complex(f.v_sub, 0) for b in f.buses}
    for _ in range(100):
        i_load = {b: np.conj(complex(p.get(b, 0.0), q.get(b, 0.0)) / S_BASE_MVA / v[b]) for b in f.buses}
        i_br = {}
        for j in reversed(order):
            i_br[j] = i_load[j] + sum(i_br[k] for k in f.children[j])
        v_old = dict(v)
        for j in order:
            z = complex(f.branches.at[j, "r"], f.branches.at[j, "x"])
            v[j] = v[f.parent[j]] - z * i_br[j]
        if max(abs(v[b] - v_old[b]) for b in f.buses) < tol:
            break
    losses = sum(abs(i_br[j]) ** 2 * f.branches.at[j, "r"] for j in order) * S_BASE_MVA
    return pd.Series({b: abs(v[b]) for b in f.buses}), losses


def branch_lengths_km(f: Feeder, ohm_per_km: float = 0.30) -> pd.Series:
    """Proxy line lengths from resistance (IEEE 33 publishes impedances, not lengths)."""
    return f.branches.r_ohm / ohm_per_km
