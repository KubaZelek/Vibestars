#!/usr/bin/env python3
"""
Stage 1 profiler for the Quesma SWE-chat enhanced bundle.

Usage:
    python hackathon_stage1.py /path/to/swe-chat-enhanced-2026-07-05

Expected:
    <bundle>/metadata/sessions.json

Outputs:
    ./analysis_output/session_metrics.csv
    ./analysis_output/top_candidates.csv
    ./analysis_output/summary.txt

This stage intentionally DOES NOT call an LLM.
It finds statistically unusual sessions cheaply from metadata first.
"""

import argparse
import json
import math
import re
from pathlib import Path

import numpy as np
import pandas as pd


METRIC_PATTERNS = {
    "tokens": re.compile(r"token", re.I),
    "tool_calls": re.compile(r"tool.*call|call.*tool", re.I),
    "messages": re.compile(r"message|turn", re.I),
    "bytes": re.compile(r"bytes|size", re.I),
    "duration": re.compile(r"duration|elapsed|time_ms|duration_ms", re.I),
}

ID_PATTERNS = {
    "session_id": re.compile(r"(^|\.)(session_id|trajectory_id|id)$", re.I),
    "repository": re.compile(r"repository|repo", re.I),
    "harness": re.compile(r"harness|agent|client", re.I),
    "model": re.compile(r"model", re.I),
    "trajectory": re.compile(r"trajectory|transcript.*path", re.I),
}


def load_sessions(path: Path) -> pd.DataFrame:
    with path.open("r", encoding="utf-8") as f:
        obj = json.load(f)

    if isinstance(obj, dict):
        for key in ("sessions", "trajectories", "data"):
            if isinstance(obj.get(key), list):
                rows = obj[key]
                break
        else:
            list_values = [v for v in obj.values() if isinstance(v, list)]
            if len(list_values) == 1:
                rows = list_values[0]
            else:
                raise ValueError(
                    "Could not find a list of sessions. "
                    f"Top-level keys: {list(obj.keys())[:20]}"
                )
    elif isinstance(obj, list):
        rows = obj
    else:
        raise ValueError("Unsupported sessions.json structure")

    return pd.json_normalize(rows, sep=".")


def pick_first_column(columns, pattern):
    matches = [c for c in columns if pattern.search(c)]
    return matches[0] if matches else None


def numeric_metric_columns(df: pd.DataFrame):
    found = {}
    for family, pattern in METRIC_PATTERNS.items():
        cols = []
        for c in df.columns:
            if not pattern.search(c):
                continue
            converted = pd.to_numeric(df[c], errors="coerce")
            if converted.notna().sum() >= max(5, int(len(df) * 0.05)):
                df[c] = converted
                cols.append(c)
        found[family] = cols
    return found


def robust_z(series: pd.Series) -> pd.Series:
    """Robust z-score based on median absolute deviation."""
    x = pd.to_numeric(series, errors="coerce")
    median = x.median()
    mad = (x - median).abs().median()

    if pd.isna(mad) or mad == 0:
        std = x.std()
        if pd.isna(std) or std == 0:
            return pd.Series(np.zeros(len(x)), index=x.index)
        return (x - x.mean()) / std

    return 0.6745 * (x - median) / mad


def log_robust_z(series: pd.Series) -> pd.Series:
    x = pd.to_numeric(series, errors="coerce").clip(lower=0)
    return robust_z(np.log1p(x))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path, help="Root directory of the extracted bundle")
    parser.add_argument("--top", type=int, default=150, help="Number of candidates to export")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("analysis_output"),
        help="Output directory",
    )
    args = parser.parse_args()

    sessions_path = args.bundle / "metadata" / "sessions.json"
    if not sessions_path.exists():
        raise SystemExit(f"Missing file: {sessions_path}")

    args.output.mkdir(parents=True, exist_ok=True)
    df = load_sessions(sessions_path)

    ids = {
        name: pick_first_column(df.columns, pattern)
        for name, pattern in ID_PATTERNS.items()
    }
    metrics = numeric_metric_columns(df)

    score_cols = []
    score_parts = []

    # Take all useful numeric metadata fields, but cap the influence of any one family.
    for family in ("tokens", "tool_calls", "messages", "bytes", "duration"):
        family_cols = metrics.get(family, [])
        if not family_cols:
            continue

        family_zs = []
        for c in family_cols:
            zc = f"z::{c}"
            df[zc] = log_robust_z(df[c]).clip(lower=-5, upper=10)
            family_zs.append(zc)
            score_cols.append(zc)

        family_score = df[family_zs].max(axis=1, skipna=True)
        df[f"score::{family}"] = family_score
        score_parts.append(f"score::{family}")

    if not score_parts:
        raise SystemExit(
            "I could not detect numeric metadata fields for tokens/tool calls/messages/bytes/duration.\n"
            "Run the script and inspect summary.txt / printed columns, then adapt METRIC_PATTERNS."
        )

    # Global anomaly score
    df["anomaly_score_global"] = df[score_parts].mean(axis=1, skipna=True)

    # More defensible score: compare within repository where possible.
    repo_col = ids["repository"]
    if repo_col:
        within_parts = []
        for family_col in score_parts:
            out = f"within_repo::{family_col}"
            df[out] = (
                df.groupby(repo_col, dropna=False)[family_col]
                .transform(lambda s: robust_z(s).clip(lower=-5, upper=10))
            )
            within_parts.append(out)
        df["anomaly_score_within_repo"] = df[within_parts].mean(axis=1, skipna=True)
    else:
        df["anomaly_score_within_repo"] = df["anomaly_score_global"]

    # Prefer sessions that are anomalous both absolutely and relative to their repo.
    df["candidate_score"] = (
        0.45 * df["anomaly_score_global"].fillna(0)
        + 0.55 * df["anomaly_score_within_repo"].fillna(0)
    )

    # Useful derived ratios if matching columns exist.
    token_cols = metrics.get("tokens", [])
    tool_cols = metrics.get("tool_calls", [])
    msg_cols = metrics.get("messages", [])

    if token_cols and tool_cols:
        t = df[token_cols].sum(axis=1, min_count=1)
        tools = df[tool_cols].sum(axis=1, min_count=1)
        df["derived::tokens_per_tool_call"] = t / tools.replace(0, np.nan)

    if token_cols and msg_cols:
        t = df[token_cols].sum(axis=1, min_count=1)
        msgs = df[msg_cols].sum(axis=1, min_count=1)
        df["derived::tokens_per_message"] = t / msgs.replace(0, np.nan)

    # Export
    df.to_csv(args.output / "session_metrics.csv", index=False)

    preferred_cols = [
        ids["session_id"],
        ids["repository"],
        ids["harness"],
        ids["model"],
        ids["trajectory"],
        "candidate_score",
        "anomaly_score_global",
        "anomaly_score_within_repo",
    ]
    preferred_cols = [c for c in preferred_cols if c and c in df.columns]

    useful_raw = []
    for family in ("tokens", "tool_calls", "messages", "bytes", "duration"):
        useful_raw.extend(metrics.get(family, []))
    useful_raw = [c for c in useful_raw if c not in preferred_cols]

    derived = [c for c in df.columns if c.startswith("derived::")]
    candidate_cols = preferred_cols + useful_raw + derived

    top = (
        df.sort_values("candidate_score", ascending=False)
        .head(args.top)[candidate_cols]
    )
    top.to_csv(args.output / "top_candidates.csv", index=False)

    summary = []
    summary.append(f"Sessions: {len(df):,}")
    summary.append(f"Columns: {len(df.columns):,}")
    summary.append("")
    summary.append("Detected identity columns:")
    for k, v in ids.items():
        summary.append(f"  {k}: {v}")
    summary.append("")
    summary.append("Detected metric columns:")
    for family, cols in metrics.items():
        summary.append(f"  {family}: {cols}")
    summary.append("")
    summary.append("Top repositories by session count:")
    if repo_col:
        for repo, n in df[repo_col].value_counts(dropna=False).head(20).items():
            summary.append(f"  {n:>6}  {repo}")
    else:
        summary.append("  repository column not detected")

    text = "\n".join(summary)
    (args.output / "summary.txt").write_text(text, encoding="utf-8")

    print(text)
    print()
    print(f"Wrote: {args.output / 'session_metrics.csv'}")
    print(f"Wrote: {args.output / 'top_candidates.csv'}")
    print(f"Wrote: {args.output / 'summary.txt'}")


if __name__ == "__main__":
    main()
