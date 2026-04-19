import csv
import os
import sqlite3
from contextlib import closing

from src.debater_identity_filter import (
    load_qualifying_tournament_ids_from_judge_db,
    normalize_event_label,
    parse_tourn_id,
)


def write_judge_ids_csv(path: str, judge_ids: list[int]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["judge_person_id"])
        writer.writeheader()
        for judge_person_id in judge_ids:
            writer.writerow({"judge_person_id": int(judge_person_id)})


def _table_has_column(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any((row["name"] or "") == column_name for row in rows)


def _collect_candidate_judges(
    judge_db_path: str,
    allowset: set[str],
    qualifying_tournament_ids: set[int],
) -> tuple[set[int], set[int]]:
    with closing(sqlite3.connect(judge_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        table_row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='judge_rounds' LIMIT 1"
        ).fetchone()
        if table_row is None:
            raise RuntimeError("Judge database is missing table 'judge_rounds'.")

        rows = conn.execute(
            """
            SELECT judge_person_id, event, tournament_url
            FROM judge_rounds
            WHERE judge_person_id IS NOT NULL
            """
        ).fetchall()
        all_judge_rows = conn.execute(
            """
            SELECT judge_person_id
            FROM judge_pages
            WHERE judge_person_id IS NOT NULL
            """
        ).fetchall()

    all_judge_ids: set[int] = {int(row["judge_person_id"]) for row in all_judge_rows}
    kept_judge_ids: set[int] = set()
    for row in rows:
        judge_person_id = int(row["judge_person_id"])
        all_judge_ids.add(judge_person_id)

        event_label = normalize_event_label(row["event"] or "")
        if event_label not in allowset:
            continue

        tourn_id = parse_tourn_id(row["tournament_url"] or "")
        if tourn_id is None:
            continue
        if int(tourn_id) in qualifying_tournament_ids:
            kept_judge_ids.add(judge_person_id)

    return all_judge_ids, kept_judge_ids


def _clone_database(source_db_path: str, target_db_path: str) -> None:
    target_parent = os.path.dirname(target_db_path)
    if target_parent:
        os.makedirs(target_parent, exist_ok=True)
    if os.path.abspath(source_db_path) == os.path.abspath(target_db_path):
        raise RuntimeError("Filtered judge DB path must be different from source judge DB path.")
    if os.path.exists(target_db_path):
        os.remove(target_db_path)

    with closing(sqlite3.connect(source_db_path)) as src_conn, closing(sqlite3.connect(target_db_path)) as dst_conn:
        src_conn.backup(dst_conn)


def _prune_non_kept_judges(filtered_db_path: str, kept_judge_ids: set[int]) -> dict[str, int]:
    deleted_by_table: dict[str, int] = {}
    with closing(sqlite3.connect(filtered_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")

        table_rows = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type='table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        table_names = [row["name"] for row in table_rows]

        for table_name in table_names:
            if not _table_has_column(conn, table_name, "judge_person_id"):
                continue

            if kept_judge_ids:
                placeholders = ",".join("?" for _ in kept_judge_ids)
                sql = f"DELETE FROM {table_name} WHERE judge_person_id NOT IN ({placeholders})"
                params = tuple(sorted(kept_judge_ids))
            else:
                sql = f"DELETE FROM {table_name}"
                params = ()

            with conn:
                cursor = conn.execute(sql, params)
            deleted_by_table[table_name] = int(cursor.rowcount or 0)

    return deleted_by_table


def build_filtered_judge_database(
    source_judge_db_path: str,
    filtered_judge_db_path: str,
    allowed_event_labels: list[str],
    output_dir: str,
    min_tournament_hs_rounds: int = 500,
) -> dict:
    if not os.path.exists(source_judge_db_path):
        raise RuntimeError(f"Judge database not found: {source_judge_db_path}")

    allowset = {normalize_event_label(item) for item in allowed_event_labels if normalize_event_label(item)}
    if not allowset:
        raise RuntimeError("Event allowlist is empty; provide at least one event label.")

    qualifying_tournament_ids = load_qualifying_tournament_ids_from_judge_db(
        judge_db_path=source_judge_db_path,
        min_hs_rounds=min_tournament_hs_rounds,
    )
    all_judge_ids, kept_judge_ids = _collect_candidate_judges(
        judge_db_path=source_judge_db_path,
        allowset=allowset,
        qualifying_tournament_ids=qualifying_tournament_ids,
    )

    _clone_database(source_db_path=source_judge_db_path, target_db_path=filtered_judge_db_path)
    deleted_by_table = _prune_non_kept_judges(
        filtered_db_path=filtered_judge_db_path,
        kept_judge_ids=kept_judge_ids,
    )

    ids_csv = os.path.join(output_dir, "judges", "judge_ids_filtered.csv")
    write_judge_ids_csv(ids_csv, sorted(kept_judge_ids))

    removed_count = max(0, len(all_judge_ids) - len(kept_judge_ids))
    print(
        "Judge DB filter complete | "
        f"all_unique_judges={len(all_judge_ids)} | "
        f"qualifying_tournaments={len(qualifying_tournament_ids)} | "
        f"kept_judges={len(kept_judge_ids)} | "
        f"removed_judges={removed_count}"
    )
    print(f"Filtered judge IDs CSV: {ids_csv}")
    print(f"Filtered judge DB: {filtered_judge_db_path}")

    return {
        "all_unique_judges": len(all_judge_ids),
        "kept_judges": len(kept_judge_ids),
        "removed_judges": removed_count,
        "filtered_judge_db_path": filtered_judge_db_path,
        "filtered_judge_ids_csv": ids_csv,
        "allowlist_size": len(allowset),
        "qualifying_tournament_count": len(qualifying_tournament_ids),
        "min_tournament_hs_rounds": int(min_tournament_hs_rounds),
        "deleted_rows_by_table": deleted_by_table,
    }
