
from __future__ import annotations

APP_VERSION = "v8-fenix3-custom-hr"

import html
import io
import json
import math
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from fit_workout_writer import FitWorkoutStep, FitCustomHrRangeStep, create_fit_workout, create_fit_workout_custom_hr_ranges


def _parse_iso_time(value: str) -> datetime:
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def as_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def parse_gpx_bytes(data: bytes) -> pd.DataFrame:
    root = ET.fromstring(data)
    ns = {
        "gpx": "http://www.topografix.com/GPX/1/1",
        "gpxtpx": "http://www.garmin.com/xmlschemas/TrackPointExtension/v1",
    }
    trkpts = root.findall(".//gpx:trkpt", ns) or root.findall(".//trkpt")

    points: list[dict[str, Any]] = []
    first_time: datetime | None = None
    last_latlon: tuple[float, float] | None = None
    cumulative_distance_m = 0.0

    for trkpt in trkpts:
        lat = float(trkpt.attrib.get("lat"))
        lon = float(trkpt.attrib.get("lon"))

        ele_el = trkpt.find("gpx:ele", ns) or trkpt.find("ele")
        time_el = trkpt.find("gpx:time", ns) or trkpt.find("time")

        ts = _parse_iso_time(time_el.text) if time_el is not None and time_el.text else None
        if first_time is None and ts is not None:
            first_time = ts
        elapsed_s = (ts - first_time).total_seconds() if ts is not None and first_time is not None else len(points) * 5.0

        altitude = float(ele_el.text) if ele_el is not None and ele_el.text else None

        hr = None
        for elem in trkpt.iter():
            tag = elem.tag.split("}")[-1].lower()
            if tag == "hr" and elem.text:
                try:
                    hr = float(elem.text)
                    break
                except ValueError:
                    pass

        if last_latlon is not None:
            cumulative_distance_m += haversine_m(last_latlon[0], last_latlon[1], lat, lon)
        last_latlon = (lat, lon)

        points.append(
            {
                "elapsed_s": elapsed_s,
                "timestamp": ts,
                "latitude": lat,
                "longitude": lon,
                "altitude_m": altitude,
                "heart_rate": hr,
                "distance_m": cumulative_distance_m,
            }
        )

    df = pd.DataFrame(points)
    if df.empty:
        raise ValueError("No trackpoints found in GPX.")
    return add_derived_columns(df)


def parse_json_bytes(data: bytes) -> pd.DataFrame:
    obj = json.loads(data.decode("utf-8"))

    if isinstance(obj, dict) and "features" in obj:
        raise ValueError("This looks like a summary JSON. Upload GPX or trace JSON with point-level heart_rate/duration.")
    if not isinstance(obj, list):
        raise ValueError("JSON must be a list of point samples.")

    rows: list[dict[str, Any]] = []
    first_ts_ms = None

    for item in obj:
        if not isinstance(item, dict):
            continue
        ts_ms = item.get("timestamp")
        if first_ts_ms is None and ts_ms is not None:
            first_ts_ms = ts_ms

        if item.get("duration") is not None:
            elapsed_s = float(item["duration"]) / 1000.0
        elif ts_ms is not None and first_ts_ms is not None:
            elapsed_s = (float(ts_ms) - float(first_ts_ms)) / 1000.0
        else:
            elapsed_s = len(rows) * 5.0

        ts = datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=timezone.utc) if ts_ms is not None else None
        rows.append(
            {
                "elapsed_s": elapsed_s,
                "timestamp": ts,
                "heart_rate": as_float_or_none(item.get("heart_rate")),
                "distance_m": as_float_or_none(item.get("distance")),
                "speed_mps": as_float_or_none(item.get("speed")),
                "altitude_m": as_float_or_none(item.get("altitude")),
                "latitude": as_float_or_none(item.get("latitude")),
                "longitude": as_float_or_none(item.get("longitude")),
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError("No usable samples found in JSON.")
    return add_derived_columns(df)


def add_derived_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy().sort_values("elapsed_s").reset_index(drop=True)
    if "distance_m" not in df.columns:
        df["distance_m"] = None
    if "speed_mps" not in df.columns or df["speed_mps"].isna().all():
        if df["distance_m"].notna().sum() >= 2:
            df["speed_mps"] = df["distance_m"].diff() / df["elapsed_s"].diff()
        else:
            df["speed_mps"] = None
    df["elapsed_min"] = df["elapsed_s"] / 60.0
    df["time_label"] = df["elapsed_s"].apply(format_duration)
    return df


def format_duration(seconds: float | int | None) -> str:
    if seconds is None or pd.isna(seconds):
        return ""
    seconds = int(round(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def resample_track(df: pd.DataFrame, interval_s: int, smooth_window: int, method: str, old_hrmax: int) -> pd.DataFrame:
    work = df.copy()
    work["bin_s"] = (work["elapsed_s"] // interval_s * interval_s).astype(int)

    agg = {
        "elapsed_s": "mean",
        "heart_rate": "median" if method == "Median" else "mean",
        "distance_m": "max",
        "speed_mps": "mean",
        "altitude_m": "mean",
    }
    out = work.groupby("bin_s", as_index=False).agg({k: v for k, v in agg.items() if k in work.columns})

    out.loc[(out["heart_rate"] < 25) | (out["heart_rate"] > 245), "heart_rate"] = None
    out["heart_rate"] = out["heart_rate"].interpolate(limit_direction="both")
    out["heart_rate_smooth"] = (
        out["heart_rate"].rolling(window=smooth_window, center=True, min_periods=1).median()
        if smooth_window > 1
        else out["heart_rate"]
    )
    out["old_hr_pct"] = out["heart_rate_smooth"] / float(old_hrmax) * 100.0
    out["elapsed_min"] = out["elapsed_s"] / 60.0
    out["time_label"] = out["elapsed_s"].apply(format_duration)
    return out


def build_zone_defs(z2_bpm: int, z3_bpm: int, z4_bpm: int, z5_bpm: int, old_hrmax: int, new_hrmax: int, zwo_power: dict[int, float]) -> list[dict[str, Any]]:
    raw = [
        ("Z1", 1, 0, z2_bpm),
        ("Z2", 2, z2_bpm, z3_bpm),
        ("Z3", 3, z3_bpm, z4_bpm),
        ("Z4", 4, z4_bpm, z5_bpm),
        ("Z5", 5, z5_bpm, 999),
    ]
    out = []
    for zone, zone_num, low_bpm, high_bpm in raw:
        low_pct = low_bpm / old_hrmax * 100.0 if low_bpm > 0 else 0.0
        high_pct = high_bpm / old_hrmax * 100.0 if high_bpm < 999 else 100.0
        out.append(
            {
                "zone": zone,
                "zone_num": zone_num,
                "low_bpm": low_bpm,
                "high_bpm": high_bpm,
                "low_pct": low_pct,
                "high_pct": high_pct,
                "new_bpm_reference": pct_to_bpm_range(low_pct, high_pct, new_hrmax),
                "zwo_power": float(zwo_power.get(zone_num, 0.50)),
            }
        )
    return out


def zone_from_hr(hr: float | None, zone_defs: list[dict[str, Any]]) -> dict[str, Any]:
    if hr is None or pd.isna(hr):
        return zone_defs[0]
    for z in zone_defs:
        if hr >= z["low_bpm"] and hr < z["high_bpm"]:
            return z
    return zone_defs[-1]


def pct_to_bpm_range(low_pct: float, high_pct: float, hrmax: int) -> str:
    low = int(round(hrmax * low_pct / 100.0))
    if high_pct >= 100:
        return f"{low}+ bpm"
    high = int(round(hrmax * high_pct / 100.0)) - 1
    return f"{low}-{high} bpm"


def build_blocks(df: pd.DataFrame, zone_defs: list[dict[str, Any]], min_block_s: int) -> pd.DataFrame:
    rows = df.copy()
    rows["zone_info"] = rows["heart_rate_smooth"].apply(lambda hr: zone_from_hr(hr, zone_defs))
    rows["zone"] = rows["zone_info"].apply(lambda z: z["zone"])
    rows["zone_num"] = rows["zone_info"].apply(lambda z: z["zone_num"])
    rows["zwo_power"] = rows["zone_info"].apply(lambda z: z["zwo_power"])

    blocks = []
    cur = None
    start_s = end_s = None
    hrs, pcts, powers = [], [], []

    for _, row in rows.iterrows():
        z = row["zone"]
        zn = int(row["zone_num"])
        power = float(row["zwo_power"])
        t = float(row["bin_s"])
        hr = row["heart_rate_smooth"]
        pct = row["old_hr_pct"]

        if cur is None:
            cur = (z, zn, power)
            start_s = end_s = t
            hrs, pcts, powers = [hr], [pct], [power]
            continue

        if z == cur[0]:
            end_s = t
            hrs.append(hr)
            pcts.append(pct)
            powers.append(power)
        else:
            blocks.append(make_block(start_s, end_s, cur[0], cur[1], cur[2], hrs, pcts, zone_defs))
            cur = (z, zn, power)
            start_s = end_s = t
            hrs, pcts, powers = [hr], [pct], [power]

    if cur is not None:
        blocks.append(make_block(start_s, end_s, cur[0], cur[1], cur[2], hrs, pcts, zone_defs))

    interval_s = int(rows["bin_s"].diff().dropna().median()) if len(rows) >= 2 else 15
    for b in blocks:
        b["end_s"] += interval_s
        b["duration_s"] = max(0, b["end_s"] - b["start_s"])

    merged = []
    for b in blocks:
        if not merged:
            merged.append(b)
        elif b["duration_s"] < min_block_s:
            prev = merged[-1]
            prev["end_s"] = b["end_s"]
            prev["duration_s"] = prev["end_s"] - prev["start_s"]
            prev["old_avg_hr"] = round((prev["old_avg_hr"] + b["old_avg_hr"]) / 2.0, 1)
            prev["old_avg_pct"] = round((prev["old_avg_pct"] + b["old_avg_pct"]) / 2.0, 1)
            prev["old_max_hr"] = max(prev["old_max_hr"], b["old_max_hr"])
            prev["old_max_pct"] = max(prev["old_max_pct"], b["old_max_pct"])
            # Keep previous zone/power to reduce chatter.
        else:
            merged.append(b)

    out = pd.DataFrame(merged)
    if not out.empty:
        out["start"] = out["start_s"].apply(format_duration)
        out["end"] = out["end_s"].apply(format_duration)
        out["duration"] = out["duration_s"].apply(format_duration)
        out = out[
            [
                "start", "end", "duration", "zone", "zone_num", "old_bpm_range",
                "target_pct_range", "new_bpm_reference", "zwo_power",
                "old_avg_hr", "old_avg_pct", "old_max_hr", "old_max_pct",
                "start_s", "end_s", "duration_s",
            ]
        ]
    return out


def make_block(start_s, end_s, zone, zone_num, zwo_power, hrs, pcts, zone_defs):
    hr_values = [float(x) for x in hrs if x is not None and not pd.isna(x)]
    pct_values = [float(x) for x in pcts if x is not None and not pd.isna(x)]
    zdef = next(z for z in zone_defs if z["zone"] == zone)
    high = zdef["high_bpm"]
    old_bpm_range = f"{zdef['low_bpm']}+ bpm" if high >= 999 else f"{zdef['low_bpm']}-{high-1} bpm"
    target_pct_range = f"{zdef['low_pct']:.1f}-{zdef['high_pct']:.1f}% HRmax" if zdef["high_pct"] < 100 else f"{zdef['low_pct']:.1f}%+ HRmax"
    return {
        "start_s": float(start_s),
        "end_s": float(end_s),
        "duration_s": max(0.0, float(end_s) - float(start_s)),
        "zone": zone,
        "zone_num": int(zone_num),
        "old_bpm_range": old_bpm_range,
        "target_pct_range": target_pct_range,
        "new_bpm_reference": zdef["new_bpm_reference"],
        "zwo_power": float(zwo_power),
        "old_avg_hr": round(sum(hr_values) / len(hr_values), 1) if hr_values else None,
        "old_avg_pct": round(sum(pct_values) / len(pct_values), 1) if pct_values else None,
        "old_max_hr": round(max(hr_values), 1) if hr_values else None,
        "old_max_pct": round(max(pct_values), 1) if pct_values else None,
    }


def blocks_to_fit_steps(blocks: pd.DataFrame) -> list[FitWorkoutStep]:
    steps = []
    for _, b in blocks.iterrows():
        zone_num = int(b["zone_num"])
        duration_s = max(1, int(round(float(b["duration_s"]))))
        intensity = 5 if zone_num >= 3 else 0
        steps.append(FitWorkoutStep(duration_s=duration_s, zone=zone_num, name=f"{b['zone']} {b['duration']}", intensity=intensity))
    if steps:
        steps[0].intensity = 2
        steps[-1].intensity = 3
    return steps



# ---- Fenix 3 custom HR range export helpers, added in v8 ----------------------

def parse_bpm_range(value: Any) -> tuple[int, int]:
    """Parse strings like '143-151 bpm' or '171+ bpm' into a bpm range."""
    text = str(value or "").strip()
    m = re.search(r"(\d+)\s*-\s*(\d+)", text)
    if m:
        low = int(m.group(1))
        high = int(m.group(2))
        return low, max(low + 1, high)

    m = re.search(r"(\d+)\s*\+", text)
    if m:
        low = int(m.group(1))
        return low, low + 10

    m = re.search(r"(\d+)", text)
    if m:
        low = int(m.group(1))
        return low, low + 10

    return 120, 130


def blocks_to_custom_hr_range_steps(blocks: pd.DataFrame) -> list[FitCustomHrRangeStep]:
    """Convert edited/generated blocks to explicit bpm target range steps.

    Uses each block's new_bpm_reference, e.g. '150-160 bpm'.
    The FIT writer applies Fenix 3 +100 encoding internally.
    """
    steps: list[FitCustomHrRangeStep] = []
    for _, b in blocks.iterrows():
        duration_s = max(1, int(round(float(b["duration_s"]))))
        low, high = parse_bpm_range(b.get("new_bpm_reference", ""))

        zone_label = str(b.get("zone", "HR"))
        duration_label = str(b.get("duration", format_duration(duration_s)))
        name = f"{zone_label} {low}-{high}"

        try:
            zone_num = int(b.get("zone_num", 1))
        except Exception:
            zone_num = 1

        intensity = 5 if zone_num >= 3 else 0
        steps.append(
            FitCustomHrRangeStep(
                duration_s=duration_s,
                hr_low=low,
                hr_high=high,
                name=name,
                intensity=intensity,
            )
        )

    if steps:
        steps[0].intensity = 2
        steps[-1].intensity = 3
    return steps


def create_fit_for_blocks(workout_name: str, blocks: pd.DataFrame, export_mode: str) -> bytes:
    """Create FIT bytes from blocks using selected Garmin export mode."""
    if export_mode.startswith("Fenix 3 custom HR range"):
        return create_fit_workout_custom_hr_ranges(
            workout_name[:31],
            blocks_to_custom_hr_range_steps(blocks),
            fenix3_offset_100=True,
        )

    return create_fit_workout(workout_name[:31], blocks_to_fit_steps(blocks))

# ---- End Fenix 3 custom HR range export helpers -------------------------------


def blocks_to_zwo(blocks: pd.DataFrame, workout_name: str, description: str) -> str:
    """
    ZWO is power/FTP based. This export maps HR zones to configurable %FTP values.
    Rouvy/other apps then drive the trainer power target, which is intended to
    evoke a similar HR profile indoors.
    """
    safe_name = html.escape(workout_name)
    safe_desc = html.escape(description)
    lines = [
        '<workout_file>',
        f'  <author>HR Replay Builder</author>',
        f'  <name>{safe_name}</name>',
        f'  <description>{safe_desc}</description>',
        '  <sportType>bike</sportType>',
        '  <tags>',
        '    <tag name="HR Replay"/>',
        '  </tags>',
        '  <workout>',
    ]

    for _, b in blocks.iterrows():
        dur = int(round(float(b["duration_s"])))
        power = float(b["zwo_power"])
        zone = html.escape(str(b["zone"]))
        comment = html.escape(f"{zone} {b['duration']} source HR {b['old_bpm_range']}")
        # SteadyState is broadly supported in ZWO readers.
        lines.append(f'    <SteadyState Duration="{dur}" Power="{power:.3f}" pace="0" Cadence="90">')
        lines.append(f'      <textevent timeoffset="0" message="{comment}"/>')
        lines.append('    </SteadyState>')

    lines += [
        '  </workout>',
        '</workout_file>',
        '',
    ]
    return "\n".join(lines)


def blocks_to_yaml(blocks: pd.DataFrame, workout_name: str, old_hrmax: int, new_hrmax: int, zone_defs: list[dict[str, Any]]) -> str:
    lines = [
        f'name: "{workout_name}"',
        "sport: run",
        "export_policy:",
        "  fit: garmin_hr_zone_targets",
        "  zwo: power_proxy_for_hr_profile",
        "classification_policy: old_bpm_boundaries",
        f"old_hrmax_bpm: {old_hrmax}",
        f"new_hrmax_bpm: {new_hrmax}",
        "zones:",
    ]
    for z in zone_defs:
        lines.extend(
            [
                f"  - zone: {z['zone']}",
                f"    garmin_zone_number: {z['zone_num']}",
                f"    old_low_bpm: {z['low_bpm']}",
                f"    old_high_bpm: {z['high_bpm'] if z['high_bpm'] < 999 else 'open'}",
                f"    old_low_pct_hrmax: {z['low_pct']:.2f}",
                f"    old_high_pct_hrmax: {z['high_pct']:.2f}",
                f"    zwo_power_pct_ftp: {z['zwo_power']:.3f}",
                f"    new_bpm_reference: \"{z['new_bpm_reference']}\"",
            ]
        )
    lines.append("steps:")
    for _, b in blocks.iterrows():
        lines.extend(
            [
                f"  - duration: \"{b['duration']}\"",
                "    fit_target_type: heart_rate_zone",
                f"    fit_target_zone: {int(b['zone_num'])}",
                f"    zone_label: \"{b['zone']}\"",
                f"    source_old_bpm_range: \"{b['old_bpm_range']}\"",
                f"    zwo_power_pct_ftp: {b['zwo_power']:.3f}",
                f"    source_old_avg_hr: {b['old_avg_hr']}",
            ]
        )
    return "\n".join(lines) + "\n"


ZONE_COLORS = {
    "Z1": "rgba(170, 170, 170, 0.14)",
    "Z2": "rgba(0, 160, 0, 0.13)",
    "Z3": "rgba(0, 120, 255, 0.12)",
    "Z4": "rgba(255, 170, 0, 0.16)",
    "Z5": "rgba(255, 0, 0, 0.14)",
}


def plot_hr_profile(df: pd.DataFrame, zone_defs: list[dict[str, Any]], blocks: pd.DataFrame, show_blocks: bool) -> go.Figure:
    fig = go.Figure()

    if show_blocks and not blocks.empty:
        for _, b in blocks.iterrows():
            x0 = float(b["start_s"]) / 60.0
            x1 = float(b["end_s"]) / 60.0
            fig.add_vrect(
                x0=x0,
                x1=x1,
                fillcolor=ZONE_COLORS.get(b["zone"], "rgba(200,200,200,0.12)"),
                opacity=1.0,
                layer="below",
                line_width=0,
            )
            mid = (x0 + x1) / 2.0
            fig.add_annotation(
                x=mid,
                y=1.02,
                yref="paper",
                text=f"{b['zone']}<br>{b['duration']}",
                showarrow=False,
                font=dict(size=10),
                align="center",
            )

    fig.add_trace(
        go.Scatter(
            x=df["elapsed_min"],
            y=df["heart_rate_smooth"],
            mode="lines+markers",
            name="Heart rate",
            customdata=df[["old_hr_pct"]],
            hovertemplate="Time=%{x:.1f} min<br>HR=%{y:.0f} bpm<br>% old HRmax=%{customdata[0]:.1f}%<extra></extra>",
        )
    )

    for z in zone_defs[1:]:
        fig.add_hline(y=z["low_bpm"], line_dash="dash", annotation_text=f"{z['zone']} {z['low_bpm']} bpm")

    fig.update_layout(
        height=760,
        margin=dict(l=20, r=20, t=70, b=20),
        title="Heart-rate replay profile with generated instructions",
        xaxis_title="Elapsed time (min)",
        yaxis_title="Pulse / heart rate (bpm)",
    )
    return fig


def plot_power_proxy(blocks: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    if not blocks.empty:
        xs, ys = [], []
        for _, b in blocks.iterrows():
            x0 = float(b["start_s"]) / 60.0
            x1 = float(b["end_s"]) / 60.0
            p = float(b["zwo_power"]) * 100.0
            xs += [x0, x1]
            ys += [p, p]
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", name="ZWO %FTP proxy", line_shape="hv"))
    fig.update_layout(
        height=320,
        title="ZWO power proxy generated from HR zones",
        xaxis_title="Elapsed time (min)",
        yaxis_title="%FTP",
        margin=dict(l=20, r=20, t=50, b=20),
    )
    return fig




def infer_original_datetime_from_filename(name: str) -> datetime | None:
    """
    Supports filenames such as:
        2011-01-23_17-52-40-UTC_xxx.gpx
        2011-01-23_17-52-40-UTC_xxx.json
    """
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})", name)
    if not m:
        return None
    y, mo, d, h, mi, s = map(int, m.groups())
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def safe_name(value: str, max_len: int = 64) -> str:
    value = re.sub(r"[^A-Za-z0-9_\-]+", "_", value).strip("_")
    return (value or "workout")[:max_len]


def parse_uploaded_workout(uploaded) -> dict[str, Any]:
    data = uploaded.read()
    if uploaded.name.lower().endswith(".gpx"):
        raw = parse_gpx_bytes(data)
        kind = "gpx"
    elif uploaded.name.lower().endswith(".json"):
        raw = parse_json_bytes(data)
        kind = "json"
    else:
        raise ValueError("Unsupported file type. Use GPX or trace JSON.")

    file_dt = infer_original_datetime_from_filename(uploaded.name)
    sample_dt = None
    if "timestamp" in raw.columns:
        ts = raw["timestamp"].dropna()
        if not ts.empty:
            sample_dt = ts.iloc[0]

    original_dt = file_dt or sample_dt or datetime(1970, 1, 1, tzinfo=timezone.utc)

    duration_s = float(raw["elapsed_s"].max()) if not raw.empty else 0.0
    distance_m = float(raw["distance_m"].max()) if "distance_m" in raw and raw["distance_m"].notna().any() else 0.0
    hr = raw["heart_rate"].dropna() if "heart_rate" in raw else pd.Series(dtype=float)

    return {
        "name": uploaded.name,
        "kind": kind,
        "raw": raw,
        "original_dt": original_dt,
        "original_date": original_dt.date(),
        "duration_s": duration_s,
        "distance_m": distance_m,
        "avg_hr": float(hr.mean()) if not hr.empty else None,
        "max_hr": float(hr.max()) if not hr.empty else None,
    }


def build_date_preserving_plan(items: list[dict[str, Any]], new_start_date: date) -> list[dict[str, Any]]:
    if not items:
        return []

    sorted_items = sorted(items, key=lambda x: x["original_dt"])
    first_original_date = sorted_items[0]["original_date"]

    plan: list[dict[str, Any]] = []
    for i, item in enumerate(sorted_items):
        offset_days = (item["original_date"] - first_original_date).days
        new_date = new_start_date + timedelta(days=offset_days)

        prev_orig_gap = None
        prev_new_gap = None
        if i > 0:
            prev_orig_gap = (item["original_date"] - sorted_items[i - 1]["original_date"]).days
            prev_new_gap = (new_date - plan[i - 1]["new_date"]).days

        row = dict(item)
        row["offset_days"] = offset_days
        row["new_date"] = new_date
        row["days_since_previous_original"] = prev_orig_gap
        row["days_since_previous_new"] = prev_new_gap
        row["pause_days"] = None if prev_orig_gap is None else max(0, prev_orig_gap - 1)
        plan.append(row)

    return plan


def plan_dataframe(plan: list[dict[str, Any]]) -> pd.DataFrame:
    weekdays_no = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]
    rows = []
    for idx, item in enumerate(plan, start=1):
        rows.append(
            {
                "#": idx,
                "original_date": item["original_date"].isoformat(),
                "original_weekday": weekdays_no[item["original_date"].weekday()],
                "new_date": item["new_date"].isoformat(),
                "new_weekday": weekdays_no[item["new_date"].weekday()],
                "pause_days": "" if item["pause_days"] is None else item["pause_days"],
                "duration": format_duration(item["duration_s"]),
                "distance_km": round(item["distance_m"] / 1000.0, 2) if item["distance_m"] else None,
                "avg_hr": round(item["avg_hr"]) if item["avg_hr"] else None,
                "max_hr": round(item["max_hr"]) if item["max_hr"] else None,
                "file": item["name"],
            }
        )
    return pd.DataFrame(rows)


def build_workout_outputs(
    item: dict[str, Any],
    sequence_no: int,
    zone_defs: list[dict[str, Any]],
    old_hrmax: int,
    new_hrmax: int,
    interval_s: int,
    smooth_window: int,
    method: str,
    min_block_s: int,
    fit_export_mode: str,
) -> dict[str, Any]:
    sampled = resample_track(item["raw"], interval_s, smooth_window, method, old_hrmax)
    blocks = build_blocks(sampled, zone_defs, min_block_s)


    workout_name = f"{item['new_date'].strftime('%Y%m%d')}_{sequence_no:02d}_HRZ"
    fit_bytes = create_fit_for_blocks(workout_name[:31], blocks, fit_export_mode)

    zwo_text = blocks_to_zwo(
        blocks,
        workout_name,
        "Generated from historical HR profile. ZWO is %FTP power proxy, not true HR target.",
    )

    yaml_text = blocks_to_yaml(blocks, workout_name, old_hrmax, new_hrmax, zone_defs)

    stem = f"{item['new_date'].isoformat()}_{sequence_no:02d}_{safe_name(item['name'].rsplit('.', 1)[0])}"
    return {
        "sampled": sampled,
        "blocks": blocks,
        "workout_name": workout_name,
        "fit_filename": f"{stem}.fit",
        "fit_bytes": fit_bytes,
        "zwo_filename": f"{stem}.zwo",
        "zwo_text": zwo_text,
        "yaml_filename": f"{stem}.yaml",
        "yaml_text": yaml_text,
    }


def build_plan_zip(
    plan: list[dict[str, Any]],
    plan_df: pd.DataFrame,
    zone_defs: list[dict[str, Any]],
    old_hrmax: int,
    new_hrmax: int,
    interval_s: int,
    smooth_window: int,
    method: str,
    min_block_s: int,
    fit_export_mode: str,
) -> bytes:
    buffer = io.BytesIO()
    audit = []

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("plan.csv", plan_df.to_csv(index=False))
        z.writestr("zone_definitions.json", json.dumps(zone_defs, indent=2, ensure_ascii=False))

        for seq, item in enumerate(plan, start=1):
            outputs = build_workout_outputs(
                item,
                seq,
                zone_defs,
                old_hrmax,
                new_hrmax,
                interval_s,
                smooth_window,
                method,
                min_block_s,
                fit_export_mode,
            )
            z.writestr(f"fit/{outputs['fit_filename']}", outputs["fit_bytes"])
            z.writestr(f"zwo/{outputs['zwo_filename']}", outputs["zwo_text"])
            z.writestr(f"audit/{outputs['yaml_filename']}", outputs["yaml_text"])

            audit.append(
                {
                    "original_file": item["name"],
                    "original_date": item["original_date"].isoformat(),
                    "new_date": item["new_date"].isoformat(),
                    "pause_days": item["pause_days"],
                    "duration_s": item["duration_s"],
                    "distance_m": item["distance_m"],
                    "num_steps": int(len(outputs["blocks"])),
                    "fit_export_mode": fit_export_mode,
                    "fit_file": f"fit/{outputs['fit_filename']}",
                    "zwo_file": f"zwo/{outputs['zwo_filename']}",
                    "audit_file": f"audit/{outputs['yaml_filename']}",
                }
            )

        z.writestr("audit.json", json.dumps(audit, indent=2, ensure_ascii=False))

    return buffer.getvalue()


# ---- Manual block editing helpers, added in v7 --------------------------------

def init_manual_blocks_state(state_key: str, blocks: pd.DataFrame) -> None:
    """Initialize session-state manual blocks from generated blocks.

    Manual editing is intentionally based on block boundaries, not raw samples.
    This keeps the FIT/ZWO export deterministic while allowing human corrections.
    """
    if state_key not in st.session_state:
        st.session_state[state_key] = blocks.copy()


def reset_manual_blocks_state(state_key: str, blocks: pd.DataFrame) -> None:
    st.session_state[state_key] = blocks.copy()


def normalize_manual_blocks(blocks: pd.DataFrame) -> pd.DataFrame:
    """Recompute derived columns after manual edits."""
    out = blocks.copy().reset_index(drop=True)
    if out.empty:
        return out

    out["start_s"] = out["start_s"].astype(float)
    out["end_s"] = out["end_s"].astype(float)
    out["duration_s"] = (out["end_s"] - out["start_s"]).clip(lower=1)
    out["start"] = out["start_s"].apply(format_duration)
    out["end"] = out["end_s"].apply(format_duration)
    out["duration"] = out["duration_s"].apply(format_duration)

    # Keep blocks contiguous.
    for i in range(1, len(out)):
        out.loc[i, "start_s"] = out.loc[i - 1, "end_s"]
    out["duration_s"] = (out["end_s"] - out["start_s"]).clip(lower=1)
    out["start"] = out["start_s"].apply(format_duration)
    out["end"] = out["end_s"].apply(format_duration)
    out["duration"] = out["duration_s"].apply(format_duration)
    return out


def move_block_boundary(blocks: pd.DataFrame, boundary_index: int, delta_s: int, min_duration_s: int) -> pd.DataFrame:
    """Move the boundary between block boundary_index and boundary_index+1.

    Example: boundary_index=3 moves the end of block 3 and start of block 4.
    Positive delta makes the left block longer and the right block shorter.
    Negative delta makes the left block shorter and the right block longer.
    """
    out = blocks.copy().reset_index(drop=True)
    if out.empty or boundary_index < 0 or boundary_index >= len(out) - 1:
        return out

    left = boundary_index
    right = boundary_index + 1

    current_boundary = float(out.loc[left, "end_s"])
    left_min = float(out.loc[left, "start_s"]) + float(min_duration_s)
    right_max = float(out.loc[right, "end_s"]) - float(min_duration_s)

    new_boundary = current_boundary + float(delta_s)
    new_boundary = max(left_min, min(right_max, new_boundary))

    out.loc[left, "end_s"] = new_boundary
    out.loc[right, "start_s"] = new_boundary
    return normalize_manual_blocks(out)


def apply_zone_edit(blocks: pd.DataFrame, row_index: int, zone_label: str, zone_defs: list[dict[str, Any]], new_hrmax: int) -> pd.DataFrame:
    """Change zone for one block."""
    out = blocks.copy().reset_index(drop=True)
    if out.empty or row_index < 0 or row_index >= len(out):
        return out

    zdef = next((z for z in zone_defs if z["zone"] == zone_label), None)
    if not zdef:
        return out

    out.loc[row_index, "zone"] = zdef["zone"]
    out.loc[row_index, "zone_num"] = int(zdef["zone_num"])
    if "new_bpm_reference" in out.columns:
        out.loc[row_index, "new_bpm_reference"] = pct_to_bpm_range(zdef["low_pct"], zdef["high_pct"], new_hrmax)
    return normalize_manual_blocks(out)


def merge_neighbor_blocks(blocks: pd.DataFrame, row_index: int, direction: str) -> pd.DataFrame:
    """Merge selected block with previous or next block."""
    out = blocks.copy().reset_index(drop=True)
    if len(out) < 2:
        return out

    if direction == "previous":
        a, b = row_index - 1, row_index
    else:
        a, b = row_index, row_index + 1

    if a < 0 or b >= len(out):
        return out

    out.loc[a, "end_s"] = out.loc[b, "end_s"]
    out.loc[a, "duration_s"] = out.loc[a, "end_s"] - out.loc[a, "start_s"]

    # Keep the zone of the first block. This is deliberate: it gives predictable manual control.
    out = out.drop(index=b).reset_index(drop=True)
    return normalize_manual_blocks(out)


def split_block(blocks: pd.DataFrame, row_index: int, min_duration_s: int) -> pd.DataFrame:
    """Split a block in two equal pieces, keeping same zone."""
    out = blocks.copy().reset_index(drop=True)
    if out.empty or row_index < 0 or row_index >= len(out):
        return out

    start_s = float(out.loc[row_index, "start_s"])
    end_s = float(out.loc[row_index, "end_s"])
    duration = end_s - start_s
    if duration < 2 * min_duration_s:
        return out

    mid = start_s + duration / 2.0
    first = out.loc[row_index].copy()
    second = out.loc[row_index].copy()
    first["end_s"] = mid
    second["start_s"] = mid

    new_rows = []
    for i in range(len(out)):
        if i == row_index:
            new_rows.append(first)
            new_rows.append(second)
        else:
            new_rows.append(out.loc[i])
    return normalize_manual_blocks(pd.DataFrame(new_rows).reset_index(drop=True))


def blocks_key_from_source(name: str, settings_signature: str = "") -> str:
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", name)[:80]
    return f"manual_blocks_{safe}_{settings_signature}"


def plot_hr_profile_with_manual_blocks(sampled: pd.DataFrame, blocks: pd.DataFrame, zone_defs: list[dict[str, Any]], old_hrmax: int, title: str):
    """Plot HR curve and manually edited instruction spans."""
    fig = go.Figure()

    # Zone background spans from blocks.
    zone_colors = {
        "Z1": "rgba(180, 180, 180, 0.18)",
        "Z2": "rgba(80, 180, 80, 0.18)",
        "Z3": "rgba(80, 140, 220, 0.18)",
        "Z4": "rgba(230, 170, 40, 0.20)",
        "Z5": "rgba(230, 80, 80, 0.20)",
    }

    if blocks is not None and not blocks.empty:
        for _, b in blocks.iterrows():
            x0 = float(b["start_s"]) / 60.0
            x1 = float(b["end_s"]) / 60.0
            z = str(b["zone"])
            fig.add_vrect(
                x0=x0,
                x1=x1,
                fillcolor=zone_colors.get(z, "rgba(200,200,200,0.12)"),
                line_width=0,
                layer="below",
            )
            fig.add_annotation(
                x=(x0 + x1) / 2.0,
                y=1.06,
                xref="x",
                yref="paper",
                text=f"{z}<br>{b['duration']}",
                showarrow=False,
                font=dict(size=9),
            )

    fig.add_trace(
        go.Scatter(
            x=sampled["elapsed_min"],
            y=sampled["heart_rate_smooth"],
            mode="lines+markers",
            name="Heart rate",
            customdata=sampled[["old_hr_pct"]] if "old_hr_pct" in sampled.columns else None,
            hovertemplate="Time=%{x:.1f} min<br>HR=%{y:.0f} bpm<br>% old HRmax=%{customdata[0]:.1f}%<extra></extra>",
        )
    )

    # HR boundary lines.
    for z in zone_defs[1:]:
        bpm = old_hrmax * z["low_pct"] / 100.0
        fig.add_hline(
            y=bpm,
            line_dash="dash",
            annotation_text=f"{z['zone']} {bpm:.0f} bpm",
            annotation_position="right",
        )

    fig.update_layout(
        height=650,
        margin=dict(l=20, r=20, t=50, b=30),
        title=title,
        xaxis_title="Elapsed time (min)",
        yaxis_title="Pulse / heart rate (bpm)",
    )
    return fig


def render_manual_block_editor(
    blocks: pd.DataFrame,
    state_key: str,
    zone_defs: list[dict[str, Any]],
    new_hrmax: int,
    min_step_s: int,
):
    """Manual editing UI for block boundaries and zones."""
    st.subheader("Manual block editor")
    st.caption(
        "Praktisk graf-redigering: velg grensen mellom to blokker og flytt den. "
        "Dette endrer samme blokker som eksporteres til FIT/ZWO."
    )

    current = normalize_manual_blocks(st.session_state[state_key])
    st.session_state[state_key] = current

    if current.empty:
        st.info("No blocks to edit.")
        return current

    c1, c2, c3 = st.columns([2, 1, 1])

    with c1:
        if len(current) > 1:
            boundary_options = list(range(len(current) - 1))
            boundary = st.selectbox(
                "Boundary to move",
                boundary_options,
                format_func=lambda i: f"{i+1}: {current.loc[i,'zone']} → {current.loc[i+1,'zone']} at {current.loc[i,'end']}",
            )
        else:
            boundary = None

    with c2:
        step_s = st.selectbox("Move step", [15, 30, 60, 120], index=1)

    with c3:
        st.write("")
        st.write("")
        if st.button("Reset generated blocks"):
            # The caller should pass original in another state key if required.
            pass

    if len(current) > 1 and boundary is not None:
        bleft, bright = st.columns(2)
        with bleft:
            if st.button(f"← Move boundary {step_s}s", key=f"{state_key}_left"):
                st.session_state[state_key] = move_block_boundary(current, boundary, -int(step_s), int(min_step_s))
                st.rerun()
        with bright:
            if st.button(f"Move boundary {step_s}s →", key=f"{state_key}_right"):
                st.session_state[state_key] = move_block_boundary(current, boundary, int(step_s), int(min_step_s))
                st.rerun()

    st.markdown("**Zone / split / merge selected block**")
    e1, e2, e3, e4 = st.columns([2, 1, 1, 1])
    with e1:
        row_index = st.selectbox(
            "Block",
            list(range(len(current))),
            format_func=lambda i: f"{i+1}: {current.loc[i,'start']}–{current.loc[i,'end']} {current.loc[i,'zone']}",
            key=f"{state_key}_block_select",
        )
    with e2:
        new_zone = st.selectbox(
            "Zone",
            [z["zone"] for z in zone_defs],
            index=[z["zone"] for z in zone_defs].index(str(current.loc[row_index, "zone"])),
            key=f"{state_key}_zone_select",
        )
        if st.button("Apply zone", key=f"{state_key}_apply_zone"):
            st.session_state[state_key] = apply_zone_edit(current, row_index, new_zone, zone_defs, int(new_hrmax))
            st.rerun()
    with e3:
        if st.button("Split", key=f"{state_key}_split"):
            st.session_state[state_key] = split_block(current, row_index, int(min_step_s))
            st.rerun()
    with e4:
        if st.button("Merge next", key=f"{state_key}_merge_next"):
            st.session_state[state_key] = merge_neighbor_blocks(current, row_index, "next")
            st.rerun()

    shown = current.copy()
    # Put most useful columns first; tolerate packages where some columns don't exist.
    preferred_cols = [
        "start", "end", "duration", "zone", "zone_num", "new_bpm_reference",
        "old_avg_hr", "old_avg_pct", "old_max_hr", "old_max_pct",
        "start_s", "end_s", "duration_s",
    ]
    shown = shown[[c for c in preferred_cols if c in shown.columns]]
    st.dataframe(shown, use_container_width=True, hide_index=True)
    return st.session_state[state_key]

# ---- End manual block editing helpers ---------------------------------------

def main():
    st.set_page_config(page_title="HR Replay Builder v8", layout="wide")
    st.title("HR Replay Builder v8 — Fenix 3 custom HR ranges + editable blocks")
    st.caption("Built on v7.3: multi-file plan, editable blocks, ZWO, and Garmin FIT export with Fenix 3 custom HR bpm ranges.")

    uploaded_files = st.file_uploader(
        "Upload GPX or trace JSON files",
        type=["gpx", "json"],
        accept_multiple_files=True,
    )

    with st.sidebar:
        st.header("Plan")
        new_start_date = st.date_input("New start date", value=date.today() + timedelta(days=1))
        st.caption("First historical workout maps to this date. Later workouts keep the same day offsets/pause days.")

        st.header("HRmax mapping")
        old_hrmax = st.number_input("Old HRmax for reference/% conversion", min_value=120, max_value=240, value=212, step=1)
        new_hrmax = st.number_input("Current HRmax for reference only", min_value=120, max_value=240, value=180, step=1)

        st.header("Analysis")
        interval_s = st.selectbox("Resample interval", [5, 10, 15, 30, 60], index=2)
        smooth_window = st.slider("Smoothing window", min_value=1, max_value=9, value=3, step=2)
        method = st.selectbox("Resample HR method", ["Median", "Mean"], index=0)
        min_block_s = st.selectbox("Minimum instruction block", [15, 30, 60, 90, 120, 180, 300], index=0)
        show_blocks = st.checkbox("Show instruction blocks on graph", value=True)

        st.header("Garmin FIT export")
        fit_export_mode = st.selectbox(
            "Garmin FIT export mode",
            [
                "Fenix 3 custom HR range (+100 encoding)",
                "Garmin HR Zone 1-5",
            ],
            index=0,
        )
        st.caption("Fenix 3 custom mode writes explicit bpm ranges using the +100 encoding you verified on the watch.")

        st.header("Zone boundaries in old bpm")
        st.caption("These classify the old workout. FIT uses Garmin zones. ZWO maps zones to %FTP.")
        z2_bpm = st.number_input("Z2 starts at bpm", min_value=60, max_value=230, value=140, step=1)
        z3_bpm = st.number_input("Z3 starts at bpm", min_value=60, max_value=235, value=160, step=1)
        z4_bpm = st.number_input("Z4 starts at bpm", min_value=60, max_value=240, value=175, step=1)
        z5_bpm = st.number_input("Z5 starts at bpm", min_value=60, max_value=245, value=190, step=1)

        st.header("ZWO %FTP proxy per zone")
        st.caption("Rouvy/ZWO is power based. Tune these until indoor HR response resembles the old profile.")
        z1_power = st.number_input("Z1 Power", min_value=0.20, max_value=2.00, value=0.45, step=0.01, format="%.2f")
        z2_power = st.number_input("Z2 Power", min_value=0.20, max_value=2.00, value=0.60, step=0.01, format="%.2f")
        z3_power = st.number_input("Z3 Power", min_value=0.20, max_value=2.00, value=0.75, step=0.01, format="%.2f")
        z4_power = st.number_input("Z4 Power", min_value=0.20, max_value=2.00, value=0.90, step=0.01, format="%.2f")
        z5_power = st.number_input("Z5 Power", min_value=0.20, max_value=2.00, value=1.05, step=0.01, format="%.2f")

    if not uploaded_files:
        st.info("Upload several GPX files to create a date-preserving replay plan.")
        return

    if not (z2_bpm < z3_bpm < z4_bpm < z5_bpm):
        st.error("Zone boundaries must increase: Z2 < Z3 < Z4 < Z5.")
        return

    zwo_power = {1: z1_power, 2: z2_power, 3: z3_power, 4: z4_power, 5: z5_power}
    zone_defs = build_zone_defs(int(z2_bpm), int(z3_bpm), int(z4_bpm), int(z5_bpm), int(old_hrmax), int(new_hrmax), zwo_power)

    parsed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for uploaded in uploaded_files:
        try:
            parsed.append(parse_uploaded_workout(uploaded))
        except Exception as exc:
            errors.append({"file": uploaded.name, "error": str(exc)})

    if errors:
        st.warning("Some files could not be parsed.")
        st.dataframe(pd.DataFrame(errors), use_container_width=True, hide_index=True)

    if not parsed:
        st.error("No usable workout files found.")
        return

    plan = build_date_preserving_plan(parsed, new_start_date)
    plan_df = plan_dataframe(plan)

    st.subheader("Original running days → new planned running days")
    st.caption("The plan preserves the same day offsets and pause days between historical workouts.")
    st.dataframe(plan_df, use_container_width=True, hide_index=True)

    selected_idx = st.selectbox(
        "Preview workout",
        list(range(len(plan))),
        format_func=lambda i: f"{plan[i]['original_date']} → {plan[i]['new_date']} | {plan[i]['name']}",
    )

    selected = plan[selected_idx]
    outputs = build_workout_outputs(
        selected,
        selected_idx + 1,
        zone_defs,
        int(old_hrmax),
        int(new_hrmax),
        int(interval_s),
        int(smooth_window),
        method,
        int(min_block_s),
        fit_export_mode,
    )
    sampled = outputs["sampled"]
    generated_blocks = outputs["blocks"]

    settings_signature = f"{old_hrmax}_{new_hrmax}_{interval_s}_{smooth_window}_{method}_{min_block_s}_" + "_".join(f"{z.get('zone')}:{z.get('low_pct', z.get('low_bpm', ''))}-{z.get('high_pct', z.get('high_bpm', ''))}" for z in zone_defs)
    manual_key = blocks_key_from_source(selected["name"], settings_signature)
    init_manual_blocks_state(manual_key, generated_blocks)

    if st.button("Reset manual edits for selected workout", key=f"{manual_key}_reset_btn"):
        reset_manual_blocks_state(manual_key, generated_blocks)
        st.rerun()

    blocks = normalize_manual_blocks(st.session_state[manual_key])

    left, right = st.columns([2, 1])
    with left:
        st.plotly_chart(plot_hr_profile(sampled, zone_defs, blocks, show_blocks), use_container_width=True)

    with right:
        st.subheader("Selected workout")
        st.metric("Original date", f"{selected['original_date']}")
        st.metric("Planned date", f"{selected['new_date']}")
        st.metric("Pause days before", "" if selected["pause_days"] is None else f"{selected['pause_days']}")
        st.metric("Duration", format_duration(selected["duration_s"]))
        if selected["distance_m"]:
            st.metric("Distance", f"{selected['distance_m']/1000.0:.2f} km")
        if selected["avg_hr"]:
            st.metric("Avg HR", f"{selected['avg_hr']:.0f} bpm")
        if selected["max_hr"]:
            st.metric("Max HR", f"{selected['max_hr']:.0f} bpm")

        st.subheader("Single-workout export")
        st.download_button(
            "Download preview .FIT",
            outputs["fit_bytes"],
            file_name=outputs["fit_filename"],
            mime="application/octet-stream",
        )
        st.download_button(
            "Download preview .ZWO",
            outputs["zwo_text"],
            file_name=outputs["zwo_filename"],
            mime="application/xml",
        )
        st.download_button(
            "Download preview YAML audit",
            outputs["yaml_text"],
            file_name=outputs["yaml_filename"],
            mime="text/yaml",
        )

    blocks = render_manual_block_editor(blocks, manual_key, zone_defs, int(new_hrmax), int(min_block_s))

    st.subheader("Generated / edited steps for preview")
    st.caption("FIT uses selected Garmin mode. In Fenix 3 custom mode, blocks export as explicit bpm ranges. ZWO = %FTP power proxy.")
    st.dataframe(blocks, use_container_width=True, hide_index=True)

    with st.expander("Zone definitions"):
        st.dataframe(pd.DataFrame(zone_defs), use_container_width=True, hide_index=True)

    with st.expander("Resampled data"):
        st.dataframe(sampled, use_container_width=True, hide_index=True)

    
    st.subheader("Export selected edited workout")
    edited_workout_name = f"{selected['new_date'].strftime('%Y%m%d')}_{safe_name(selected['name'].rsplit('.',1)[0])}_edited"[:31]
    edited_fit_bytes = create_fit_for_blocks(edited_workout_name, blocks, fit_export_mode)
    st.download_button(
        "Download selected edited Garmin FIT",
        edited_fit_bytes,
        file_name=f"{selected['new_date'].isoformat()}_{safe_name(selected['name'].rsplit('.',1)[0])}_edited.fit",
        mime="application/octet-stream",
    )

    edited_zwo_text = blocks_to_zwo(
        blocks,
        edited_workout_name,
        "Manually edited HR replay. ZWO is %FTP proxy, not true HR target.",
    )
    st.download_button(
        "Download selected edited ZWO",
        edited_zwo_text,
        file_name=f"{selected['new_date'].isoformat()}_{safe_name(selected['name'].rsplit('.',1)[0])}_edited.zwo",
        mime="application/xml",
    )

    edited_yaml_text = blocks_to_yaml(
        blocks,
        edited_workout_name,
        int(old_hrmax),
        int(new_hrmax),
        zone_defs,
    )
    st.download_button(
        "Download selected edited YAML audit",
        edited_yaml_text,
        file_name=f"{selected['new_date'].isoformat()}_{safe_name(selected['name'].rsplit('.',1)[0])}_edited.yaml",
        mime="text/yaml",
    )

    st.subheader("Export full plan")
    plan_zip = build_plan_zip(
        plan,
        plan_df,
        zone_defs,
        int(old_hrmax),
        int(new_hrmax),
        int(interval_s),
        int(smooth_window),
        method,
        int(min_block_s),
        fit_export_mode,
    )
    st.download_button(
        "Download ZIP: full plan (.FIT + .ZWO + audit)",
        plan_zip,
        file_name=f"hr_replay_v6_plan_{new_start_date.isoformat()}.zip",
        mime="application/zip",
    )

    st.info(
        "Import Garmin workouts by copying files from the ZIP's fit/ folder to Garmin/NewFiles on the watch. "
        "Use plan.csv to see which workout belongs to which planned day."
    )


if __name__ == "__main__":
    main()
