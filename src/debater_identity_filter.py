import csv
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_event_label(value: str) -> str:
    return " ".join((value or "").split()).strip().upper()


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


def ensure_identity_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS debater_identity (
                debater_id TEXT PRIMARY KEY,
                debater_name TEXT NOT NULL,
                race_probability_vector_json TEXT,
                updated_at TEXT NOT NULL
            );
            """
        )
    ensure_column(conn, "debater_identity", "race_probability_vector_json", "TEXT")


def ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, column_sql: str) -> None:
    columns = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    existing = {row["name"] for row in columns}
    if column_name not in existing:
        with conn:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")


def write_ids_csv(path: str, ids: list[str]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["debater_id"])
        writer.writeheader()
        for debater_id in ids:
            writer.writerow({"debater_id": debater_id})


def load_qualifying_tournament_ids_from_judge_db(
    judge_db_path: str,
    min_hs_rounds: int,
) -> set[int]:
    if not os.path.exists(judge_db_path):
        raise RuntimeError(f"Judge database not found: {judge_db_path}")

    with closing(sqlite3.connect(judge_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        table_row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='judge_rounds' LIMIT 1"
        ).fetchone()
        if table_row is None:
            raise RuntimeError("Judge database is missing table 'judge_rounds'.")

        rows = conn.execute(
            """
            SELECT tournament_url, level
            FROM judge_rounds
            WHERE COALESCE(tournament_url, '') <> ''
            """
        ).fetchall()

    hs_round_counts: dict[int, int] = {}
    for row in rows:
        tourn_id = parse_tourn_id(row["tournament_url"] or "")
        if tourn_id is None:
            continue
        if not is_hs_level(row["level"] or ""):
            continue
        hs_round_counts[tourn_id] = hs_round_counts.get(tourn_id, 0) + 1

    return {tourn_id for tourn_id, count in hs_round_counts.items() if count >= min_hs_rounds}


def rebuild_debater_identity_for_events(
    debater_db_path: str,
    allowed_event_labels: list[str],
    output_dir: str,
    judge_db_path: str,
    min_tournament_hs_rounds: int = 100,
) -> dict:
    if not os.path.exists(debater_db_path):
        raise RuntimeError(f"Debater database not found: {debater_db_path}")

    allowset = {normalize_event_label(item) for item in allowed_event_labels if normalize_event_label(item)}
    if not allowset:
        raise RuntimeError("Event allowlist is empty; provide at least one event label.")
    qualifying_tournament_ids = load_qualifying_tournament_ids_from_judge_db(
        judge_db_path=judge_db_path,
        min_hs_rounds=min_tournament_hs_rounds,
    )

    with closing(sqlite3.connect(debater_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        ensure_identity_schema(conn)

        table_row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='debater_entries' LIMIT 1"
        ).fetchone()
        if table_row is None:
            raise RuntimeError("Debater database is missing table 'debater_entries'.")

        existing_identity = conn.execute(
            """
            SELECT debater_id, debater_name, race_probability_vector_json
            FROM debater_identity
            """
        ).fetchall()
        existing_name_by_id = {str(row["debater_id"]): (row["debater_name"] or "") for row in existing_identity}
        existing_race_vector_by_id = {
            str(row["debater_id"]): (row["race_probability_vector_json"] or "")
            for row in existing_identity
        }

        rows = conn.execute(
            """
            SELECT DISTINCT debater_id, judge_event_label, tourn_id
            FROM debater_entries
            WHERE COALESCE(debater_id, '') <> ''
              AND COALESCE(judge_event_label, '') <> ''
              AND tourn_id IS NOT NULL
            """
        ).fetchall()

        all_unique_ids = {str(row["debater_id"]) for row in rows}
        kept_ids = {
            str(row["debater_id"])
            for row in rows
            if normalize_event_label(row["judge_event_label"]) in allowset
            and int(row["tourn_id"]) in qualifying_tournament_ids
        }

        now_iso = utc_now_iso()
        with conn:
            conn.execute("DELETE FROM debater_identity")
            if kept_ids:
                conn.executemany(
                    """
                    INSERT INTO debater_identity (
                        debater_id, debater_name, race_probability_vector_json, updated_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        (
                            debater_id,
                            existing_name_by_id.get(debater_id, ""),
                            existing_race_vector_by_id.get(debater_id, ""),
                            now_iso,
                        )
                        for debater_id in sorted(kept_ids)
                    ],
                )

    csv_path = os.path.join(output_dir, "debaters", "debater_identity_ids_filtered.csv")
    write_ids_csv(csv_path, sorted(kept_ids))

    removed_count = max(0, len(all_unique_ids) - len(kept_ids))
    print(
        "Debater identity filter complete | "
        f"all_unique_ids={len(all_unique_ids)} | "
        f"qualifying_tournaments={len(qualifying_tournament_ids)} | "
        f"kept_ids={len(kept_ids)} | removed_ids={removed_count}"
    )
    print(f"Filtered identity ID CSV: {csv_path}")

    return {
        "all_unique_ids": len(all_unique_ids),
        "kept_ids": len(kept_ids),
        "removed_ids": removed_count,
        "identity_ids_csv": csv_path,
        "allowlist_size": len(allowset),
        "qualifying_tournament_count": len(qualifying_tournament_ids),
        "min_tournament_hs_rounds": int(min_tournament_hs_rounds),
    }
