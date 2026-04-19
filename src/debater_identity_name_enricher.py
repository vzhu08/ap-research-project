import csv
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from tqdm import tqdm

from src.judge_scraper import login, normalized_text
from src.stop_controller import StopController


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


def build_identity_url(base_url: str, url_template: str, debater_id: str) -> str:
    raw = url_template.format(
        debater_id=debater_id,
        person_id=debater_id,
        id=debater_id,
        id1=debater_id,
    )
    return raw if raw.lower().startswith("http") else urljoin(base_url, raw)


def _looks_like_name(value: str) -> bool:
    text = normalized_text(value or "")
    if not text:
        return False
    if len(text) < 2 or len(text) > 80:
        return False
    if any(char.isdigit() for char in text):
        return False

    lowered = text.lower()
    blocked_terms = (
        "tabroom",
        "tournament grid",
        "entry record",
        "record for",
        "show prior seasons",
        "search tournaments",
    )
    if any(term in lowered for term in blocked_terms):
        return False
    return True


def extract_name_from_identity_page(page: BeautifulSoup) -> str:
    # Primary pattern from team_results pages: top profile heading.
    for selector in ("#content h3.nospace", "#content h3", "h3.nospace", "h3"):
        for node in page.select(selector):
            candidate = normalized_text(node.get_text(" ", strip=True))
            if _looks_like_name(candidate):
                return candidate

    # Fallback from inline script templates on the same page.
    regexes = [
        re.compile(r'Record for "\s*\+\s*\'([^\']+)\'\s*\+\s*" with others this season"'),
        re.compile(r'Tournament grid for "\s*\+\s*\'([^\']+)\'\s*\+\s*" - current season"'),
    ]
    for script in page.select("script"):
        script_text = script.string or script.get_text(" ", strip=True) or ""
        for pattern in regexes:
            match = pattern.search(script_text)
            if not match:
                continue
            candidate = normalized_text(match.group(1))
            if _looks_like_name(candidate):
                return candidate

    raise RuntimeError("Could not parse debater name from team_results page")


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
        writer = csv.DictWriter(handle, fieldnames=["debater_id", "source_url", "error"])
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


def _load_debater_ids(conn: sqlite3.Connection, only_blank_names: bool) -> list[str]:
    if only_blank_names:
        rows = conn.execute(
            """
            SELECT debater_id
            FROM debater_identity
            WHERE COALESCE(TRIM(debater_id), '') <> ''
              AND COALESCE(TRIM(debater_name), '') = ''
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
    return [str(row["debater_id"]) for row in rows]


def _count_total_identity_ids(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM debater_identity
        WHERE COALESCE(TRIM(debater_id), '') <> ''
        """
    ).fetchone()
    return int(row["c"] or 0) if row else 0


def _remaining_blank_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM debater_identity
        WHERE COALESCE(TRIM(debater_id), '') <> ''
          AND COALESCE(TRIM(debater_name), '') = ''
        """
    ).fetchone()
    return int(row["c"] or 0) if row else 0


def enrich_debater_identity_names(
    username: str,
    password: str,
    base_url: str,
    debater_db_path: str,
    url_template: str,
    sleep_seconds: float,
    max_retries: int,
    retry_backoff: float,
    only_blank_names: bool = True,
    stop_flag_path: str | None = None,
    enable_signal_stop: bool = True,
    stops_file: str | None = None,
    tqdm_position: int = 0,
    emit_console_logs: bool = True,
) -> dict:
    if not os.path.exists(debater_db_path):
        raise RuntimeError(f"Debater database not found: {debater_db_path}")

    failures_file = os.path.join(os.path.dirname(debater_db_path), "identity_name_failures.csv")
    stops_file = stops_file or os.path.join(os.path.dirname(debater_db_path), "stops.csv")

    with closing(sqlite3.connect(debater_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        table_row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='debater_identity' LIMIT 1"
        ).fetchone()
        if table_row is None:
            raise RuntimeError("Debater database is missing table 'debater_identity'.")

        _ensure_stops_table(conn)
        debater_ids = _load_debater_ids(conn, only_blank_names=only_blank_names)
        total_identity_ids = _count_total_identity_ids(conn)
        initial_completed = max(0, total_identity_ids - len(debater_ids)) if only_blank_names else 0
        progress_total = total_identity_ids if only_blank_names else len(debater_ids)
        if not debater_ids:
            _write_failures_csv(failures_file, [])
            _write_stops_csv(stops_file, _fetch_stop_rows(conn))
            return {
                "processed_count": 0,
                "updated_count": 0,
                "failed_count": 0,
                "remaining_blank_count": _remaining_blank_count(conn),
                "failures_file": failures_file,
                "stopped": False,
                "stop_reason": "",
                "stop_unit_key": "",
                "stops_file": stops_file,
            }

    browser = login(username, password, f"{base_url}/user/login/login.mhtml")
    if emit_console_logs:
        if only_blank_names:
            print(
                "Debater identity name enrichment target count: "
                f"remaining={len(debater_ids)} of total={progress_total} (already_completed={initial_completed})"
            )
        else:
            print(f"Debater identity name enrichment target count: {len(debater_ids)}")
        print("Estimated total time for remaining: warming up from live run timings...")

    failures: list[dict] = []
    sleep_seconds = max(0.0, float(sleep_seconds))
    stop_controller = StopController(stop_flag_path=stop_flag_path, enable_signal_stop=enable_signal_stop)
    stopped = False
    stop_reason = ""
    stop_unit_key = ""
    updated_count = 0
    eta_samples = 0
    eta_avg_seconds_per_id = 0.0
    last_progress_update_at = time.perf_counter()

    try:
        with closing(sqlite3.connect(debater_db_path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            _ensure_stops_table(conn)

            pbar = tqdm(
                total=progress_total,
                initial=initial_completed,
                desc="Debater Identity Names",
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

            for debater_id in debater_ids:
                if stop_controller.stop_requested():
                    stopped = True
                    stop_reason = stop_controller.stop_reason()
                    stop_unit_key = f"debater_id={debater_id}"
                    break

                source_url = build_identity_url(base_url=base_url, url_template=url_template, debater_id=debater_id)
                try:
                    response = None
                    for attempt in range(1, max(1, int(max_retries)) + 1):
                        try:
                            response = browser.session.get(source_url)
                            break
                        except Exception:
                            if attempt >= max(1, int(max_retries)):
                                raise
                            time.sleep(max(0.0, float(retry_backoff)))

                    if response is None or response.status_code != 200:
                        status_code = response.status_code if response is not None else "unknown"
                        raise RuntimeError(f"HTTP {status_code}")

                    page = BeautifulSoup(response.text, "lxml")
                    debater_name = extract_name_from_identity_page(page)
                    with conn:
                        cursor = conn.execute(
                            """
                            UPDATE debater_identity
                            SET debater_name = ?, updated_at = ?
                            WHERE debater_id = ?
                            """,
                            (debater_name, utc_now_iso(), debater_id),
                        )
                        updated_count += int(cursor.rowcount or 0)
                except Exception as exc:
                    failures.append(
                        {
                            "debater_id": debater_id,
                            "source_url": source_url,
                            "error": str(exc),
                        }
                    )
                finally:
                    if sleep_seconds > 0:
                        time.sleep(sleep_seconds)
                pbar.update(1)

                now = time.perf_counter()
                interval = max(0.0, now - last_progress_update_at)
                last_progress_update_at = now
                if interval > 0:
                    eta_samples += 1
                    # True running average over all processed IDs for stable ETA.
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
                            "debater_identity_name_enricher",
                            stop_unit_key,
                            stop_reason,
                            stop_controller.requested_at() or utc_now_iso(),
                            utc_now_iso(),
                        ),
                    )

            remaining_blank = _remaining_blank_count(conn)
            stop_rows = _fetch_stop_rows(conn)
    finally:
        stop_controller.close()

    _write_failures_csv(failures_file, failures)
    _write_stops_csv(stops_file, stop_rows)

    return {
        "processed_count": len(debater_ids),
        "updated_count": updated_count,
        "failed_count": len(failures),
        "remaining_blank_count": remaining_blank,
        "failures_file": failures_file,
        "stopped": stopped,
        "stop_reason": stop_reason,
        "stop_unit_key": stop_unit_key,
        "stops_file": stops_file,
    }
