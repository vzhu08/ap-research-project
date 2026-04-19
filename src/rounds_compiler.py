import csv
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse


def default_round_database_path(output_dir: str) -> str:
    return os.path.join(output_dir, "rounds", "round_data.sqlite3")


def default_round_progress_path(output_dir: str) -> str:
    return os.path.join(output_dir, "rounds", "progress.csv")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_tourn_id(url: str) -> int | None:
    try:
        query = parse_qs(urlparse(url or "").query)
        raw = query.get("tourn_id", [None])[0]
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def normalize_event_label(value: str) -> str:
    return " ".join((value or "").split()).strip().upper()


def normalize_team_code(value: str) -> str:
    return " ".join((value or "").split()).strip().upper()


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def load_event_code_map(debater_db_path: str | None) -> dict[tuple[int, str], int]:
    if not debater_db_path or not os.path.exists(debater_db_path):
        return {}

    with closing(sqlite3.connect(debater_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        if not table_exists(conn, "debater_tournament_events"):
            return {}

        rows = conn.execute(
            """
            SELECT tourn_id, judge_event_label, event_id
            FROM debater_tournament_events
            WHERE tourn_id IS NOT NULL
              AND COALESCE(judge_event_label, '') <> ''
              AND event_id IS NOT NULL
            """
        ).fetchall()

    mapping: dict[tuple[int, str], int] = {}
    for row in rows:
        key = (int(row["tourn_id"]), normalize_event_label(row["judge_event_label"]))
        event_id = int(row["event_id"])
        current = mapping.get(key)
        if current is None or event_id < current:
            mapping[key] = event_id
    return mapping


def load_debater_match_index(debater_db_path: str | None) -> dict[tuple[int, str, str], set[str]]:
    if not debater_db_path or not os.path.exists(debater_db_path):
        return {}

    with closing(sqlite3.connect(debater_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        if not table_exists(conn, "debater_entries"):
            return {}

        rows = conn.execute(
            """
            SELECT debater_id, tourn_id, judge_event_label, team_code
            FROM debater_entries
            WHERE COALESCE(debater_id, '') <> ''
              AND COALESCE(team_code, '') <> ''
              AND tourn_id IS NOT NULL
              AND COALESCE(judge_event_label, '') <> ''
            """
        ).fetchall()

    index: dict[tuple[int, str, str], set[str]] = {}
    for row in rows:
        key = (
            int(row["tourn_id"]),
            normalize_event_label(row["judge_event_label"]),
            normalize_team_code(row["team_code"]),
        )
        index.setdefault(key, set()).add(str(row["debater_id"]))
    return index


def create_rounds_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(
            """
            DROP TABLE IF EXISTS compiled_rounds;

            CREATE TABLE compiled_rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER,
                tournament_name TEXT,
                event_code INTEGER,
                event_name TEXT,
                judge_id INTEGER NOT NULL,
                judge_name TEXT,
                level TEXT,
                date_text TEXT,
                date_sort_key TEXT,
                aff_team_code TEXT,
                neg_team_code TEXT,
                aff_id_1 TEXT,
                aff_id_2 TEXT,
                neg_id_1 TEXT,
                neg_id_2 TEXT,
                aff_debater_ids_json TEXT,
                neg_debater_ids_json TEXT,
                source_row_index INTEGER NOT NULL
            );

            CREATE INDEX idx_compiled_rounds_tournament
                ON compiled_rounds (tournament_id, date_sort_key, event_name);

            CREATE INDEX idx_compiled_rounds_judge
                ON compiled_rounds (judge_id, source_row_index);
            """
        )


def append_progress_row(progress_file: str, row: dict) -> None:
    parent = os.path.dirname(progress_file)
    if parent:
        os.makedirs(parent, exist_ok=True)

    write_header = not os.path.exists(progress_file)
    fieldnames = [
        "run_at",
        "status",
        "total_round_count",
        "hs_round_count",
        "removed_non_hs_round_count",
        "event_code_map_size",
        "rounds_database_path",
        "error",
    ]
    with open(progress_file, "a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow({name: row.get(name, "") for name in fieldnames})


def compile_hs_rounds_database(
    judge_db_path: str,
    output_dir: str,
    debater_db_path: str | None = None,
    rounds_db_path: str | None = None,
    progress_file: str | None = None,
    force_reprocess: bool = False,
) -> dict:
    if not os.path.exists(judge_db_path):
        raise RuntimeError(f"Judge database not found: {judge_db_path}")

    run_at = utc_now_iso()
    rounds_db_path = rounds_db_path or default_round_database_path(output_dir)
    progress_file = progress_file or default_round_progress_path(output_dir)
    rounds_parent = os.path.dirname(rounds_db_path)
    if rounds_parent:
        os.makedirs(rounds_parent, exist_ok=True)

    if force_reprocess:
        print("Rounds force reprocess enabled: clearing rounds DB/progress before compile.")
        if os.path.exists(rounds_db_path):
            os.remove(rounds_db_path)
        if os.path.exists(progress_file):
            os.remove(progress_file)

    print(f"Rounds compiler: source judge DB = {judge_db_path}")
    print(f"Rounds compiler: output rounds DB = {rounds_db_path}")

    total_round_count = 0
    try:
        event_code_map = load_event_code_map(debater_db_path)
        debater_match_index = load_debater_match_index(debater_db_path)

        with closing(sqlite3.connect(judge_db_path)) as judge_conn:
            judge_conn.row_factory = sqlite3.Row
            if not table_exists(judge_conn, "judge_rounds"):
                raise RuntimeError("Judge database is missing table 'judge_rounds'.")

            round_columns = {
                row["name"] for row in judge_conn.execute("PRAGMA table_info(judge_rounds)").fetchall()
            }

            tourn_id_select = "jr.tourn_id AS tourn_id" if "tourn_id" in round_columns else "NULL AS tourn_id"

            total_round_count = int(judge_conn.execute("SELECT COUNT(*) FROM judge_rounds").fetchone()[0] or 0)
            print(f"Rounds compiler: total judge_rounds rows = {total_round_count}")

            rows = judge_conn.execute(
                f"""
                SELECT
                    jr.judge_person_id,
                    jp.judge_name,
                    jr.row_index,
                    jr.tournament,
                    jr.level,
                    jr.date_text,
                    jr.date_sort_key,
                    jr.event,
                    jr.aff,
                    jr.neg,
                    jr.tournament_url,
                    {tourn_id_select}
                FROM judge_rounds jr
                LEFT JOIN judge_pages jp
                    ON jp.judge_person_id = jr.judge_person_id
                WHERE UPPER(TRIM(COALESCE(jr.level, ''))) = 'HS'
                """
            ).fetchall()
            print(f"Rounds compiler: HS rows selected = {len(rows)}")

        compiled_rows = []
        rows_with_any_match = 0
        for row in rows:
            judge_id = int(row["judge_person_id"])
            row_index = int(row["row_index"])
            tournament_id = row["tourn_id"]
            if tournament_id in {"", None}:
                tournament_id = parse_tourn_id(row["tournament_url"] or "")
            else:
                tournament_id = int(tournament_id)

            event_name = row["event"] or ""
            event_norm = normalize_event_label(event_name)
            aff_team_code = row["aff"] or ""
            neg_team_code = row["neg"] or ""
            aff_norm = normalize_team_code(aff_team_code)
            neg_norm = normalize_team_code(neg_team_code)

            event_code = None
            if tournament_id is not None:
                event_code = event_code_map.get((int(tournament_id), normalize_event_label(event_name)))

            aff_ids_sorted: list[str] = []
            neg_ids_sorted: list[str] = []
            if tournament_id is not None and event_norm:
                if aff_norm:
                    aff_ids_sorted = sorted(debater_match_index.get((int(tournament_id), event_norm, aff_norm), set()))
                if neg_norm:
                    neg_ids_sorted = sorted(debater_match_index.get((int(tournament_id), event_norm, neg_norm), set()))

            if aff_ids_sorted or neg_ids_sorted:
                rows_with_any_match += 1

            compiled_rows.append(
                {
                    "tournament_id": tournament_id,
                    "tournament_name": row["tournament"] or "",
                    "event_code": event_code,
                    "event_name": event_name,
                    "judge_id": judge_id,
                    "judge_name": row["judge_name"] or "",
                    "level": row["level"] or "",
                    "date_text": row["date_text"] or "",
                    "date_sort_key": row["date_sort_key"] or "",
                    "aff_team_code": aff_team_code,
                    "neg_team_code": neg_team_code,
                    "aff_id_1": aff_ids_sorted[0] if len(aff_ids_sorted) > 0 else "",
                    "aff_id_2": aff_ids_sorted[1] if len(aff_ids_sorted) > 1 else "",
                    "neg_id_1": neg_ids_sorted[0] if len(neg_ids_sorted) > 0 else "",
                    "neg_id_2": neg_ids_sorted[1] if len(neg_ids_sorted) > 1 else "",
                    "aff_debater_ids_json": json.dumps(aff_ids_sorted, ensure_ascii=False),
                    "neg_debater_ids_json": json.dumps(neg_ids_sorted, ensure_ascii=False),
                    "source_row_index": row_index,
                }
            )

        compiled_rows.sort(
            key=lambda item: (
                item["tournament_id"] if item["tournament_id"] is not None else 2_000_000_000,
                item["date_sort_key"],
                (item["event_name"] or "").upper(),
                (item["judge_name"] or "").upper(),
                item["judge_id"],
                item["source_row_index"],
            )
        )

        with closing(sqlite3.connect(rounds_db_path)) as rounds_conn:
            create_rounds_schema(rounds_conn)
            with rounds_conn:
                rounds_conn.executemany(
                    """
                    INSERT INTO compiled_rounds (
                        tournament_id,
                        tournament_name,
                        event_code,
                        event_name,
                        judge_id,
                        judge_name,
                        level,
                        date_text,
                        date_sort_key,
                        aff_team_code,
                        neg_team_code,
                        aff_id_1,
                        aff_id_2,
                        neg_id_1,
                        neg_id_2,
                        aff_debater_ids_json,
                        neg_debater_ids_json,
                        source_row_index
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            item["tournament_id"],
                            item["tournament_name"],
                            item["event_code"],
                            item["event_name"],
                            item["judge_id"],
                            item["judge_name"],
                            item["level"],
                            item["date_text"],
                            item["date_sort_key"],
                            item["aff_team_code"],
                            item["neg_team_code"],
                            item["aff_id_1"],
                            item["aff_id_2"],
                            item["neg_id_1"],
                            item["neg_id_2"],
                            item["aff_debater_ids_json"],
                            item["neg_debater_ids_json"],
                            item["source_row_index"],
                        )
                        for item in compiled_rows
                    ],
                )

        hs_round_count = len(compiled_rows)
        result = {
            "rounds_database_path": rounds_db_path,
            "total_round_count": total_round_count,
            "hs_round_count": hs_round_count,
            "removed_non_hs_round_count": total_round_count - hs_round_count,
            "event_code_map_size": len(event_code_map),
            "rows_with_any_debater_match": rows_with_any_match,
        }
        append_progress_row(
            progress_file,
            {
                "run_at": run_at,
                "status": "completed",
                "total_round_count": result["total_round_count"],
                "hs_round_count": result["hs_round_count"],
                "removed_non_hs_round_count": result["removed_non_hs_round_count"],
                "event_code_map_size": result["event_code_map_size"],
                "rounds_database_path": result["rounds_database_path"],
                "error": "",
            },
        )
        print(
            "Rounds compiler complete | "
            f"hs_rounds={result['hs_round_count']} | "
            f"filtered_non_hs={result['removed_non_hs_round_count']} | "
            f"event_codes={result['event_code_map_size']} | "
            f"rows_with_any_debater_match={result['rows_with_any_debater_match']}"
        )
        print(f"Rounds compiler progress log: {progress_file}")
        return result
    except Exception as exc:
        append_progress_row(
            progress_file,
            {
                "run_at": run_at,
                "status": "failed",
                "total_round_count": total_round_count,
                "hs_round_count": "",
                "removed_non_hs_round_count": "",
                "event_code_map_size": "",
                "rounds_database_path": rounds_db_path,
                "error": str(exc).replace("\n", " ").replace("\r", " "),
            },
        )
        raise
