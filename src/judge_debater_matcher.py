import csv
import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from tqdm import tqdm


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def format_duration(seconds: float) -> str:
    total = int(max(0.0, seconds))
    hrs = total // 3600
    mins = (total % 3600) // 60
    secs = total % 60
    if hrs > 0:
        return f"{hrs}h {mins}m {secs}s"
    if mins > 0:
        return f"{mins}m {secs}s"
    return f"{secs}s"


def parse_tourn_id(url: str) -> int | None:
    try:
        query = parse_qs(urlparse(url or "").query)
        raw = query.get("tourn_id", [None])[0]
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def normalize_text(value: str) -> str:
    return " ".join((value or "").split()).strip()


def normalize_event_label(value: str) -> str:
    return normalize_text(value).upper()


def normalize_team_code(value: str) -> str:
    return normalize_text(value).upper()


def ensure_match_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS judge_round_debaters (
                judge_person_id INTEGER NOT NULL,
                row_index INTEGER NOT NULL,
                side TEXT NOT NULL,
                debater_id TEXT NOT NULL,
                tourn_id INTEGER,
                event_label TEXT,
                team_code TEXT,
                matched_at TEXT NOT NULL,
                PRIMARY KEY (judge_person_id, row_index, side, debater_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_judge_round_debaters_lookup
            ON judge_round_debaters (tourn_id, event_label, team_code)
            """
        )

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(judge_rounds)").fetchall()}
    with conn:
        if "tourn_id" not in columns:
            conn.execute("ALTER TABLE judge_rounds ADD COLUMN tourn_id INTEGER")
        if "aff_id_1" not in columns:
            conn.execute("ALTER TABLE judge_rounds ADD COLUMN aff_id_1 TEXT")
        if "aff_id_2" not in columns:
            conn.execute("ALTER TABLE judge_rounds ADD COLUMN aff_id_2 TEXT")
        if "neg_id_1" not in columns:
            conn.execute("ALTER TABLE judge_rounds ADD COLUMN neg_id_1 TEXT")
        if "neg_id_2" not in columns:
            conn.execute("ALTER TABLE judge_rounds ADD COLUMN neg_id_2 TEXT")
        if "aff_debater_ids_json" not in columns:
            conn.execute("ALTER TABLE judge_rounds ADD COLUMN aff_debater_ids_json TEXT")
        if "neg_debater_ids_json" not in columns:
            conn.execute("ALTER TABLE judge_rounds ADD COLUMN neg_debater_ids_json TEXT")
        if "debater_match_status" not in columns:
            conn.execute("ALTER TABLE judge_rounds ADD COLUMN debater_match_status TEXT")
        if "debater_match_updated_at" not in columns:
            conn.execute("ALTER TABLE judge_rounds ADD COLUMN debater_match_updated_at TEXT")


def build_debater_index(debater_db_path: str) -> dict[tuple[int, str, str], set[str]]:
    if not os.path.exists(debater_db_path):
        raise RuntimeError(f"Debater database not found: {debater_db_path}")

    with closing(sqlite3.connect(debater_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        table_row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='debater_entries' LIMIT 1"
        ).fetchone()
        if table_row is None:
            raise RuntimeError("Debater database is missing table 'debater_entries'.")

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


def write_csv(path: str, fieldnames: list[str], rows: list[dict]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def flush_round_batch(
    conn: sqlite3.Connection,
    update_rows: list[tuple],
    delete_rows: list[tuple[int, int]],
) -> None:
    if not update_rows and not delete_rows:
        return

    with conn:
        if update_rows:
            conn.executemany(
                """
                UPDATE judge_rounds
                SET
                    tourn_id = ?,
                    aff_id_1 = ?,
                    aff_id_2 = ?,
                    neg_id_1 = ?,
                    neg_id_2 = ?,
                    aff_debater_ids_json = ?,
                    neg_debater_ids_json = ?,
                    debater_match_status = ?,
                    debater_match_updated_at = ?
                WHERE judge_person_id = ? AND row_index = ?
                """,
                update_rows,
            )
        if delete_rows:
            conn.executemany(
                """
                DELETE FROM judge_round_debaters
                WHERE judge_person_id = ? AND row_index = ?
                """,
                delete_rows,
            )


def match_debaters_into_judge_db(
    judge_db_path: str,
    debater_db_path: str,
    output_dir: str,
) -> dict:
    if not os.path.exists(judge_db_path):
        raise RuntimeError(f"Judge database not found: {judge_db_path}")

    debater_index = build_debater_index(debater_db_path)
    print(f"Debater index keys: {len(debater_index)}")

    matched_rows_out: list[dict] = []
    unmatched_rows_out: list[dict] = []
    round_matches: list[tuple[int, int, str, str, int | None, str, str, str]] = []
    now_iso = utc_now_iso()

    with closing(sqlite3.connect(judge_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        ensure_match_schema(conn)

        rounds = conn.execute(
            """
            SELECT
                judge_person_id,
                row_index,
                tournament,
                event,
                aff,
                neg,
                tournament_url
            FROM judge_rounds
            ORDER BY judge_person_id, row_index
            """
        ).fetchall()
        print(f"Judge rounds to process: {len(rounds)}")

        update_batch: list[tuple] = []
        delete_batch: list[tuple[int, int]] = []
        batch_size = 2000

        pbar = tqdm(rounds, total=len(rounds), desc="Matching Rounds", unit="round", smoothing=0.9)
        eta_samples = 0
        eta_avg_seconds_per_round = 0.0
        last_iteration_done_at = time.perf_counter()
        for row in pbar:
            judge_person_id = int(row["judge_person_id"])
            row_index = int(row["row_index"])
            tourn_id = parse_tourn_id(row["tournament_url"] or "")
            event_norm = normalize_event_label(row["event"] or "")
            aff_team = row["aff"] or ""
            neg_team = row["neg"] or ""
            aff_norm = normalize_team_code(aff_team)
            neg_norm = normalize_team_code(neg_team)

            aff_ids = set()
            neg_ids = set()

            if tourn_id is not None and event_norm and aff_norm:
                aff_ids = debater_index.get((tourn_id, event_norm, aff_norm), set())
            if tourn_id is not None and event_norm and neg_norm:
                neg_ids = debater_index.get((tourn_id, event_norm, neg_norm), set())

            aff_ids_sorted = sorted(aff_ids)
            neg_ids_sorted = sorted(neg_ids)
            aff_json = json.dumps(aff_ids_sorted, ensure_ascii=False)
            neg_json = json.dumps(neg_ids_sorted, ensure_ascii=False)
            aff_id_1 = aff_ids_sorted[0] if len(aff_ids_sorted) > 0 else ""
            aff_id_2 = aff_ids_sorted[1] if len(aff_ids_sorted) > 1 else ""
            neg_id_1 = neg_ids_sorted[0] if len(neg_ids_sorted) > 0 else ""
            neg_id_2 = neg_ids_sorted[1] if len(neg_ids_sorted) > 1 else ""

            if aff_ids_sorted and neg_ids_sorted:
                status = "both_matched"
            elif aff_ids_sorted:
                status = "aff_only"
            elif neg_ids_sorted:
                status = "neg_only"
            elif tourn_id is None:
                status = "missing_tourn_id"
            elif not event_norm:
                status = "missing_event"
            else:
                status = "no_match"

            update_batch.append(
                (
                    tourn_id,
                    aff_id_1,
                    aff_id_2,
                    neg_id_1,
                    neg_id_2,
                    aff_json,
                    neg_json,
                    status,
                    now_iso,
                    judge_person_id,
                    row_index,
                )
            )
            delete_batch.append((judge_person_id, row_index))

            if len(update_batch) >= batch_size:
                flush_round_batch(conn, update_batch, delete_batch)
                update_batch.clear()
                delete_batch.clear()

            for debater_id in aff_ids_sorted:
                round_matches.append(
                    (
                        judge_person_id,
                        row_index,
                        "aff",
                        debater_id,
                        tourn_id,
                        row["event"] or "",
                        aff_team,
                        now_iso,
                    )
                )
            for debater_id in neg_ids_sorted:
                round_matches.append(
                    (
                        judge_person_id,
                        row_index,
                        "neg",
                        debater_id,
                        tourn_id,
                        row["event"] or "",
                        neg_team,
                        now_iso,
                    )
                )

            detail_row = {
                "judge_person_id": judge_person_id,
                "row_index": row_index,
                "tourn_id": tourn_id if tourn_id is not None else "",
                "tournament": row["tournament"] or "",
                "event": row["event"] or "",
                "aff_team_code": aff_team,
                "neg_team_code": neg_team,
                "aff_id_1": aff_id_1,
                "aff_id_2": aff_id_2,
                "neg_id_1": neg_id_1,
                "neg_id_2": neg_id_2,
                "aff_debater_ids_json": aff_json,
                "neg_debater_ids_json": neg_json,
                "status": status,
            }
            if status in {"both_matched", "aff_only", "neg_only"}:
                matched_rows_out.append(detail_row)
            else:
                unmatched_rows_out.append(detail_row)

            now = time.perf_counter()
            interval = max(0.0, now - last_iteration_done_at)
            last_iteration_done_at = now
            if interval > 0:
                eta_samples += 1
                # True running average over all processed rounds for stable ETA.
                eta_avg_seconds_per_round = (
                    ((eta_avg_seconds_per_round * (eta_samples - 1)) + interval) / eta_samples
                )

                remaining_rounds = max(0, len(rounds) - pbar.n)
                eta_seconds = remaining_rounds * eta_avg_seconds_per_round
                pbar.set_postfix_str(
                    f"ETA~{format_duration(eta_seconds)} avg={eta_avg_seconds_per_round:.2f}s/round",
                    refresh=False,
                )

        pbar.close()
        flush_round_batch(conn, update_batch, delete_batch)

        if round_matches:
            with conn:
                conn.executemany(
                    """
                    INSERT INTO judge_round_debaters (
                        judge_person_id, row_index, side, debater_id, tourn_id,
                        event_label, team_code, matched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    round_matches,
                )

    out_dir = os.path.join(output_dir, "matches")
    matched_csv = os.path.join(out_dir, "judge_round_debater_matches.csv")
    unmatched_csv = os.path.join(out_dir, "judge_round_debater_unmatched.csv")
    write_csv(
        matched_csv,
        [
            "judge_person_id",
            "row_index",
            "tourn_id",
            "tournament",
            "event",
            "aff_team_code",
            "neg_team_code",
            "aff_id_1",
            "aff_id_2",
            "neg_id_1",
            "neg_id_2",
            "aff_debater_ids_json",
            "neg_debater_ids_json",
            "status",
        ],
        matched_rows_out,
    )
    write_csv(
        unmatched_csv,
        [
            "judge_person_id",
            "row_index",
            "tourn_id",
            "tournament",
            "event",
            "aff_team_code",
            "neg_team_code",
            "aff_id_1",
            "aff_id_2",
            "neg_id_1",
            "neg_id_2",
            "aff_debater_ids_json",
            "neg_debater_ids_json",
            "status",
        ],
        unmatched_rows_out,
    )

    print(f"Matched rounds (including partial): {len(matched_rows_out)}")
    print(f"Unmatched rounds: {len(unmatched_rows_out)}")
    print(f"Round-to-debater link rows: {len(round_matches)}")
    print(f"Match output: {matched_csv}")
    print(f"Unmatched output: {unmatched_csv}")

    return {
        "matched_round_count": len(matched_rows_out),
        "unmatched_round_count": len(unmatched_rows_out),
        "round_debater_link_count": len(round_matches),
        "matched_csv": matched_csv,
        "unmatched_csv": unmatched_csv,
    }
