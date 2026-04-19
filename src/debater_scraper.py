import csv
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
from tqdm import tqdm

from src.judge_scraper import login, normalized_text
from src.stop_controller import StopController
from src.storage import SQLiteDebaterStorage


TEAM_RESULTS_PATH = "/index/results/team_results.mhtml"
FIELDS_PATH = "/index/tourn/fields.mhtml"
RESULTS_INDEX_PATH = "/index/tourn/results/index.mhtml"


def init_debater_output_dir(output_dir: str) -> str:
    debaters_dir = os.path.join(output_dir, "debaters")
    os.makedirs(debaters_dir, exist_ok=True)
    return debaters_dir


def default_debater_database_path(output_dir: str) -> str:
    return os.path.join(output_dir, "debaters", "debater_data.sqlite3")


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


def init_progress_writer(progress_file: str):
    write_header = not os.path.exists(progress_file)
    handle = open(progress_file, "a", newline="", encoding="utf-8-sig")
    writer = csv.DictWriter(
        handle,
        fieldnames=[
            "tourn_id",
            "tournament_name",
            "judge_event_label",
            "event_id",
            "status",
            "row_count",
            "stored_at",
        ],
    )
    if write_header:
        writer.writeheader()
    return handle, writer


def load_processed_event_keys_from_progress_csv(progress_file: str) -> set[tuple[int, str]]:
    if not os.path.exists(progress_file):
        return set()

    accepted_statuses = {"completed", "completed_empty", "unmatched"}
    processed: set[tuple[int, str]] = set()

    with open(progress_file, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            status = (row.get("status") or "").strip()
            if status not in accepted_statuses and not status.startswith("skipped_tournament_"):
                continue
            try:
                tourn_id = int((row.get("tourn_id") or "").strip())
            except Exception:
                continue
            judge_event_label = (row.get("judge_event_label") or "").strip()
            if not judge_event_label:
                continue
            processed.add((tourn_id, judge_event_label))

    return processed


def init_failures_writer(failures_file: str):
    parent = os.path.dirname(failures_file)
    if parent:
        os.makedirs(parent, exist_ok=True)


def init_stops_writer(stops_file: str):
    parent = os.path.dirname(stops_file)
    if parent:
        os.makedirs(parent, exist_ok=True)


def write_failures_csv(failures_file: str, failure_rows: list[dict]) -> None:
    with open(failures_file, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "id",
                "tourn_id",
                "tournament_name",
                "judge_event_label",
                "event_id",
                "error",
                "failed_at",
                "status",
                "resolved_at",
            ],
        )
        writer.writeheader()
        for row in failure_rows:
            writer.writerow(
                {
                    "id": row.get("id", ""),
                    "tourn_id": row.get("tourn_id", ""),
                    "tournament_name": row.get("tournament_name", ""),
                    "judge_event_label": row.get("judge_event_label", ""),
                    "event_id": row.get("event_id", ""),
                    "error": row.get("error", ""),
                    "failed_at": row.get("failed_at", ""),
                    "status": "solved" if int(row.get("is_resolved", 0) or 0) else "open",
                    "resolved_at": row.get("resolved_at", ""),
                }
            )


def write_stops_csv(stops_file: str, stop_rows: list[dict]) -> None:
    with open(stops_file, "w", newline="", encoding="utf-8-sig") as handle:
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


def normalize_sidebar_label(label: str) -> str:
    cleaned = normalized_text(label)
    if cleaned.endswith(" Results"):
        cleaned = cleaned[: -len(" Results")]
    return cleaned


def parse_tourn_id(url: str) -> int | None:
    try:
        query = parse_qs(urlparse(url).query)
        raw = query.get("tourn_id", [None])[0]
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def load_tournaments_from_judge_db(database_path: str) -> list[dict]:
    if not os.path.exists(database_path):
        raise RuntimeError(f"Judge database not found: {database_path}")

    tourn_map: dict[int, dict] = {}

    with closing(sqlite3.connect(database_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT tournament_url, tournament, event
            FROM judge_rounds
            WHERE COALESCE(tournament_url, '') <> ''
              AND COALESCE(event, '') <> ''
            ORDER BY tournament
            """
        ).fetchall()

    for row in rows:
        tourn_id = parse_tourn_id(row["tournament_url"])
        if tourn_id is None:
            continue

        bucket = tourn_map.setdefault(
            tourn_id,
            {
                "tourn_id": tourn_id,
                "tournament_name": row["tournament"] or "",
                "judge_events": set(),
            },
        )

        if not bucket["tournament_name"] and row["tournament"]:
            bucket["tournament_name"] = row["tournament"]

        event_label = normalized_text(row["event"] or "")
        if event_label:
            bucket["judge_events"].add(event_label)

    tournaments = []
    for tournament in sorted(tourn_map.values(), key=lambda item: item["tourn_id"]):
        tournaments.append(
            {
                "tourn_id": tournament["tourn_id"],
                "tournament_name": tournament["tournament_name"],
                "judge_events": sorted(tournament["judge_events"]),
            }
        )

    if not tournaments:
        raise RuntimeError(
            "No tournaments found in the judge database. The judge DB appears to be missing judging-record rows."
        )
    return tournaments


def write_tournament_ids_csv(debaters_dir: str, tournament_rows: list[dict]) -> str:
    path = os.path.join(debaters_dir, "tournament_ids.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "tourn_id",
                "tournament_name",
                "judge_event_label",
                "matched_sidebar_label",
                "dropdown_label",
                "event_id",
                "status",
            ],
        )
        writer.writeheader()
        for row in tournament_rows:
            writer.writerow(row)
    return path


def fetch_with_retries(browser, url: str, max_retries: int, retry_backoff: float):
    response = None
    for attempt in range(1, max_retries + 1):
        try:
            response = browser.session.get(url)
            break
        except Exception:
            if attempt == max_retries:
                raise
            # Intentionally no backoff delay: prefer immediate retries for speed.
            continue

    if response is None or response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code if response is not None else 'unknown'} for {url}")

    return response


def parse_results_event_options(page: BeautifulSoup) -> list[dict]:
    select = page.select_one('select[name="event_id"]')
    if not select:
        return []

    options = []
    for option in select.select("option[value]"):
        raw_value = option.get("value", "").strip()
        if not raw_value:
            continue
        try:
            event_id = int(raw_value)
        except ValueError:
            continue
        options.append(
            {
                "event_id": event_id,
                "dropdown_label": normalized_text(option.get_text(" ", strip=True)),
            }
        )
    return options


def parse_selected_event_id(page: BeautifulSoup) -> int | None:
    select = page.select_one('select[name="event_id"]')
    if not select:
        return None

    selected = select.select_one("option[selected]")
    if selected is None:
        selected = select.select_one("option")
    if selected is None:
        return None

    raw_value = selected.get("value", "").strip()
    try:
        return int(raw_value)
    except ValueError:
        return None


def parse_results_sidebar_label(page: BeautifulSoup) -> str:
    sidenotes = page.select("div.sidenote h4")
    for heading in sidenotes:
        text = normalized_text(heading.get_text(" ", strip=True))
        if text.endswith(" Results") and text != "Event Results":
            return normalize_sidebar_label(text)
    return ""


def build_results_page_url(base_url: str, tourn_id: int, event_id: int | None = None) -> str:
    query = {"tourn_id": tourn_id}
    if event_id is not None:
        query["event_id"] = event_id
    return f"{base_url}{RESULTS_INDEX_PATH}?{urlencode(query)}"


def resolve_event_ids_for_tournament(
    browser,
    base_url: str,
    tournament: dict,
    max_retries: int,
    retry_backoff: float,
    sleep_min: float,
    sleep_max: float,
) -> tuple[dict[str, dict], list[dict]]:
    tourn_id = tournament["tourn_id"]
    initial_url = build_results_page_url(base_url, tourn_id)
    initial_page = BeautifulSoup(
        fetch_with_retries(browser, initial_url, max_retries, retry_backoff).text,
        "lxml",
    )

    event_options = parse_results_event_options(initial_page)
    matched: dict[str, dict] = {}
    unmatched = []
    target_events = list(tournament["judge_events"])
    target_events_norm = {event: normalize_sidebar_label(event) for event in target_events}

    # Fast path: map directly from the single results-index page dropdown labels.
    normalized_option_buckets: dict[str, list[dict]] = {}
    for option in event_options:
        normalized_dropdown = normalize_sidebar_label(option["dropdown_label"])
        normalized_option_buckets.setdefault(normalized_dropdown, []).append(option)

    used_event_ids = set()
    for judge_event in target_events:
        judge_norm = target_events_norm[judge_event]
        option_candidates = normalized_option_buckets.get(judge_norm, [])
        picked = None
        for candidate in option_candidates:
            if candidate["event_id"] in used_event_ids:
                continue
            picked = candidate
            break
        if picked is None:
            continue

        used_event_ids.add(picked["event_id"])
        matched[judge_event] = {
            "event_id": picked["event_id"],
            "matched_sidebar_label": judge_event,
            "dropdown_label": picked["dropdown_label"],
            "source_url": build_results_page_url(base_url, tourn_id, event_id=picked["event_id"]),
        }

    # Fallback path: only probe unresolved judge events with per-event results calls.
    unresolved_judge_events = {event for event in target_events if event not in matched}
    if unresolved_judge_events:
        for option in event_options:
            if option["event_id"] in used_event_ids:
                continue

            event_id = option["event_id"]
            results_url = build_results_page_url(base_url, tourn_id, event_id=event_id)
            results_page = BeautifulSoup(
                fetch_with_retries(browser, results_url, max_retries, retry_backoff).text,
                "lxml",
            )

            selected_event_id = parse_selected_event_id(results_page)
            sidebar_label = parse_results_sidebar_label(results_page)
            if not sidebar_label:
                continue

            for judge_event in list(unresolved_judge_events):
                if normalize_sidebar_label(judge_event) != normalize_sidebar_label(sidebar_label):
                    continue
                matched[judge_event] = {
                    "event_id": selected_event_id or event_id,
                    "matched_sidebar_label": sidebar_label,
                    "dropdown_label": option["dropdown_label"],
                    "source_url": results_url,
                }
                unresolved_judge_events.remove(judge_event)
                break

            if not unresolved_judge_events:
                break

    for judge_event in target_events:
        if judge_event not in matched:
            unmatched.append(
                {
                    "tourn_id": tourn_id,
                    "tournament_name": tournament["tournament_name"],
                    "judge_event_label": judge_event,
                    "matched_sidebar_label": "",
                    "dropdown_label": "",
                    "event_id": "",
                    "status": "unmatched",
                }
            )

    return matched, unmatched


def build_entries_page_urls(base_url: str, tourn_id: int, event_id: int) -> list[str]:
    base = f"{base_url}{FIELDS_PATH}"
    urls = [
        f"{base}?{urlencode({'tourn_id': tourn_id, 'event_id': event_id})}",
        f"{base}?{urlencode({'event_id': event_id, 'tourn_id': tourn_id})}",
        f"{base}?{urlencode({'tourn_id': tourn_id})}&event_id={event_id}",
    ]
    return list(dict.fromkeys(urls))


def extract_debater_ids_from_link(url: str) -> list[str]:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    ids = []
    for key in ("id1", "id2", "id", "person_id"):
        for value in query.get(key, []):
            if value and value.isdigit():
                ids.append(value)

    # Preserve order while deduplicating.
    deduped = []
    seen = set()
    for value in ids:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def parse_debater_names_from_anchor_text(anchor_text: str) -> list[str]:
    text = normalized_text(anchor_text or "")
    if not text:
        return []

    # Common separators for partner/team formatting.
    normalized = text.replace(" vs ", " / ").replace(" and ", " / ").replace("&", "/")
    parts = [normalized_text(part) for part in normalized.split("/") if normalized_text(part)]
    seen = set()
    names = []
    for part in parts:
        lowered = part.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        names.append(part)
    return names


def parse_entries_page_for_debater_rows(
    page: BeautifulSoup,
    base_url: str,
    tourn_id: int,
    judge_event_label: str,
    matched_sidebar_label: str,
    event_id: int,
    tournament_name: str,
) -> list[dict]:
    rows = []
    seen_pairs = set()

    target_table = None
    target_header_row = None
    code_idx = None

    def extract_headers_from_table(table) -> tuple[list[str], object | None]:
        header_cells = table.select("thead th")
        header_row = None
        if header_cells:
            header_row = table.select_one("thead tr")
        else:
            for tr in table.select("tr"):
                th_cells = tr.find_all("th")
                if th_cells:
                    header_cells = th_cells
                    header_row = tr
                    break
        headers = [normalized_text(cell.get_text(" ", strip=True)).lower() for cell in header_cells]
        return headers, header_row

    for table in page.select("table"):
        headers, header_row = extract_headers_from_table(table)
        if "code" in headers:
            target_table = table
            code_idx = headers.index("code")
            target_header_row = header_row
            break

    if target_table is None:
        target_table = page.select_one("table")

    if target_table:
        if code_idx is None:
            headers, header_row = extract_headers_from_table(target_table)
            code_idx = headers.index("code") if "code" in headers else None
            target_header_row = header_row

        if code_idx is None:
            return []

        for tr in target_table.select("tr"):
            if target_header_row is not None and tr == target_header_row:
                continue

            if tr.find_all("th"):
                continue

            cells = tr.find_all("td")
            if not cells:
                continue

            if code_idx >= len(cells):
                continue

            team_code = normalized_text(cells[code_idx].get_text(" ", strip=True))
            if not team_code or team_code.lower() == "code":
                continue

            row_links = []
            row_ids = set()
            row_name_by_id: dict[str, str] = {}
            for anchor in tr.find_all("a", href=True):
                full_url = urljoin(base_url, anchor.get("href", "").strip())
                parsed = urlparse(full_url)
                if "results" not in parsed.path:
                    continue
                if "id1=" not in full_url and "id2=" not in full_url and "id=" not in full_url and "person_id=" not in full_url:
                    continue
                ids = extract_debater_ids_from_link(full_url)
                if not ids:
                    continue
                row_links.append(full_url)
                row_ids.update(ids)
                parsed_names = parse_debater_names_from_anchor_text(anchor.get_text(" ", strip=True))
                if len(parsed_names) == len(ids):
                    for debater_id, debater_name in zip(ids, parsed_names):
                        row_name_by_id.setdefault(debater_id, debater_name)
                elif len(parsed_names) == 1 and len(ids) == 1:
                    row_name_by_id.setdefault(ids[0], parsed_names[0])

            if not row_ids:
                continue

            source_url = row_links[0]
            for debater_id in sorted(row_ids):
                key = (debater_id, team_code, source_url)
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                rows.append(
                    {
                        "debater_id": debater_id,
                        "team_code": team_code,
                        "tourn_id": tourn_id,
                        "tournament_name": tournament_name,
                        "judge_event_label": judge_event_label,
                        "matched_sidebar_label": matched_sidebar_label,
                        "event_id": event_id,
                        "source_url": source_url,
                        "debater_name": row_name_by_id.get(debater_id, ""),
                    }
                )

    return rows


def entries_page_has_code_table(page: BeautifulSoup) -> bool:
    for table in page.select("table"):
        header_cells = table.select("thead th")
        headers = [normalized_text(cell.get_text(" ", strip=True)).lower() for cell in header_cells]
        if "code" in headers:
            return True

        if not header_cells:
            for tr in table.select("tr"):
                th_cells = tr.find_all("th")
                if not th_cells:
                    continue
                headers = [normalized_text(cell.get_text(" ", strip=True)).lower() for cell in th_cells]
                if "code" in headers:
                    return True
                break
    return False


def scrape_entries_for_event(
    browser,
    base_url: str,
    tourn_id: int,
    tournament_name: str,
    judge_event_label: str,
    resolved_event: dict,
    max_retries: int,
    retry_backoff: float,
    sleep_min: float,
    sleep_max: float,
) -> list[dict]:
    last_error = None
    found_entries_page = False
    for url in build_entries_page_urls(base_url, tourn_id, resolved_event["event_id"]):
        try:
            page = BeautifulSoup(
                fetch_with_retries(browser, url, max_retries, retry_backoff).text,
                "lxml",
            )
            if entries_page_has_code_table(page):
                found_entries_page = True
            rows = parse_entries_page_for_debater_rows(
                page,
                base_url=base_url,
                tourn_id=tourn_id,
                tournament_name=tournament_name,
                judge_event_label=judge_event_label,
                matched_sidebar_label=resolved_event["matched_sidebar_label"],
                event_id=resolved_event["event_id"],
            )
            if rows:
                return rows
        except Exception as exc:
            last_error = exc

    if not found_entries_page:
        raise RuntimeError(
            f"Entries page with Code column not found for tourn_id={tourn_id}, event_id={resolved_event['event_id']}"
        )

    if last_error is not None:
        raise last_error

    return []


def write_csv(path: str, fieldnames: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def reset_debater_storage_for_reprocess(storage: SQLiteDebaterStorage) -> None:
    with storage.conn:
        storage.conn.execute("DELETE FROM debater_entries")
        storage.conn.execute("DELETE FROM debater_identity")
        storage.conn.execute("DELETE FROM debater_name_observations")
        storage.conn.execute("DELETE FROM debater_tournament_events")
        storage.conn.execute("DELETE FROM debater_progress")
        storage.conn.execute("DELETE FROM debater_profiles")


def write_debater_output_files(
    debaters_dir: str,
    debater_rows: list[dict],
    unmatched_rows: list[dict],
    debater_profiles: list[dict],
) -> dict:
    debater_ids_path = os.path.join(debaters_dir, "debater_ids.csv")
    unique_ids_path = os.path.join(debaters_dir, "debater_ids_unique.csv")
    unmatched_path = os.path.join(debaters_dir, "unmatched_events.csv")
    profiles_path = os.path.join(debaters_dir, "debater_profiles.csv")

    write_csv(
        debater_ids_path,
        [
            "debater_id",
            "debater_name",
            "team_code",
            "tourn_id",
            "tournament_name",
            "judge_event_label",
            "matched_sidebar_label",
            "event_id",
            "source_url",
        ],
        debater_rows,
    )

    unique_seen = set()
    unique_rows = []
    for row in sorted(
        debater_rows,
        key=lambda item: (item["debater_id"], item["tourn_id"], item["team_code"], item["judge_event_label"]),
    ):
        key = (row["debater_id"], row["tourn_id"], row["team_code"])
        if key in unique_seen:
            continue
        unique_seen.add(key)
        unique_rows.append(
            {
                "debater_id": row["debater_id"],
                "debater_name": row.get("debater_name", ""),
                "team_code": row["team_code"],
                "tourn_id": row["tourn_id"],
                "tournament_name": row["tournament_name"],
            }
        )
    write_csv(unique_ids_path, ["debater_id", "debater_name", "team_code", "tourn_id", "tournament_name"], unique_rows)

    write_csv(
        unmatched_path,
        ["tourn_id", "tournament_name", "judge_event_label", "matched_sidebar_label", "dropdown_label", "event_id", "status"],
        unmatched_rows,
    )

    write_csv(
        profiles_path,
        ["debater_id", "tournament_count", "tournaments_json", "updated_at"],
        debater_profiles,
    )

    return {
        "debater_ids_csv": debater_ids_path,
        "debater_ids_unique_csv": unique_ids_path,
        "unmatched_events_csv": unmatched_path,
        "debater_profiles_csv": profiles_path,
    }


def scrape_debater_ids_from_judge_db(
    username: str,
    password: str,
    base_url: str,
    database_path: str,
    output_dir: str,
    sleep_min: float,
    sleep_max: float,
    max_retries: int,
    retry_backoff: float,
    debater_database_path: str | None = None,
    progress_file: str | None = None,
    failures_file: str | None = None,
    stop_flag_path: str | None = None,
    enable_signal_stop: bool = True,
    stops_file: str | None = None,
    force_reprocess: bool = False,
) -> dict:
    tournaments = load_tournaments_from_judge_db(database_path)
    debaters_dir = init_debater_output_dir(output_dir)
    debater_database_path = debater_database_path or default_debater_database_path(output_dir)
    progress_file = progress_file or os.path.join(debaters_dir, "progress.csv")
    failures_file = failures_file or os.path.join(debaters_dir, "failures.csv")
    stops_file = stops_file or os.path.join(debaters_dir, "stops.csv")
    print(f"Found {len(tournaments)} unique tournaments")
    print(f"Debater SQLite database: {debater_database_path}")
    print(f"Debater progress CSV: {progress_file}")

    browser = login(username, password, f"{base_url}/user/login/login.mhtml")
    print("Logged in successfully")

    storage = SQLiteDebaterStorage(debater_database_path)
    stop_controller = StopController(stop_flag_path=stop_flag_path, enable_signal_stop=enable_signal_stop)

    if force_reprocess:
        print("Force reprocess enabled: clearing debater entries/progress tables before scraping.")
        reset_debater_storage_for_reprocess(storage)
        if os.path.exists(progress_file):
            os.remove(progress_file)
        processed_event_keys = set()
    else:
        processed_event_keys = storage.load_processed_event_keys()
        processed_from_progress_csv = load_processed_event_keys_from_progress_csv(progress_file)
        if processed_from_progress_csv:
            processed_event_keys.update(processed_from_progress_csv)
        print(f"Resuming with {len(processed_event_keys)} completed event keys already stored")

    total_events = sum(len(tournament["judge_events"]) for tournament in tournaments)
    configured_event_keys = {
        (int(tournament["tourn_id"]), judge_event_label)
        for tournament in tournaments
        for judge_event_label in tournament["judge_events"]
    }
    initial_done = len(processed_event_keys.intersection(configured_event_keys))
    remaining_events = total_events - initial_done
    print(f"Processing {remaining_events} remaining events of {total_events}")
    print("Estimated total time for remaining: warming up from live run timings...")

    progress_handle, progress_writer = init_progress_writer(progress_file)
    progress_flush_every = 25
    pending_progress_writes = 0
    eta_samples = 0
    eta_avg_seconds_per_event = 0.0
    last_progress_update_at = time.perf_counter()

    def flush_progress(force: bool = False) -> None:
        nonlocal pending_progress_writes
        if force or pending_progress_writes >= progress_flush_every:
            progress_handle.flush()
            pending_progress_writes = 0

    def update_progress_eta() -> None:
        nonlocal eta_samples, eta_avg_seconds_per_event, last_progress_update_at
        if pbar is None:
            return
        now = time.perf_counter()
        interval = max(0.0, now - last_progress_update_at)
        last_progress_update_at = now
        if interval <= 0:
            return

        eta_samples += 1
        # True running average over all processed events for stable ETA.
        eta_avg_seconds_per_event = (
            ((eta_avg_seconds_per_event * (eta_samples - 1)) + interval) / eta_samples
        )

        remaining = max(0, total_events - pbar.n)
        eta_seconds = remaining * eta_avg_seconds_per_event
        pbar.set_postfix_str(
            f"ETA~{format_duration(eta_seconds)} avg={eta_avg_seconds_per_event:.2f}s/event",
            refresh=False,
        )

    def current_eta_label() -> str:
        if eta_samples <= 0 or eta_avg_seconds_per_event <= 0:
            return "warming-up"
        remaining = max(0, total_events - (pbar.n if pbar is not None else 0))
        return format_duration(remaining * eta_avg_seconds_per_event)

    init_failures_writer(failures_file)
    init_stops_writer(stops_file)
    write_failures_csv(failures_file, storage.fetch_failures())
    write_stops_csv(stops_file, storage.fetch_stop_events())
    tournament_rows = []
    debater_rows = []
    unmatched_rows = []
    debater_profiles = []
    profile_count = 0
    identity_count = 0
    pbar = None
    stopped = False
    stop_reason = ""
    stop_unit_key = ""

    try:
        def skip_tournament(
            tournament_row: dict,
            status: str,
            reason: str,
            resolved_events_map: dict[str, dict] | None = None,
        ) -> None:
            nonlocal pending_progress_writes
            resolved_events_map = resolved_events_map or {}
            tourn_id = int(tournament_row["tourn_id"])
            for judge_event_label in tournament_row["judge_events"]:
                resolved_event = resolved_events_map.get(judge_event_label, {})
                event_id = resolved_event.get("event_id")
                matched_sidebar_label = resolved_event.get("matched_sidebar_label", "")
                dropdown_label = resolved_event.get("dropdown_label", "")
                storage.save_tournament_event(
                    {
                        "tourn_id": tourn_id,
                        "tournament_name": tournament_row["tournament_name"],
                        "judge_event_label": judge_event_label,
                        "matched_sidebar_label": matched_sidebar_label,
                        "dropdown_label": dropdown_label,
                        "event_id": event_id,
                        "status": status,
                    }
                )
                event_key = (tourn_id, judge_event_label)
                storage.mark_event_processed(
                    tourn_id=tourn_id,
                    judge_event_label=judge_event_label,
                    event_id=event_id,
                    status=status,
                    row_count=0,
                )
                progress_writer.writerow(
                    {
                        "tourn_id": tourn_id,
                        "tournament_name": tournament_row["tournament_name"],
                        "judge_event_label": judge_event_label,
                        "event_id": event_id or "",
                        "status": status,
                        "row_count": 0,
                        "stored_at": utc_now_iso(),
                    }
                )
                pending_progress_writes += 1
                flush_progress()
                if event_key not in processed_event_keys:
                    processed_event_keys.add(event_key)
                    pbar.update(1)
                    update_progress_eta()
            tqdm.write(
                f"{tourn_id} | {tournament_row['tournament_name']} | "
                f"skipped_tournament ({status}): {reason}"
            )

        pbar = tqdm(
            total=total_events,
            initial=initial_done,
            desc="Debater Events",
            unit="event",
            smoothing=0.9,
        )

        total_tournaments = len(tournaments)
        for tournament_idx, tournament in enumerate(tournaments, start=1):
            tournament_started_at = time.perf_counter()
            if stop_controller.stop_requested():
                stopped = True
                stop_reason = stop_controller.stop_reason()
                stop_unit_key = f"tourn_id={int(tournament['tourn_id'])}"
                break

            tournament_event_keys = {
                (int(tournament["tourn_id"]), judge_event_label)
                for judge_event_label in tournament["judge_events"]
            }
            if tournament_event_keys and tournament_event_keys.issubset(processed_event_keys):
                tqdm.write(
                    f"\n=== Tournament {tournament_idx}/{total_tournaments} | "
                    f"tourn_id={tournament['tourn_id']} | {tournament['tournament_name']} | "
                    f"events={len(tournament['judge_events'])} | ETA~{current_eta_label()} ==="
                )
                tqdm.write("  - already processed/skipped in prior run; skipping network fetch")
                continue

            tqdm.write(
                f"\n=== Tournament {tournament_idx}/{total_tournaments} | "
                f"tourn_id={tournament['tourn_id']} | {tournament['tournament_name']} | "
                f"events={len(tournament['judge_events'])} | ETA~{current_eta_label()} ==="
            )

            try:
                resolved_events, unresolved = resolve_event_ids_for_tournament(
                    browser=browser,
                    base_url=base_url,
                    tournament=tournament,
                    max_retries=max_retries,
                    retry_backoff=retry_backoff,
                    sleep_min=sleep_min,
                    sleep_max=sleep_max,
                )
            except Exception as exc:
                skip_tournament(
                    tournament_row=tournament,
                    status="skipped_tournament_missing_results",
                    reason=str(exc),
                )
                elapsed = time.perf_counter() - tournament_started_at
                extra_sleep = max(0.0, 1.0 - elapsed)
                if extra_sleep > 0:
                    time.sleep(extra_sleep)
                continue

            for unresolved_row in unresolved:
                storage.save_tournament_event(unresolved_row)
                event_key = (int(unresolved_row["tourn_id"]), unresolved_row["judge_event_label"])
                if event_key in processed_event_keys:
                    continue

                if stop_controller.stop_requested():
                    stopped = True
                    stop_reason = stop_controller.stop_reason()
                    stop_unit_key = f"tourn_id={event_key[0]}|event={event_key[1]}"
                    break

                stored_at = utc_now_iso()
                storage.mark_event_processed(
                    tourn_id=event_key[0],
                    judge_event_label=event_key[1],
                    event_id=None,
                    status="unmatched",
                    row_count=0,
                )
                progress_writer.writerow(
                    {
                        "tourn_id": event_key[0],
                        "tournament_name": unresolved_row.get("tournament_name", ""),
                        "judge_event_label": event_key[1],
                        "event_id": "",
                        "status": "unmatched",
                        "row_count": 0,
                        "stored_at": stored_at,
                    }
                )
                pending_progress_writes += 1
                flush_progress()
                processed_event_keys.add(event_key)
                pbar.update(1)
                update_progress_eta()
                tqdm.write(
                    f"  - {event_key[1]} | collected 0 (unmatched)"
                )
            if stopped:
                break

            for judge_event_label, resolved_event in sorted(resolved_events.items()):
                storage.save_tournament_event(
                    {
                        "tourn_id": tournament["tourn_id"],
                        "tournament_name": tournament["tournament_name"],
                        "judge_event_label": judge_event_label,
                        "matched_sidebar_label": resolved_event["matched_sidebar_label"],
                        "dropdown_label": resolved_event["dropdown_label"],
                        "event_id": resolved_event["event_id"],
                        "status": "matched",
                    }
                )

            skip_current_tournament = False
            for judge_event_label, resolved_event in resolved_events.items():
                event_key = (int(tournament["tourn_id"]), judge_event_label)
                if event_key in processed_event_keys:
                    continue

                if stop_controller.stop_requested():
                    stopped = True
                    stop_reason = stop_controller.stop_reason()
                    stop_unit_key = f"tourn_id={event_key[0]}|event={event_key[1]}"
                    break

                try:
                    event_rows = scrape_entries_for_event(
                        browser=browser,
                        base_url=base_url,
                        tourn_id=tournament["tourn_id"],
                        tournament_name=tournament["tournament_name"],
                        judge_event_label=judge_event_label,
                        resolved_event=resolved_event,
                        max_retries=max_retries,
                        retry_backoff=retry_backoff,
                        sleep_min=sleep_min,
                        sleep_max=sleep_max,
                    )
                except Exception as exc:
                    skip_tournament(
                        tournament_row=tournament,
                        status="skipped_tournament_missing_entries",
                        reason=str(exc),
                        resolved_events_map=resolved_events,
                    )
                    # Skip entire tournament as soon as one required entries page is missing.
                    elapsed = time.perf_counter() - tournament_started_at
                    extra_sleep = max(0.0, 1.0 - elapsed)
                    if extra_sleep > 0:
                        time.sleep(extra_sleep)
                    skip_current_tournament = True
                    break

                storage.save_debater_rows(event_rows)
                status = "completed" if event_rows else "completed_empty"
                resolved_count = storage.mark_failure_resolved(
                    tourn_id=tournament["tourn_id"],
                    judge_event_label=judge_event_label,
                    event_id=resolved_event.get("event_id"),
                )
                if resolved_count > 0:
                    write_failures_csv(failures_file, storage.fetch_failures())
                storage.mark_event_processed(
                    tourn_id=tournament["tourn_id"],
                    judge_event_label=judge_event_label,
                    event_id=resolved_event.get("event_id"),
                    status=status,
                    row_count=len(event_rows),
                )
                progress_writer.writerow(
                    {
                        "tourn_id": tournament["tourn_id"],
                        "tournament_name": tournament["tournament_name"],
                        "judge_event_label": judge_event_label,
                        "event_id": resolved_event.get("event_id") or "",
                        "status": status,
                        "row_count": len(event_rows),
                        "stored_at": utc_now_iso(),
                    }
                )
                pending_progress_writes += 1
                flush_progress()
                processed_event_keys.add(event_key)
                pbar.update(1)
                update_progress_eta()
                tqdm.write(
                    f"  - {judge_event_label} | collected {len(event_rows)}"
                )
            if stopped:
                break
            if skip_current_tournament:
                continue

            elapsed = time.perf_counter() - tournament_started_at
            extra_sleep = max(0.0, 1.0 - elapsed)
            if extra_sleep > 0:
                time.sleep(extra_sleep)

        tournament_rows = storage.fetch_tournament_rows()
        debater_rows = storage.fetch_debater_rows()
        unmatched_rows = [row for row in tournament_rows if row.get("status") == "unmatched"]
        identity_count = storage.rebuild_identity_ids_only()
        profile_count = storage.rebuild_debater_profiles()
        debater_profiles = storage.fetch_debater_profiles()

        if stopped:
            storage.log_stop_event(
                phase="debater",
                unit_key=stop_unit_key,
                reason=stop_reason,
                requested_at=stop_controller.requested_at() or utc_now_iso(),
                stopped_at=utc_now_iso(),
            )
            write_stops_csv(stops_file, storage.fetch_stop_events())
            print(f"Debater scraping stopped gracefully ({stop_reason}) at {stop_unit_key}")
    finally:
        if pbar is not None:
            pbar.close()
        flush_progress(force=True)
        progress_handle.close()
        stop_controller.close()
        storage.close()

    tournament_csv = write_tournament_ids_csv(debaters_dir, tournament_rows)
    output_paths = write_debater_output_files(debaters_dir, debater_rows, unmatched_rows, debater_profiles)
    unique_ids = sorted({row["debater_id"] for row in debater_rows})

    print(f"Extracted {len(debater_rows)} debater-id rows (from DB)")
    print(f"Unique debater IDs: {len(unique_ids)}")
    print(f"Debater identity rows (ids only): {identity_count}")
    print(f"Debater profile rows: {profile_count}")
    print(f"Tournament/event CSV: {tournament_csv}")
    for unique_row in sorted(
        {
            (
                row["debater_id"],
                row["team_code"],
                row["tournament_name"],
            )
            for row in debater_rows
        }
    ):
        print(f"{unique_row[0]} | {unique_row[1]} | {unique_row[2]}")

    return {
        "tournament_count": len(tournaments),
        "debater_row_count": len(debater_rows),
        "unique_debater_count": len(unique_ids),
        "tournament_ids_csv": tournament_csv,
        "debater_database_path": debater_database_path,
        "progress_file": progress_file,
        "failures_file": failures_file,
        "stops_file": stops_file,
        "stopped": stopped,
        "stop_reason": stop_reason,
        "stop_unit_key": stop_unit_key,
        **output_paths,
    }
