import csv
import json
import math
import os
import sqlite3
import time
from collections import deque
from contextlib import closing
from datetime import datetime, timezone

from tqdm import tqdm

from src.stop_controller import StopController


RACE_VECTOR_ORDER = ("asian", "black", "hispanic", "white")
RACE_MODEL_NAME = "predict_race_fl"
COMMON_SUFFIXES = {
    "jr",
    "sr",
    "ii",
    "iii",
    "iv",
    "v",
    "vi",
    "vii",
    "viii",
    "ix",
    "x",
    "phd",
    "md",
    "esq",
}


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


def _ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, column_sql: str) -> None:
    columns = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    existing = {row["name"] for row in columns}
    if column_name not in existing:
        with conn:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")


def _ensure_stops_table(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS debater_scrape_stops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phase TEXT NOT NULL,
                unit_key TEXT,
                reason TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                stopped_at TEXT NOT NULL
            )
            """
        )


def _fetch_stop_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, phase, unit_key, reason, requested_at, stopped_at
        FROM debater_scrape_stops
        ORDER BY id ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _write_failures_csv(path: str, failure_rows: list[dict]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["debater_id", "debater_name", "error"])
        writer.writeheader()
        for row in failure_rows:
            writer.writerow(row)


def _write_stops_csv(path: str, stop_rows: list[dict]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["id", "phase", "unit_key", "reason", "requested_at", "stopped_at"],
        )
        writer.writeheader()
        for row in stop_rows:
            writer.writerow(
                {
                    "id": row.get("id", ""),
                    "phase": row.get("phase", ""),
                    "unit_key": row.get("unit_key", ""),
                    "reason": row.get("reason", ""),
                    "requested_at": row.get("requested_at", ""),
                    "stopped_at": row.get("stopped_at", ""),
                }
            )


def _parse_name(full_name: str) -> tuple[str, str]:
    raw_tokens = [token for token in (full_name or "").strip().split() if token]
    tokens = [token.strip(" ,.") for token in raw_tokens if token.strip(" ,.")]

    # Strip one or more trailing suffix tokens (e.g., "Jr.", "III", "PhD").
    while tokens:
        normalized_last = tokens[-1].lower().replace(".", "")
        if normalized_last in COMMON_SUFFIXES:
            tokens.pop()
            continue
        break

    if not tokens:
        raise ValueError("Name is blank")
    if len(tokens) == 1:
        # Fallback for single-token names so imputation can still proceed.
        return tokens[0], tokens[0]
    # Middle tokens are intentionally ignored: use only first and last.
    return tokens[0], tokens[-1]


def _to_records(frame) -> list[dict]:
    if hasattr(frame, "to_dict"):
        try:
            return frame.to_dict(orient="records")
        except TypeError:
            pass
    if hasattr(frame, "to_dicts"):
        return frame.to_dicts()
    if hasattr(frame, "to_pydict"):
        col_map = frame.to_pydict()
        keys = list(col_map.keys())
        count = len(col_map[keys[0]]) if keys else 0
        return [{key: col_map[key][idx] for key in keys} for idx in range(count)]
    raise TypeError(f"Unsupported pyethnicity return type: {type(frame)}")


def _extract_probability_map(record: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, value in record.items():
        key_norm = str(key).strip().lower()
        if key_norm not in RACE_VECTOR_ORDER:
            continue
        try:
            val = float(value)
        except Exception as exc:
            raise RuntimeError(f"Non-numeric probability for '{key_norm}': {value!r}") from exc
        if math.isnan(val):
            raise RuntimeError(f"NaN probability for '{key_norm}'")
        out[key_norm] = val

    missing = [label for label in RACE_VECTOR_ORDER if label not in out]
    if missing:
        raise RuntimeError(f"Missing probability columns from pyethnicity output: {missing}")
    return out


def _build_vector_json(probability_map: dict[str, float]) -> str:
    payload = {
        "order": list(RACE_VECTOR_ORDER),
        "values": [float(probability_map[label]) for label in RACE_VECTOR_ORDER],
        "model": RACE_MODEL_NAME,
    }
    return json.dumps(payload, ensure_ascii=False)


def _load_identity_rows(conn: sqlite3.Connection, only_blank_vectors: bool) -> list[dict]:
    if only_blank_vectors:
        rows = conn.execute(
            """
            SELECT debater_id
            FROM debater_identity
            WHERE COALESCE(TRIM(debater_id), '') <> ''
              AND COALESCE(TRIM(race_probability_vector_json), '') = ''
            ORDER BY debater_id
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT debater_id
            FROM debater_identity
            WHERE COALESCE(TRIM(debater_id), '') <> ''
            ORDER BY debater_id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _load_debater_name(conn: sqlite3.Connection, debater_id: str) -> str:
    row = conn.execute(
        """
        SELECT debater_name
        FROM debater_identity
        WHERE debater_id = ?
        """,
        (debater_id,),
    ).fetchone()
    return (row["debater_name"] or "").strip() if row else ""


def _count_total_identity_ids(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM debater_identity
        WHERE COALESCE(TRIM(debater_id), '') <> ''
        """
    ).fetchone()
    return int(row["c"] or 0) if row else 0


def _remaining_blank_vector_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM debater_identity
        WHERE COALESCE(TRIM(debater_id), '') <> ''
          AND COALESCE(TRIM(race_probability_vector_json), '') = ''
        """
    ).fetchone()
    return int(row["c"] or 0) if row else 0


def _clear_vectors(conn: sqlite3.Connection) -> int:
    with conn:
        cursor = conn.execute(
            """
            UPDATE debater_identity
            SET race_probability_vector_json = NULL,
                updated_at = ?
            WHERE COALESCE(TRIM(debater_id), '') <> ''
            """,
            (utc_now_iso(),),
        )
    return int(cursor.rowcount or 0)


def impute_debater_identity_race_probabilities(
    debater_db_path: str,
    only_blank_vectors: bool = True,
    force_reprocess: bool = False,
    wait_for_name_poll_seconds: float = 2.0,
    stop_flag_path: str | None = None,
    enable_signal_stop: bool = True,
    stops_file: str | None = None,
    tqdm_position: int = 0,
    emit_console_logs: bool = True,
) -> dict:
    if not os.path.exists(debater_db_path):
        raise RuntimeError(f"Debater database not found: {debater_db_path}")

    try:
        import pyethnicity
    except Exception as exc:
        raise RuntimeError(
            "PyEthnicity is not installed. Install dependencies (including `pyethnicity`) before running this stage."
        ) from exc

    failures_file = os.path.join(os.path.dirname(debater_db_path), "identity_race_failures.csv")
    stops_file = stops_file or os.path.join(os.path.dirname(debater_db_path), "stops.csv")

    forced_reset_count = 0
    with closing(sqlite3.connect(debater_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        table_row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='debater_identity' LIMIT 1"
        ).fetchone()
        if table_row is None:
            raise RuntimeError("Debater database is missing table 'debater_identity'.")

        _ensure_column(conn, "debater_identity", "race_probability_vector_json", "TEXT")
        _ensure_stops_table(conn)

        if force_reprocess:
            forced_reset_count = _clear_vectors(conn)

        identity_rows = _load_identity_rows(conn, only_blank_vectors=only_blank_vectors)
        total_identity_ids = _count_total_identity_ids(conn)
        initial_completed = max(0, total_identity_ids - len(identity_rows)) if only_blank_vectors else 0
        progress_total = total_identity_ids if only_blank_vectors else len(identity_rows)
        if not identity_rows:
            _write_failures_csv(failures_file, [])
            _write_stops_csv(stops_file, _fetch_stop_rows(conn))
            return {
                "processed_count": 0,
                "updated_count": 0,
                "failed_count": 0,
                "remaining_blank_vector_count": _remaining_blank_vector_count(conn),
                "failures_file": failures_file,
                "stopped": False,
                "stop_reason": "",
                "stop_unit_key": "",
                "stops_file": stops_file,
                "vector_order": list(RACE_VECTOR_ORDER),
                "model": RACE_MODEL_NAME,
                "forced_reset_count": forced_reset_count,
            }

    wait_for_name_poll_seconds = max(0.1, float(wait_for_name_poll_seconds))
    if emit_console_logs:
        if only_blank_vectors:
            print(
                "Debater identity race-imputation target count: "
                f"remaining={len(identity_rows)} of total={progress_total} (already_completed={initial_completed})"
            )
        else:
            print(f"Debater identity race-imputation target count: {len(identity_rows)}")
        print("Estimated total time for remaining: warming up from live run timings...")
        print(f"Race vector order: {list(RACE_VECTOR_ORDER)}")
        print(
            "Parallel-safe mode: if debater_name is blank, waiting for name enricher update "
            f"(poll={wait_for_name_poll_seconds:.2f}s)"
        )

    failures: list[dict] = []
    stop_controller = StopController(stop_flag_path=stop_flag_path, enable_signal_stop=enable_signal_stop)
    stopped = False
    stop_reason = ""
    stop_unit_key = ""
    updated_count = 0
    processed_count = 0
    eta_samples = 0
    eta_avg_seconds_per_id = 0.0
    last_progress_update_at = time.perf_counter()

    try:
        with closing(sqlite3.connect(debater_db_path)) as conn:
            conn.row_factory = sqlite3.Row
            _ensure_column(conn, "debater_identity", "race_probability_vector_json", "TEXT")
            _ensure_stops_table(conn)
            conn.execute("PRAGMA busy_timeout=5000")

            pbar = tqdm(
                total=progress_total,
                initial=initial_completed,
                desc="Debater Identity Race",
                unit="id",
                smoothing=0.9,
                position=max(0, int(tqdm_position)),
                mininterval=0.5,
                maxinterval=2.0,
                dynamic_ncols=True,
                leave=True,
            )
            last_postfix_render_at = 0.0

            def render_postfix(status_text: str) -> None:
                nonlocal last_postfix_render_at
                now_render = time.perf_counter()
                if (now_render - last_postfix_render_at) < 0.5:
                    return
                pbar.set_postfix_str(status_text, refresh=False)
                last_postfix_render_at = now_render

            pending_ids = deque(str(row.get("debater_id", "")).strip() for row in identity_rows)
            pending_ids = deque([debater_id for debater_id in pending_ids if debater_id])
            no_progress_in_pass = 0

            while pending_ids:
                if stop_controller.stop_requested():
                    stopped = True
                    stop_reason = stop_controller.stop_reason()
                    stop_unit_key = f"debater_id={pending_ids[0]}"
                    break

                debater_id = pending_ids.popleft()
                current_name = _load_debater_name(conn, debater_id)
                if not current_name:
                    pending_ids.append(debater_id)
                    no_progress_in_pass += 1
                    if no_progress_in_pass >= max(1, len(pending_ids)):
                        remaining = max(0, progress_total - pbar.n)
                        render_postfix(
                            f"waiting_for_names poll={wait_for_name_poll_seconds:.1f}s remaining={remaining}",
                        )
                        time.sleep(wait_for_name_poll_seconds)
                        no_progress_in_pass = 0
                    continue

                try:
                    first_name, last_name = _parse_name(current_name)
                    frame = pyethnicity.predict_race_fl(first_name=[first_name], last_name=[last_name])
                    records = _to_records(frame)
                    if len(records) != 1:
                        raise RuntimeError(f"PyEthnicity row-count mismatch (expected 1, got {len(records)})")
                    probability_map = _extract_probability_map(records[0])
                    vector_json = _build_vector_json(probability_map)
                    with conn:
                        cursor = conn.execute(
                            """
                            UPDATE debater_identity
                            SET race_probability_vector_json = ?,
                                updated_at = ?
                            WHERE debater_id = ?
                            """,
                            (vector_json, utc_now_iso(), debater_id),
                        )
                    updated_count += int(cursor.rowcount or 0)
                except Exception as exc:
                    failures.append(
                        {
                            "debater_id": debater_id,
                            "debater_name": current_name,
                            "error": str(exc),
                        }
                    )

                processed_count += 1
                no_progress_in_pass = 0
                pbar.update(1)

                now = time.perf_counter()
                interval = max(0.0, now - last_progress_update_at)
                last_progress_update_at = now
                if interval > 0:
                    eta_samples += 1
                    eta_avg_seconds_per_id = (
                        ((eta_avg_seconds_per_id * (eta_samples - 1)) + interval) / eta_samples
                    )
                    remaining_ids = max(0, progress_total - pbar.n)
                    eta_seconds = remaining_ids * eta_avg_seconds_per_id
                    render_postfix(
                        f"ETA~{format_duration(eta_seconds)} avg={eta_avg_seconds_per_id:.2f}s/id"
                    )

            pbar.close()

            if stopped:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO debater_scrape_stops (phase, unit_key, reason, requested_at, stopped_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            "debater_identity_race_imputer",
                            stop_unit_key,
                            stop_reason,
                            stop_controller.requested_at() or utc_now_iso(),
                            utc_now_iso(),
                        ),
                    )

            remaining_blank_count = _remaining_blank_vector_count(conn)
            stop_rows = _fetch_stop_rows(conn)
    finally:
        stop_controller.close()

    _write_failures_csv(failures_file, failures)
    _write_stops_csv(stops_file, stop_rows)

    return {
        "processed_count": processed_count,
        "updated_count": updated_count,
        "failed_count": len(failures),
        "remaining_blank_vector_count": remaining_blank_count,
        "failures_file": failures_file,
        "stopped": stopped,
        "stop_reason": stop_reason,
        "stop_unit_key": stop_unit_key,
        "stops_file": stops_file,
        "vector_order": list(RACE_VECTOR_ORDER),
        "model": RACE_MODEL_NAME,
        "forced_reset_count": forced_reset_count,
    }
