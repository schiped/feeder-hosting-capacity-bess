"""Download the CAMMESA demand used for the peak-day load shape.

Source: CAMMESA public demand API (5-minute SADI demand; only the most recent
~6 months are served), resampled to hourly. The raw file is not redistributed
in this repository; run this script before run_feeder.py.

    python data/fetch_data.py
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import requests

RAW = Path(__file__).parent / "raw"
CAMMESA_URL = (
    "https://api.cammesa.com/demanda-svc/demanda/"
    "ObtieneDemandaYTemperaturaRegionByFecha"
)
SADI_REGION = 1002  # whole interconnected system


def fetch_cammesa(days_back: int = 200, region: int = SADI_REGION) -> Path:
    today = dt.date.today()
    frames = []
    for d in range(days_back, 0, -1):
        day = today - dt.timedelta(days=d)
        r = requests.get(CAMMESA_URL, params={"fecha": day.isoformat(), "id_region": region}, timeout=30)
        r.raise_for_status()
        rows = r.json()
        if rows:
            frames.append(pd.DataFrame(rows))
    if not frames:
        raise RuntimeError("CAMMESA API returned no data")
    df = pd.concat(frames, ignore_index=True)
    df["time"] = pd.to_datetime(df["fecha"], format="%Y-%m-%dT%H:%M:%S.%f%z")
    hourly = (
        df.set_index("time")[["dem", "temp"]]
        .sort_index()
        .resample("1h")
        .mean()
        .dropna(subset=["dem"])
        .rename(columns={"dem": "demand_mw", "temp": "temp_c"})
    )
    out = RAW / "cammesa_sadi_demand_hourly.csv"
    hourly.to_csv(out)
    print(f"CAMMESA: {len(hourly)} hours, {hourly.index.min()} -> {hourly.index.max()}")
    return out


if __name__ == "__main__":
    RAW.mkdir(parents=True, exist_ok=True)
    fetch_cammesa()
