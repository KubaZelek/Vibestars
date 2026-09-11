#!/usr/bin/env python3
"""
Quesma hackathon - Stage 2 sampler

Goal:
1. Build a defensible shortlist for "failure to make progress".
2. Avoid domination by one repository.
3. Add matched-ish control sessions with similar activity but more visible progress.
4. Copy only the selected raw transcripts into a tiny review bundle.

Usage:
  python stage2_sample.py \
      --metrics /path/to/session_metrics.csv \
      --bundle /path/to/swe-chat-enhanced-2026-07-05 \
      --output stage2_sample

Outputs:
  stage2_sample/risk_sessions.csv
  stage2_sample/control_sessions.csv
  stage2_sample/review_manifest.csv
  stage2_sample/transcripts/*.jsonl
  stage2_sample/review_bundle.tar.gz

Notes:
- agent_lines is only a PROXY for progress. Read-only/research tasks may legitimately
  have zero code output, so final classification must inspect the transcript.
- We deliberately do NOT combine statistics.* tokens with checkpoint_token_usage.*
  tokens because these may be overlapping accounting systems.
"""

import argparse
import ast
import math
import shutil
import tarfile
from pathlib import Path

import numpy as np
import pandas as pd


def robust_z(s: pd.Series) -> pd.Series:
    x = pd.to_numeric(s, errors="coerce")
    med = x.median()
    mad = (x - med).abs().median()
    if pd.isna(mad) or mad == 0:
        sd = x.std()
        if pd.isna(sd) or sd == 0:
            return pd.Series(0.0, index=s.index)
        return (x - x.mean()) / sd
    return 0.6745 * (x - med) / mad


def parse_files_touched(v):
    if pd.isna(v):
        return np.nan
    if isinstance(v, (list, tuple, set)):
        return len(v)
    try:
        parsed = ast.literal_eval(str(v))
        return len(parsed) if isinstance(parsed, (list, tuple, set)) else np.nan
    except Exception:
        return np.nan


def diversify(df, n, max_per_repo=2, max_per_agent=12):
    chosen = []
    repo_counts = {}
    agent_counts = {}
    for idx, row in df.iterrows():
        repo = str(row.get("source.repository", ""))
        agent = str(row.get("agent", ""))
        if repo_counts.get(repo, 0) >= max_per_repo:
            continue
        if agent_counts.get(agent, 0) >= max_per_agent:
            continue
        chosen.append(idx)
        repo_counts[repo] = repo_counts.get(repo, 0) + 1
        agent_counts[agent] = agent_counts.get(agent, 0) + 1
        if len(chosen) >= n:
            break
    return df.loc[chosen].copy()


def pick_controls(df, risks, n):
    """
    Prefer same repo + same agent and comparable tool-call scale,
    but with much higher agent-lines-per-tool.
    """
    pool = df.copy()
    pool = pool[
        pool["initial_attribution.agent_lines"].notna()
        & (pool["statistics.tool_calls"] >= 50)
    ].copy()

    pool["lines_per_100_tools"] = (
        100 * pool["initial_attribution.agent_lines"]
        / pool["statistics.tool_calls"].replace(0, np.nan)
    )

    used = set()
    controls = []

    for _, risk in risks.iterrows():
        if len(controls) >= n:
            break

        repo = risk["source.repository"]
        agent = risk.get("agent")
        tools = max(float(risk["statistics.tool_calls"]), 1.0)

        cand = pool[
            (pool["source.repository"] == repo)
            & (pool["agent"].fillna("") == ("" if pd.isna(agent) else str(agent)))
            & (~pool["trajectory"].isin(used))
        ].copy()

        # Avoid selecting the risk trajectory/session itself.
        cand = cand[
            (cand["trajectory"] != risk["trajectory"])
            & (cand["session_id"].astype(str) != str(risk["session_id"]))
        ]

        # Similar scale: between 1/3x and 3x tool calls.
        cand = cand[
            cand["statistics.tool_calls"].between(tools / 3, tools * 3)
        ]

        if cand.empty:
            continue

        cand["tool_distance"] = (
            np.log1p(cand["statistics.tool_calls"]) - math.log1p(tools)
        ).abs()

        # Reward visible progress, mildly penalize activity mismatch.
        cand["control_score"] = (
            np.log1p(cand["lines_per_100_tools"].clip(lower=0))
            - 0.75 * cand["tool_distance"]
        )

        pick = cand.sort_values("control_score", ascending=False).iloc[0]
        controls.append(pick)
        used.add(pick["trajectory"])

    if not controls:
        return pd.DataFrame(columns=df.columns)

    out = pd.DataFrame(controls).drop_duplicates("trajectory")
    return out.head(n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--metrics", type=Path, required=True)
    ap.add_argument("--bundle", type=Path, required=True)
    ap.add_argument("--output", type=Path, default=Path("stage2_sample"))
    ap.add_argument("--risk-n", type=int, default=30)
    ap.add_argument("--control-n", type=int, default=15)
    ap.add_argument("--min-tools", type=int, default=100)
    args = ap.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    transcript_out = args.output / "transcripts"
    transcript_out.mkdir(exist_ok=True)

    df = pd.read_csv(args.metrics, low_memory=False)

    required = [
        "trajectory", "session_id", "source.repository", "agent",
        "statistics.tool_calls", "statistics.assistant_messages",
        "initial_attribution.agent_lines", "files_touched",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise SystemExit(f"Missing columns: {missing}")

    df["files_touched_count"] = df["files_touched"].apply(parse_files_touched)

    # Duplicate session IDs exist in the enhanced bundle. They can represent
    # multiple captured trajectories/checkpoints. For a review sample, keep
    # the most active trajectory for each session ID to reduce near-duplicates.
    df = (
        df.sort_values("statistics.tool_calls", ascending=False)
        .drop_duplicates("session_id", keep="first")
        .copy()
    )

    # Activity signal
    df["log_tools"] = np.log1p(df["statistics.tool_calls"].clip(lower=0))
    df["log_assistant"] = np.log1p(df["statistics.assistant_messages"].clip(lower=0))

    # Observable-result proxies. These are NOT treated as ground truth.
    df["log_agent_lines"] = np.log1p(
        df["initial_attribution.agent_lines"].fillna(0).clip(lower=0)
    )
    df["log_files"] = np.log1p(
        df["files_touched_count"].fillna(0).clip(lower=0)
    )

    # Normalize within repository so giant/complex repos do not dominate.
    for c in ("log_tools", "log_assistant", "log_agent_lines", "log_files"):
        df[f"zrepo::{c}"] = (
            df.groupby("source.repository", dropna=False)[c]
            .transform(robust_z)
            .clip(-8, 8)
        )

    df["activity_score"] = (
        df["zrepo::log_tools"] + df["zrepo::log_assistant"]
    ) / 2

    df["visible_progress_proxy"] = (
        df["zrepo::log_agent_lines"] + 0.40 * df["zrepo::log_files"]
    ) / 1.40

    # High activity + low visible output = review priority.
    df["lack_progress_risk"] = (
        df["activity_score"] - df["visible_progress_proxy"]
    )

    df["lines_per_100_tools"] = (
        100 * df["initial_attribution.agent_lines"]
        / df["statistics.tool_calls"].replace(0, np.nan)
    )

    eligible = df[
        (df["statistics.tool_calls"] >= args.min_tools)
        & df["initial_attribution.agent_lines"].notna()
    ].copy()

    # Add a small absolute-risk bonus. A session with thousands of calls
    # and almost no visible output is interesting even if its repo is weird.
    eligible["absolute_stagnation_bonus"] = (
        np.log1p(eligible["statistics.tool_calls"])
        - np.log1p(eligible["initial_attribution.agent_lines"].clip(lower=0) + 1)
    )

    eligible["review_score"] = (
        eligible["lack_progress_risk"]
        + 0.35 * eligible["absolute_stagnation_bonus"]
    )

    ranked = eligible.sort_values("review_score", ascending=False)
    risks = diversify(
        ranked,
        args.risk_n,
        max_per_repo=2,
        max_per_agent=max(8, args.risk_n // 2),
    )

    controls = pick_controls(df, risks, args.control_n)

    risks["sample_role"] = "risk"
    controls["sample_role"] = "control"

    keep_cols = [
        "sample_role", "trajectory", "session_id", "source.repository",
        "agent", "model", "strategy",
        "statistics.tool_calls", "statistics.user_messages",
        "statistics.assistant_messages",
        "initial_attribution.agent_lines",
        "initial_attribution.total_committed",
        "files_touched_count",
        "lines_per_100_tools",
        "activity_score", "visible_progress_proxy",
        "lack_progress_risk", "review_score",
        "bytes",
    ]
    keep_cols = [c for c in keep_cols if c in df.columns or c == "sample_role"]

    risks[keep_cols].to_csv(args.output / "risk_sessions.csv", index=False)
    controls[keep_cols].to_csv(args.output / "control_sessions.csv", index=False)

    manifest = pd.concat([risks, controls], ignore_index=True)
    manifest[keep_cols].to_csv(args.output / "review_manifest.csv", index=False)

    copied = []
    missing_files = []

    for _, row in manifest.iterrows():
        rel = Path(str(row["trajectory"]))
        src = args.bundle / rel
        if not src.exists():
            missing_files.append(str(src))
            continue

        role = row["sample_role"]
        safe_repo = str(row["source.repository"]).replace("/", "__")
        dst = transcript_out / f"{role}__{safe_repo}__{src.name}"
        shutil.copy2(src, dst)
        copied.append(dst)

    archive = args.output / "review_bundle.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(args.output / "review_manifest.csv", arcname="review_manifest.csv")
        for f in copied:
            tar.add(f, arcname=f"transcripts/{f.name}")

    print(f"Risk sessions selected: {len(risks)}")
    print(f"Controls selected:      {len(controls)}")
    print(f"Transcripts copied:     {len(copied)}")
    if missing_files:
        print(f"Missing transcript files: {len(missing_files)}")
        for p in missing_files[:10]:
            print("  ", p)

    print()
    print("Top risk sessions:")
    display_cols = [
        "session_id", "source.repository", "agent",
        "statistics.tool_calls", "initial_attribution.agent_lines",
        "files_touched_count", "review_score",
    ]
    print(risks[display_cols].head(15).to_string(index=False))

    print()
    print(f"Review bundle: {archive}")
    print(
        "\nIMPORTANT: a high score means 'interesting to inspect', "
        "not 'proven failure'. Transcript review is the next stage."
    )


if __name__ == "__main__":
    main()
