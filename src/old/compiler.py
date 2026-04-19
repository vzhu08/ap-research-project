# src/compiler.py

import os
import pandas as pd
import json
import sqlite3
from io import StringIO
from datetime import datetime
from typing import List, Set


def filter_events_by_exact_and_fuzzy(df: pd.DataFrame, exact_set: set, fuzzy_set: set) -> pd.DataFrame:
    if "Ev" not in df.columns:
        return pd.DataFrame()  # no event column = no match

    df = df.dropna(subset=["Ev"]).copy()  # <-- copy ensures it's safe to modify
    df["Ev_norm"] = df["Ev"].str.lower().str.strip()

    exact_mask = df["Ev_norm"].isin(exact_set)
    fuzzy_mask = df["Ev_norm"].apply(lambda ev: any(fuzzy in ev for fuzzy in fuzzy_set))

    return df[exact_mask | fuzzy_mask].copy()


def default_database_path(output_root="output") -> str:
    return os.path.join(output_root, "judge_data.sqlite3")


def build_record_label(judge_person_id: int, judge_name: str, safe_name: str) -> str:
    stem = safe_name or "".join(ch for ch in judge_name if ch.isalnum()) or f"ID_{judge_person_id}"
    return f"{judge_person_id}_{stem}"


def iter_record_data(input_folder="output/records", database_path=None):
    database_path = database_path or default_database_path(os.path.dirname(input_folder) or "output")

    if os.path.exists(database_path):
        conn = sqlite3.connect(database_path)
        try:
            page_df = pd.read_sql_query(
                """
                SELECT judge_person_id, judge_name, safe_name, record_csv
                FROM judge_pages
                WHERE COALESCE(record_csv, '') <> ''
                ORDER BY judge_person_id
                """,
                conn,
            )
        finally:
            conn.close()

        if not page_df.empty:
            for row in page_df.itertuples(index=False):
                try:
                    df = pd.read_csv(StringIO(row.record_csv), dtype=str)
                except Exception as exc:
                    print(f"[Warning] Skipping DB record for judge {row.judge_person_id}: {exc}")
                    continue

                yield build_record_label(row.judge_person_id, row.judge_name or "", row.safe_name or ""), df
            return

    if not os.path.isdir(input_folder):
        return

    for filename in os.listdir(input_folder):
        if not filename.lower().endswith(".csv"):
            continue

        rec = os.path.join(input_folder, filename)
        try:
            df = pd.read_csv(rec, dtype=str)
        except Exception as exc:
            print(f"[Warning] Skipping {rec}: {exc}")
            continue

        yield os.path.splitext(os.path.basename(rec))[0], df


def compile_judge_data(
    input_folder="output/records",
    output_file="output/compiled_data.csv",
    database_path=None,
    events=None,  # expects a dict: {'exact': set(), 'fuzzy': set()}
    cumulative_only=False
):
    """
    Loops through all judge CSVs in input_folder.
    Filters by HS level and optional event set (exact/fuzzy).
    Computes sit rate, aff/neg split, etc.
    """
    cum_rounds = cum_elims = cum_aff = cum_neg = cum_sit = cum_results_with = 0
    compiled_rows = []

    for judge_name, df in iter_record_data(input_folder=input_folder, database_path=database_path):

        # Event filtering
        if events:
            df = filter_events_by_exact_and_fuzzy(df, events['exact'], events['fuzzy'])
            if df.empty:
                continue

        # Filter to HS level
        if "Lv" in df.columns:
            df = df[df["Lv"] == "HS"]

        if df.empty:
            continue

        # Clean Vote column
        votes = df['Vote'].fillna('').str.lower().str.strip()
        aff_mask = votes.str.contains('aff') | votes.str.contains('pro') | votes.str.contains('gov') | (votes == 'a')
        neg_mask = votes.str.contains('neg') | votes.str.contains('con') | votes.str.contains('opp') | (votes == 'n')
        valid = aff_mask | neg_mask
        df = df[valid].reset_index(drop=True)
        votes = votes[valid].reset_index(drop=True)
        aff_mask = aff_mask[valid]
        neg_mask = neg_mask[valid]

        # per-judge metrics
        total_rounds = len(df)
        total_elims = df['Result'].dropna().str.strip().ne('').sum()
        total_tourns = df[['Tournament', 'Date']].drop_duplicates().shape[0] \
            if 'Tournament' in df.columns and 'Date' in df.columns else 0
        aff_count = aff_mask.sum()
        neg_count = neg_mask.sum()

        # sit rate
        results = df['Result'].fillna('').str.lower().str.strip()
        has_res = results != ''
        denom = has_res.sum()
        sit_count = sum(1 for v, r in zip(votes[has_res], results[has_res]) if v not in r) if denom else 0

        # accumulate
        cum_rounds += total_rounds
        cum_elims += total_elims
        cum_aff += aff_count
        cum_neg += neg_count
        cum_sit += sit_count
        cum_results_with += denom

        # Per-judge row (name from file)
        aff_pct = aff_count / total_rounds * 100 if total_rounds else 0.0
        neg_pct = neg_count / total_rounds * 100 if total_rounds else 0.0
        split_str = f"{aff_pct:.1f}/{neg_pct:.1f}"
        sit_rate_str = f"{sit_count / denom * 100:.1f}%" if denom else "0.0%"

        compiled_rows.append({
            'File': judge_name,
            'Total Tournaments': total_tourns,
            'Total Rounds (HS only)': total_rounds,
            'Total Elim Rounds': int(total_elims),
            'Aff/Neg Split (%)': split_str,
            'Sit Rate (%)': sit_rate_str
        })

    # Write compiled file
    if not cumulative_only:
        out_df = pd.DataFrame(compiled_rows)
        out_df.to_csv(output_file, index=False)
        print(f"Written {len(compiled_rows)} rows to {output_file}")

    # Cumulative print
    n = len(compiled_rows)
    if n:
        overall_aff_pct = cum_aff / cum_rounds * 100 if cum_rounds else 0.0
        overall_neg_pct = cum_neg / cum_rounds * 100 if cum_rounds else 0.0
        overall_split = f"{overall_aff_pct:.1f}/{overall_neg_pct:.1f}"
        overall_sit_pct = cum_sit / cum_results_with * 100 if cum_results_with else 0.0

        print(f"Processed {n} judges")
        print(f" Cumulative rounds judged : {cum_rounds}")
        print(f" Cumulative elim rounds    : {cum_elims}")
        print(f" Cumulative Aff/Neg split  : {overall_split}")
        print(f" Cumulative sit rate       : {overall_sit_pct:.1f}%")
    else:
        print("No judges processed.")


def assign_academic_year(date: pd.Timestamp) -> str:
    if pd.isna(date):
        return None
    year = date.year if date.month >= 8 else date.year - 1
    return f"{year}-{year + 1}"

def make_json_serializable(obj):
    """Recursively convert Timestamps in nested dict/list structures to strings."""
    if isinstance(obj, dict):
        return {k: make_json_serializable(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [make_json_serializable(v) for v in obj]
    elif isinstance(obj, pd.Timestamp):
        return obj.strftime('%Y-%m-%d')
    else:
        return obj

def calculate_sit_rate_by_year_range(
    input_folder="output/records",
    output_csv="output/sit_rate_by_year.csv",
    output_json="output/sit_rate_by_year.json",
    database_path=None,
    events=None,
    start_year=2012,
    end_year=2024
):
    year_data = {}
    all_rounds = []

    # Step 1: Read, filter, and tag all rounds
    for judge_id, df in iter_record_data(input_folder=input_folder, database_path=database_path):

        if 'Date' not in df.columns or 'Vote' not in df.columns:
            continue

        if "Lv" in df.columns:
            df = df[df["Lv"] == "HS"]
        if df.empty:
            continue

        if events:
            df = filter_events_by_exact_and_fuzzy(df, events['exact'], events['fuzzy'])
        if df.empty:
            continue

        df["ParsedDate"] = pd.to_datetime(
            df["Date"].astype(str).str.split().str[1],
            format="%Y-%m-%d",
            errors="coerce"
        )
        df = df.dropna(subset=["ParsedDate"])
        if df.empty:
            continue

        df["AcademicYear"] = df["ParsedDate"].apply(assign_academic_year)
        df["JudgeID"] = judge_id

        all_rounds.append(df)

    if not all_rounds:
        print("❌ No usable data found.")
        return

    full_df = pd.concat(all_rounds, ignore_index=True)

    # Step 2: Aggregate per year
    csv_summary = []

    for year in range(start_year, end_year + 1):
        label = f"{year}-{year + 1}"
        df_year = full_df[full_df["AcademicYear"] == label]

        if df_year.empty:
            csv_summary.append({
                "Year": label,
                "Sit Rate (%)": 0.0,
                "Total Rounds": 0,
                "Rounds With Result": 0
            })
            year_data[label] = []
            continue

        votes = df_year["Vote"].fillna('').str.lower().str.strip()
        results = df_year["Result"].fillna('').str.lower().str.strip()
        has_result = results != ''
        votes = votes[has_result]
        results = results[has_result]

        sit_count = sum(1 for v, r in zip(votes, results) if v not in r)
        total_with_result = len(results)
        sit_rate = (sit_count / total_with_result * 100) if total_with_result else 0.0

        csv_summary.append({
            "Year": label,
            "Sit Rate (%)": round(sit_rate, 1),
            "Total Rounds": len(df_year),
            "Rounds With Result": total_with_result
        })

        # Save full data rows as dicts (for JSON)
        year_data[label] = make_json_serializable(df_year.fillna('').to_dict(orient="records"))

    # Step 3: Write outputs
    os.makedirs(os.path.dirname(output_csv), exist_ok=True)

    pd.DataFrame(csv_summary).to_csv(output_csv, index=False)
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(year_data, f, indent=2, ensure_ascii=False)

    print(f"✅ Sit rate summary saved to: {output_csv}")
    print(f"✅ Full per-round data saved to: {output_json}")
