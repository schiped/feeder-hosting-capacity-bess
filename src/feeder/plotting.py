"""Shared chart style for every figure in the portfolio."""
import matplotlib.pyplot as plt

COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#7d8794", "#8a5cd1"]


def style() -> None:
    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "font.size": 10,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.edgecolor": "#c3cad4",
        "axes.grid": True,
        "grid.color": "#e6eaf1",
        "grid.linewidth": 0.8,
        "axes.titlesize": 11.5,
        "axes.titleweight": "semibold",
        "axes.labelcolor": "#4b5664",
        "xtick.color": "#7d8794",
        "ytick.color": "#7d8794",
        "figure.facecolor": "white",
    })
