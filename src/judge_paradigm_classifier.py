import csv
import json
import os
import re
import sqlite3
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import closing
from datetime import datetime, timezone
from urllib import error as urlerror
from urllib import request as urlrequest

from bs4 import BeautifulSoup
from tqdm import tqdm

from src.stop_controller import StopController


LAY_CATEGORY = 0
TECH_CATEGORY = 1
DEFAULT_OLLAMA_MODEL = "qwen2.5:7b"
DEFAULT_OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"
SYSTEM_PROMPT = """You are an expert debate judge paradigm classifier.

Task:
Classify a judge paradigm into exactly one category:
- 0 = Lay
- 1 = Tech

Definitions:
- Lay: Limited technical debate background; prioritizes clarity, persuasion, and communication over technical line-by-line argumentation.
- Tech: Strong technical debate background; prioritizes flow-based, line-by-line technical debating, evidence comparison, theory/framework, and technical execution.

Decision rules:
- Strongly weight explicit self-identification (e.g., "I am a lay/traditional judge," "I'm tab/national circuit," etc.).
- Consider position (e.g., "I am a debate coach" as tech and "I am a parent" as lay).
- Consider time spent in the activity (e.g., "I have judged for 30 years" as tech and "This is my first tournament judging" as lay).
- Consider technical jargon density (e.g., CP, DA, K, T, theory, perms, condo, framework, RVI, presumption, flow).
- Consider communication emphasis (e.g., "slow down," "be clear for me," "I don't flow speed") as Lay evidence.
- Consider explicit comfort with speed/spreading and technical theory as Tech evidence.
- If mixed signals exist, choose the dominant judging preference described for ballot decisions.

Output requirements:
Return strict JSON only:
{
  "category": 0 or 1
}
No extra text before or after JSON."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "integer", "enum": [LAY_CATEGORY, TECH_CATEGORY]},
    },
    "required": ["category"],
    "additionalProperties": False,
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
            CREATE TABLE IF NOT EXISTS judge_scrape_stops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phase TEXT NOT NULL,
                unit_key TEXT,
                reason TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                stopped_at TEXT NOT NULL
            )
            """
        )


def _ensure_schema(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "judge_pages", "paradigm_category", "INTEGER")
    _ensure_column(conn, "judge_pages", "paradigm_classification_justification", "TEXT")
    _ensure_column(conn, "judge_pages", "paradigm_classification_model", "TEXT")
    _ensure_column(conn, "judge_pages", "paradigm_classified_at", "TEXT")
    _ensure_stops_table(conn)


def _fetch_stop_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, phase, unit_key, reason, requested_at, stopped_at
        FROM judge_scrape_stops
        ORDER BY id ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _write_failures_csv(path: str, failure_rows: list[dict]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["judge_person_id", "judge_name", "error"])
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


def _normalize_paradigm_text(paradigm_html: str) -> str:
    soup = BeautifulSoup(paradigm_html or "", "html.parser")
    text = soup.get_text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _load_target_rows(conn: sqlite3.Connection, only_blank_categories: bool) -> list[dict]:
    if only_blank_categories:
        rows = conn.execute(
            """
            SELECT judge_person_id, judge_name, paradigm_html
            FROM judge_pages
            WHERE COALESCE(TRIM(paradigm_html), '') <> ''
              AND (paradigm_category IS NULL OR paradigm_category NOT IN (0, 1))
            ORDER BY judge_person_id
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT judge_person_id, judge_name, paradigm_html
            FROM judge_pages
            WHERE COALESCE(TRIM(paradigm_html), '') <> ''
            ORDER BY judge_person_id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _count_total_paradigm_rows(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM judge_pages
        WHERE COALESCE(TRIM(paradigm_html), '') <> ''
        """
    ).fetchone()
    return int(row["c"] or 0) if row else 0


def _remaining_uncategorized_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM judge_pages
        WHERE COALESCE(TRIM(paradigm_html), '') <> ''
          AND (paradigm_category IS NULL OR paradigm_category NOT IN (0, 1))
        """
    ).fetchone()
    return int(row["c"] or 0) if row else 0


def _clear_existing_classifications(
    conn: sqlite3.Connection,
) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM judge_pages").fetchone()
    total_rows = int(row["c"] or 0) if row else 0
    with conn:
        conn.execute(
            """
            UPDATE judge_pages
            SET paradigm_category = NULL,
                paradigm_classification_justification = NULL,
                paradigm_classification_model = NULL,
                paradigm_classified_at = NULL
            """
        )
    return total_rows


def _build_user_prompt(paradigm_text: str) -> str:
    return (
        "Classify this judge paradigm.\n"
        "Return JSON matching the schema exactly.\n\n"
        f"Paradigm:\n{paradigm_text}"
    )


def _validate_model_output(payload: dict) -> int:
    category = payload.get("category")

    if category not in {LAY_CATEGORY, TECH_CATEGORY}:
        raise RuntimeError(f"Invalid category returned by model: {category!r}")
    return int(category)


def _call_ollama_chat(
    ollama_chat_url: str,
    model_name: str,
    paradigm_text: str,
    request_timeout_seconds: float,
    num_ctx: int,
) -> int:
    request_payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(paradigm_text)},
        ],
        "stream": False,
        "think": False,
        "keep_alive": -1,
        "format": OUTPUT_SCHEMA,
        "options": {
            "temperature": 0,
            "num_ctx": max(512, int(num_ctx)),
            "num_predict": 8,
        },
    }
    request_data = json.dumps(request_payload).encode("utf-8")
    req = urlrequest.Request(
        ollama_chat_url,
        data=request_data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlrequest.urlopen(req, timeout=max(1.0, float(request_timeout_seconds))) as resp:
            raw = resp.read().decode("utf-8")
    except urlerror.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        raise RuntimeError(f"Ollama HTTP error ({exc.code}): {body or str(exc)}") from exc
    except urlerror.URLError as exc:
        raise RuntimeError(f"Ollama connection error: {exc}") from exc

    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from Ollama response envelope: {raw[:200]}") from exc

    content = ((envelope.get("message") or {}).get("content") or "").strip()
    if not content:
        raise RuntimeError("Ollama returned empty message content.")

    try:
        model_payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Ollama content was not valid JSON: {content[:200]}") from exc

    return _validate_model_output(model_payload)


def _truncate_paradigm_text(paradigm_text: str, max_input_chars: int) -> str:
    cap = max(200, int(max_input_chars))
    text = (paradigm_text or "").strip()
    if len(text) <= cap:
        return text
    return text[:cap].rstrip()


def _classify_single_judge(
    judge_person_id: int,
    judge_name: str,
    paradigm_text: str,
    ollama_chat_url: str,
    model_name: str,
    request_timeout_seconds: float,
    max_retries: int,
    retry_backoff: float,
    num_ctx: int,
) -> dict:
    category: int | None = None
    last_error = ""
    tries = max(1, int(max_retries) + 1)
    for attempt in range(1, tries + 1):
        try:
            category = _call_ollama_chat(
                ollama_chat_url=ollama_chat_url,
                model_name=model_name,
                paradigm_text=paradigm_text,
                request_timeout_seconds=request_timeout_seconds,
                num_ctx=num_ctx,
            )
            break
        except Exception as exc:
            last_error = str(exc)
            if attempt < tries:
                time.sleep(max(0.0, float(retry_backoff)))

    return {
        "judge_person_id": int(judge_person_id),
        "judge_name": judge_name,
        "category": category,
        "error": last_error,
    }


def _warm_up_model(
    ollama_chat_url: str,
    model_name: str,
    request_timeout_seconds: float,
    num_ctx: int,
) -> None:
    # Prime model load + KV/cache path before the main loop to reduce startup stalls.
    _call_ollama_chat(
        ollama_chat_url=ollama_chat_url,
        model_name=model_name,
        paradigm_text="Please speak clearly and avoid speed.",
        request_timeout_seconds=request_timeout_seconds,
        num_ctx=num_ctx,
    )


def classify_judge_paradigms_with_ollama(
    judge_db_path: str,
    model_name: str = DEFAULT_OLLAMA_MODEL,
    ollama_chat_url: str = DEFAULT_OLLAMA_CHAT_URL,
    only_blank_categories: bool = True,
    force_reprocess: bool = False,
    request_timeout_seconds: float = 90.0,
    max_retries: int = 1,
    retry_backoff: float = 0.5,
    max_input_chars: int = 1800,
    max_parallel_requests: int = 8,
    num_ctx: int = 1024,
    warmup_model: bool = True,
    stop_flag_path: str | None = None,
    enable_signal_stop: bool = True,
    stops_file: str | None = None,
    tqdm_position: int = 0,
    emit_console_logs: bool = True,
) -> dict:
    if not os.path.exists(judge_db_path):
        raise RuntimeError(f"Judge database not found: {judge_db_path}")

    failures_file = os.path.join(os.path.dirname(judge_db_path), "paradigm_classification_failures.csv")
    stops_file = stops_file or os.path.join(os.path.dirname(judge_db_path), "stops.csv")

    forced_reset_count = 0
    with closing(sqlite3.connect(judge_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        table_row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='judge_pages' LIMIT 1"
        ).fetchone()
        if table_row is None:
            raise RuntimeError("Judge database is missing table 'judge_pages'.")

        _ensure_schema(conn)
        effective_only_blank_categories = bool(only_blank_categories) and not bool(force_reprocess)

        if force_reprocess:
            print("Force reset enabled: clearing existing judge paradigm classifications...")
            forced_reset_count = _clear_existing_classifications(conn)
            print(f"Force reset complete: cleared {forced_reset_count} rows.")

        target_rows = _load_target_rows(conn, only_blank_categories=effective_only_blank_categories)
        total_rows = _count_total_paradigm_rows(conn)
        initial_completed = max(0, total_rows - len(target_rows)) if effective_only_blank_categories else 0
        progress_total = total_rows if effective_only_blank_categories else len(target_rows)
        if not target_rows:
            _write_failures_csv(failures_file, [])
            _write_stops_csv(stops_file, _fetch_stop_rows(conn))
            return {
                "processed_count": 0,
                "updated_count": 0,
                "failed_count": 0,
                "remaining_unclassified_count": _remaining_uncategorized_count(conn),
                "failures_file": failures_file,
                "stopped": False,
                "stop_reason": "",
                "stop_unit_key": "",
                "stops_file": stops_file,
                "model": model_name,
                "forced_reset_count": forced_reset_count,
            }

    if emit_console_logs:
        if effective_only_blank_categories:
            print(
                "Judge paradigm classification target count: "
                f"remaining={len(target_rows)} of total={progress_total} (already_completed={initial_completed})"
            )
        else:
            print(f"Judge paradigm classification target count: {len(target_rows)}")
        print(f"Using Ollama model: {model_name}")
        print(f"Ollama endpoint: {ollama_chat_url}")

    if warmup_model:
        try:
            if emit_console_logs:
                print("Warming up Ollama model...")
            _warm_up_model(
                ollama_chat_url=ollama_chat_url,
                model_name=model_name,
                request_timeout_seconds=min(float(request_timeout_seconds), 30.0),
                num_ctx=num_ctx,
            )
        except Exception as exc:
            if emit_console_logs:
                print(f"Warmup warning (continuing anyway): {exc}")

    failures: list[dict] = []
    stop_controller = StopController(stop_flag_path=stop_flag_path, enable_signal_stop=enable_signal_stop)
    stopped = False
    stop_reason = ""
    stop_unit_key = ""
    processed_count = 0
    updated_count = 0
    eta_samples = 0
    eta_avg_seconds_per_judge = 0.0
    last_progress_update_at = time.perf_counter()

    try:
        with closing(sqlite3.connect(judge_db_path)) as conn:
            conn.row_factory = sqlite3.Row
            _ensure_schema(conn)
            conn.execute("PRAGMA busy_timeout=5000")

            pbar = tqdm(
                total=progress_total,
                initial=initial_completed,
                desc="Judge Paradigm LLM",
                unit="judge",
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

            parallelism = max(1, int(max_parallel_requests))
            total_rows = len(target_rows)
            next_row_index = 0
            in_flight: dict[Future, tuple[int, str]] = {}

            def submit_next(executor: ThreadPoolExecutor) -> None:
                nonlocal next_row_index, processed_count
                if next_row_index >= total_rows:
                    return
                row = target_rows[next_row_index]
                next_row_index += 1

                judge_person_id = int(row["judge_person_id"])
                judge_name = (row["judge_name"] or "").strip()
                paradigm_text = _truncate_paradigm_text(
                    _normalize_paradigm_text(row.get("paradigm_html") or ""),
                    max_input_chars=max_input_chars,
                )
                if not paradigm_text:
                    failures.append(
                        {
                            "judge_person_id": judge_person_id,
                            "judge_name": judge_name,
                            "error": "Paradigm text is blank after HTML normalization.",
                        }
                    )
                    processed_count += 1
                    pbar.update(1)
                    return

                future = executor.submit(
                    _classify_single_judge,
                    judge_person_id,
                    judge_name,
                    paradigm_text,
                    ollama_chat_url,
                    model_name,
                    request_timeout_seconds,
                    max_retries,
                    retry_backoff,
                    num_ctx,
                )
                in_flight[future] = (judge_person_id, judge_name)

            with ThreadPoolExecutor(max_workers=parallelism) as executor:
                while len(in_flight) < parallelism and next_row_index < total_rows:
                    if stop_controller.stop_requested():
                        stopped = True
                        stop_reason = stop_controller.stop_reason()
                        stop_unit_key = f"judge_person_id={target_rows[next_row_index]['judge_person_id']}"
                        break
                    submit_next(executor)

                while in_flight:
                    if stop_controller.stop_requested():
                        stopped = True
                        stop_reason = stop_controller.stop_reason()
                        stop_unit_key = f"judge_person_id={next(iter(in_flight.values()))[0]}"
                        break

                    done, _ = wait(set(in_flight.keys()), timeout=0.2, return_when=FIRST_COMPLETED)
                    if not done:
                        continue

                    for future in done:
                        judge_person_id, judge_name = in_flight.pop(future)
                        try:
                            result = future.result()
                        except Exception as exc:
                            result = {
                                "judge_person_id": judge_person_id,
                                "judge_name": judge_name,
                                "category": None,
                                "error": str(exc),
                            }

                        category = result.get("category")
                        if category is None:
                            failures.append(
                                {
                                    "judge_person_id": judge_person_id,
                                    "judge_name": judge_name,
                                    "error": result.get("error") or "Unknown classification error.",
                                }
                            )
                        else:
                            with conn:
                                cursor = conn.execute(
                                    """
                                    UPDATE judge_pages
                                    SET paradigm_category = ?,
                                        paradigm_classification_justification = ?,
                                        paradigm_classification_model = ?,
                                        paradigm_classified_at = ?
                                    WHERE judge_person_id = ?
                                    """,
                                    (
                                        int(category),
                                        "",
                                        model_name,
                                        utc_now_iso(),
                                        judge_person_id,
                                    ),
                                )
                            updated_count += int(cursor.rowcount or 0)

                        processed_count += 1
                        pbar.update(1)

                        now = time.perf_counter()
                        interval = max(0.0, now - last_progress_update_at)
                        last_progress_update_at = now
                        if interval > 0:
                            eta_samples += 1
                            eta_avg_seconds_per_judge = (
                                ((eta_avg_seconds_per_judge * (eta_samples - 1)) + interval) / eta_samples
                            )
                            remaining_judges = max(0, progress_total - pbar.n)
                            eta_seconds = remaining_judges * eta_avg_seconds_per_judge
                            render_postfix(
                                f"ETA~{format_duration(eta_seconds)} avg={eta_avg_seconds_per_judge:.2f}s/judge"
                            )

                    while len(in_flight) < parallelism and next_row_index < total_rows and not stopped:
                        submit_next(executor)

            pbar.close()

            if stopped:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO judge_scrape_stops (phase, unit_key, reason, requested_at, stopped_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            "judge_paradigm_classifier",
                            stop_unit_key,
                            stop_reason,
                            stop_controller.requested_at() or utc_now_iso(),
                            utc_now_iso(),
                        ),
                    )

            remaining_uncategorized_count = _remaining_uncategorized_count(conn)
            stop_rows = _fetch_stop_rows(conn)
    finally:
        stop_controller.close()

    _write_failures_csv(failures_file, failures)
    _write_stops_csv(stops_file, stop_rows)

    return {
        "processed_count": processed_count,
        "updated_count": updated_count,
        "failed_count": len(failures),
        "remaining_unclassified_count": remaining_uncategorized_count,
        "failures_file": failures_file,
        "stopped": stopped,
        "stop_reason": stop_reason,
        "stop_unit_key": stop_unit_key,
        "stops_file": stops_file,
        "model": model_name,
        "forced_reset_count": forced_reset_count,
    }
