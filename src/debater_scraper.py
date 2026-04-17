import csv
import os
import random
import sqlite3
import time
from contextlib import closing
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from src.judge_scraper import login, normalized_text


TEAM_RESULTS_PATH = "/index/results/team_results.mhtml"
FIELDS_PATH = "/index/tourn/fields.mhtml"
RESULTS_INDEX_PATH = "/index/tourn/results/index.mhtml"


def init_debater_output_dir(output_dir: str) -> str:
    debaters_dir = os.path.join(output_dir, "debaters")
    os.makedirs(debaters_dir, exist_ok=True)
    return debaters_dir


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
            time.sleep(retry_backoff)

    if response is None or response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code if response is not None else 'unknown'} for {url}")

    return response


def sleep_with_jitter(sleep_min: float, sleep_max: float) -> None:
    time.sleep(random.uniform(sleep_min, sleep_max))


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

    for option in event_options:
        event_id = option["event_id"]
        results_url = build_results_page_url(base_url, tourn_id, event_id=event_id)
        results_page = BeautifulSoup(
            fetch_with_retries(browser, results_url, max_retries, retry_backoff).text,
            "lxml",
        )

        selected_event_id = parse_selected_event_id(results_page)
        sidebar_label = parse_results_sidebar_label(results_page)

        if not sidebar_label:
            sleep_with_jitter(sleep_min, sleep_max)
            continue

        if sidebar_label in tournament["judge_events"] and sidebar_label not in matched:
            matched[sidebar_label] = {
                "event_id": selected_event_id or event_id,
                "matched_sidebar_label": sidebar_label,
                "dropdown_label": option["dropdown_label"],
                "source_url": results_url,
            }
            print(
                f"{tourn_id} | {option['dropdown_label']} dropdown -> "
                f"{sidebar_label} sidebar -> matched Ev={sidebar_label}"
            )

        sleep_with_jitter(sleep_min, sleep_max)

    for judge_event in tournament["judge_events"]:
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
    return [
        f"{base}?{urlencode({'tourn_id': tourn_id, 'event_id': event_id})}",
        f"{base}?{urlencode({'event_id': event_id, 'tourn_id': tourn_id})}",
        f"{base}?{urlencode({'tourn_id': tourn_id})}&event_id={event_id}",
    ]


def extract_debater_ids_from_link(url: str) -> list[str]:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    ids = []
    for key in ("id1", "id2", "id"):
        for value in query.get(key, []):
            if value and value.isdigit():
                ids.append(value)

    if ids:
        return ids

    # Fallback for unexpected patterns that still include numeric ids in query keys.
    for values in query.values():
        for value in values:
            if value and value.isdigit():
                ids.append(value)

    return ids


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

    for anchor in page.find_all("a", href=True):
        href = anchor.get("href", "").strip()
        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)

        if "results" not in parsed.path:
            continue

        if "id1=" not in full_url and "id2=" not in full_url and "id=" not in full_url:
            continue

        debater_ids = extract_debater_ids_from_link(full_url)
        if not debater_ids:
            continue

        team_code = normalized_text(anchor.get_text(" ", strip=True))

        for debater_id in debater_ids:
            key = (debater_id, team_code, full_url)
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
                    "source_url": full_url,
                }
            )

    return rows


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
    for url in build_entries_page_urls(base_url, tourn_id, resolved_event["event_id"]):
        try:
            page = BeautifulSoup(
                fetch_with_retries(browser, url, max_retries, retry_backoff).text,
                "lxml",
            )
            rows = parse_entries_page_for_debater_rows(
                page,
                base_url=base_url,
                tourn_id=tourn_id,
                tournament_name=tournament_name,
                judge_event_label=judge_event_label,
                matched_sidebar_label=resolved_event["matched_sidebar_label"],
                event_id=resolved_event["event_id"],
            )
            sleep_with_jitter(sleep_min, sleep_max)
            if rows:
                return rows
        except Exception as exc:
            last_error = exc

    if last_error is not None:
        raise last_error

    return []


def write_csv(path: str, fieldnames: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_debater_output_files(debaters_dir: str, debater_rows: list[dict], unmatched_rows: list[dict]) -> dict:
    debater_ids_path = os.path.join(debaters_dir, "debater_ids.csv")
    unique_ids_path = os.path.join(debaters_dir, "debater_ids_unique.csv")
    unmatched_path = os.path.join(debaters_dir, "unmatched_events.csv")

    write_csv(
        debater_ids_path,
        [
            "debater_id",
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
                "team_code": row["team_code"],
                "tourn_id": row["tourn_id"],
                "tournament_name": row["tournament_name"],
            }
        )
    write_csv(unique_ids_path, ["debater_id", "team_code", "tourn_id", "tournament_name"], unique_rows)

    write_csv(
        unmatched_path,
        ["tourn_id", "tournament_name", "judge_event_label", "matched_sidebar_label", "dropdown_label", "event_id", "status"],
        unmatched_rows,
    )

    return {
        "debater_ids_csv": debater_ids_path,
        "debater_ids_unique_csv": unique_ids_path,
        "unmatched_events_csv": unmatched_path,
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
) -> dict:
    tournaments = load_tournaments_from_judge_db(database_path)
    debaters_dir = init_debater_output_dir(output_dir)
    print(f"Found {len(tournaments)} unique tournaments")

    browser = login(username, password, f"{base_url}/user/login/login.mhtml")
    print("Logged in successfully")

    debater_rows = []
    unmatched_rows = []
    tournament_rows = []

    for index, tournament in enumerate(tournaments, start=1):
        print(
            f"[{index}/{len(tournaments)}] Tournament {tournament['tourn_id']} "
            f"| {tournament['tournament_name']} | judge events={len(tournament['judge_events'])}"
        )

        resolved_events, unresolved = resolve_event_ids_for_tournament(
            browser=browser,
            base_url=base_url,
            tournament=tournament,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
            sleep_min=sleep_min,
            sleep_max=sleep_max,
        )
        unmatched_rows.extend(unresolved)
        tournament_rows.extend(unresolved)

        for judge_event_label, resolved_event in sorted(resolved_events.items()):
            tournament_rows.append(
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

        for judge_event_label, resolved_event in resolved_events.items():
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
                print(
                    f"{tournament['tourn_id']} | {judge_event_label} | entries error: {exc}"
                )
                continue

            for row in event_rows:
                debater_rows.append(row)
                print(f"{row['debater_id']} | {row['team_code']} | {row['tournament_name']}")

    tournament_csv = write_tournament_ids_csv(debaters_dir, tournament_rows)
    output_paths = write_debater_output_files(debaters_dir, debater_rows, unmatched_rows)
    unique_ids = sorted({row["debater_id"] for row in debater_rows})

    print(f"Extracted {len(debater_rows)} debater-id rows")
    print(f"Unique debater IDs: {len(unique_ids)}")
    print(f"Tournament/event CSV: {tournament_csv}")
    for unique_row in sorted(
        {(
            row["debater_id"],
            row["team_code"],
            row["tournament_name"],
        ) for row in debater_rows}
    ):
        print(f"{unique_row[0]} | {unique_row[1]} | {unique_row[2]}")

    return {
        "tournament_count": len(tournaments),
        "debater_row_count": len(debater_rows),
        "unique_debater_count": len(unique_ids),
        "tournament_ids_csv": tournament_csv,
        **output_paths,
    }
