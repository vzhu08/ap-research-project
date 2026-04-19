import csv
import os
import sqlite3
from collections import defaultdict
from contextlib import closing
from urllib.parse import parse_qs, urlparse


def default_tournament_stats_output_dir(output_dir: str) -> str:
    return os.path.join(output_dir, "results")


def parse_tourn_id(url: str) -> int | None:
    try:
        query = parse_qs(urlparse(url or "").query)
        raw = query.get("tourn_id", [None])[0]
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def normalize_text(value: str) -> str:
    return " ".join((value or "").split()).strip()


def is_hs_level(value: str) -> bool:
    return normalize_text(value).upper() == "HS"


def write_csv(path: str, fieldnames: list[str], rows: list[dict]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def summarize_tournaments_from_judge_db(
    judge_db_path: str,
    output_dir: str,
    results_dir: str | None = None,
) -> dict:
    if not os.path.exists(judge_db_path):
        raise RuntimeError(f"Judge database not found: {judge_db_path}")

    results_dir = results_dir or default_tournament_stats_output_dir(output_dir)
    os.makedirs(results_dir, exist_ok=True)

    with closing(sqlite3.connect(judge_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        table_row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='judge_rounds' LIMIT 1"
        ).fetchone()
        if table_row is None:
            raise RuntimeError("Judge database is missing table 'judge_rounds'.")

        rows = conn.execute(
            """
            SELECT tournament_url, tournament, event, level
            FROM judge_rounds
            """
        ).fetchall()

    tournament_name_by_id: dict[int, str] = {}
    all_tournament_ids: set[int] = set()
    hs_tournament_ids: set[int] = set()
    hs_event_pairs: set[tuple[int, str]] = set()
    hs_rounds_by_tournament: dict[int, int] = defaultdict(int)
    hs_rounds_by_event: dict[str, int] = defaultdict(int)
    hs_rounds_by_tournament_event: dict[tuple[int, str], int] = defaultdict(int)
    hs_tournaments_by_event: dict[str, set[int]] = defaultdict(set)

    for row in rows:
        tourn_id = parse_tourn_id(row["tournament_url"] or "")
        if tourn_id is None:
            continue

        tournament_name = normalize_text(row["tournament"] or "")
        event_name = normalize_text(row["event"] or "")
        level = row["level"] or ""

        all_tournament_ids.add(tourn_id)
        if tournament_name and tourn_id not in tournament_name_by_id:
            tournament_name_by_id[tourn_id] = tournament_name

        if not is_hs_level(level):
            continue

        hs_tournament_ids.add(tourn_id)
        hs_rounds_by_tournament[tourn_id] += 1
        if event_name:
            hs_event_pairs.add((tourn_id, event_name))
            hs_rounds_by_event[event_name] += 1
            hs_rounds_by_tournament_event[(tourn_id, event_name)] += 1
            hs_tournaments_by_event[event_name].add(tourn_id)

    tournaments_ranked_rows = []
    ranked_tournaments = sorted(
        hs_rounds_by_tournament.items(),
        key=lambda item: (-item[1], tournament_name_by_id.get(item[0], ""), item[0]),
    )
    for idx, (tourn_id, round_count) in enumerate(ranked_tournaments, start=1):
        tournaments_ranked_rows.append(
            {
                "rank": idx,
                "tourn_id": tourn_id,
                "tournament_name": tournament_name_by_id.get(tourn_id, ""),
                "hs_round_count": round_count,
            }
        )

    min_hs_rounds_for_csv = 100
    qualifying_tournament_ids = {
        tourn_id for tourn_id, round_count in hs_rounds_by_tournament.items() if round_count >= min_hs_rounds_for_csv
    }
    qualifying_tournaments_ranked_rows = [
        row for row in tournaments_ranked_rows if int(row["tourn_id"]) in qualifying_tournament_ids
    ]

    events_coverage_rows = []
    qualifying_hs_tournaments_by_event: dict[str, set[int]] = defaultdict(set)
    qualifying_hs_rounds_by_event: dict[str, int] = defaultdict(int)
    excluded_event_tokens = ("LD", "CX", "PAR")
    for event_name, tournament_ids in hs_tournaments_by_event.items():
        upper_event_name = event_name.upper()
        if any(token in upper_event_name for token in excluded_event_tokens):
            continue
        qualified_ids = tournament_ids.intersection(qualifying_tournament_ids)
        if not qualified_ids:
            continue
        qualifying_hs_tournaments_by_event[event_name] = qualified_ids
        for tourn_id in qualified_ids:
            qualifying_hs_rounds_by_event[event_name] += hs_rounds_by_tournament_event.get((tourn_id, event_name), 0)

    ranked_events = sorted(
        qualifying_hs_tournaments_by_event.items(),
        key=lambda item: (-qualifying_hs_rounds_by_event.get(item[0], 0), -len(item[1]), item[0]),
    )
    for event_name, tournament_ids in ranked_events:
        events_coverage_rows.append(
            {
                "event_name": event_name,
                "tournament_count": len(tournament_ids),
                "hs_round_count": qualifying_hs_rounds_by_event.get(event_name, 0),
            }
        )

    tournament_rounds_csv = os.path.join(results_dir, "tournaments_by_hs_round_count.csv")
    event_coverage_csv = os.path.join(results_dir, "events_by_tournament_count_hs.csv")

    write_csv(
        tournament_rounds_csv,
        ["rank", "tourn_id", "tournament_name", "hs_round_count"],
        qualifying_tournaments_ranked_rows,
    )
    write_csv(
        event_coverage_csv,
        ["event_name", "tournament_count", "hs_round_count"],
        events_coverage_rows,
    )

    top_100_events = events_coverage_rows[:100]
    top_100_tournaments = qualifying_tournaments_ranked_rows[:100]
    hs_round_total = sum(hs_rounds_by_tournament.values())

    print("")
    print("Tournament Summary (from judge DB)")
    print(f"- tournaments (all levels): {len(all_tournament_ids)}")
    print(f"- tournaments (HS rounds): {len(hs_tournament_ids)}")
    print(f"- tournament-event pairs (HS): {len(hs_event_pairs)}")
    print(f"- total HS rounds: {hs_round_total}")
    print(f"- tournaments in CSV (HS rounds >= {min_hs_rounds_for_csv}): {len(qualifying_tournament_ids)}")
    print("")
    print("Top 100 events by tournament coverage (HS):")
    for row in top_100_events:
        print(f"- {row['event_name']} | tournaments={row['tournament_count']} | hs_rounds={row['hs_round_count']}")
    print("")
    print("Top 100 tournaments by HS round count:")
    for row in top_100_tournaments:
        print(f"- #{row['rank']} | {row['tourn_id']} | {row['tournament_name']} | hs_rounds={row['hs_round_count']}")
    print("")
    print(f"CSV written: {tournament_rounds_csv}")
    print(f"CSV written: {event_coverage_csv}")

    return {
        "tournament_count_all_levels": len(all_tournament_ids),
        "tournament_count_hs": len(hs_tournament_ids),
        "hs_tournament_event_pair_count": len(hs_event_pairs),
        "hs_round_total": hs_round_total,
        "csv_min_hs_rounds_threshold": min_hs_rounds_for_csv,
        "csv_tournament_count": len(qualifying_tournament_ids),
        "top_100_event_count": len(top_100_events),
        "top_100_tournament_count": len(top_100_tournaments),
        "tournaments_csv": tournament_rounds_csv,
        "events_csv": event_coverage_csv,
        "results_dir": results_dir,
    }
