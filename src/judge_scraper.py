import csv
import io
import os
import random
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

import mechanicalsoup
import pandas as pd
from bs4 import BeautifulSoup
from tqdm import tqdm

from src.stop_controller import StopController
from src.storage import SQLiteJudgeStorage


def init_output_dirs(output_dir):
    os.makedirs(output_dir, exist_ok=True)
    parad_dir = os.path.join(output_dir, "paradigms")
    rec_dir = os.path.join(output_dir, "records")
    os.makedirs(parad_dir, exist_ok=True)
    os.makedirs(rec_dir, exist_ok=True)
    return parad_dir, rec_dir


def default_database_path(output_dir: str) -> str:
    return os.path.join(output_dir, "judges", "judge_data.sqlite3")


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


def load_processed_ids(progress_file, storage):
    processed = storage.load_processed_ids()
    write_header = True

    if processed:
        write_header = not os.path.exists(progress_file)
        return processed, write_header

    if os.path.exists(progress_file):
        with open(progress_file, "r", encoding="utf-8-sig") as progress_handle:
            reader = csv.DictReader(progress_handle)
            for row in reader:
                try:
                    processed.add(int(row["judge_person_id"]))
                except Exception:
                    pass
        write_header = False

    return processed, write_header


def init_failures_log(failures_file):
    parent = os.path.dirname(failures_file)
    if parent:
        os.makedirs(parent, exist_ok=True)


def init_stops_log(stops_file):
    parent = os.path.dirname(stops_file)
    if parent:
        os.makedirs(parent, exist_ok=True)


def write_failures_csv(failures_file: str, failure_rows: list[dict]) -> None:
    with open(failures_file, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "id",
                "judge_person_id",
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
                    "judge_person_id": row.get("judge_person_id", ""),
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


def load_judge_ids(id_csv, process_limit, processed_ids):
    if not os.path.exists(id_csv):
        raise RuntimeError(f"ID file '{id_csv}' not found.")
    df = pd.read_csv(id_csv, header=None, dtype=str)
    all_ids = [int(x) for x in df[0].tolist()]
    if process_limit:
        all_ids = all_ids[:process_limit]
    return [jid for jid in all_ids if jid not in processed_ids]


def extract_login_form_payload(form) -> dict[str, str]:
    payload = {}

    for field in form.select("input[name], textarea[name], select[name]"):
        name = field.get("name")
        if not name:
            continue

        if field.name == "select":
            selected = field.select_one("option[selected]") or field.select_one("option")
            payload[name] = selected.get("value", "") if selected else ""
            continue

        input_type = (field.get("type") or "").lower()
        if input_type in {"submit", "button", "image", "file"}:
            continue

        payload[name] = field.get("value", "")

    return payload


def sanitize_credential(value: str | None) -> str:
    cleaned = (value or "").strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
        cleaned = cleaned[1:-1].strip()
    return cleaned


def choose_login_form(page):
    forms = page.find_all("form")

    for form in forms:
        if form.find("input", {"name": "key"}) and form.find("input", {"name": "username"}) and form.find(
            "input", {"name": "password"}
        ):
            return form

    for form in forms:
        if form.find("input", {"id": "login_email"}) and form.find("input", {"name": "password"}):
            return form

    for form in forms:
        if form.find("input", {"name": "username"}) and form.find("input", {"name": "password"}):
            return form

    return None


def is_login_gate_response(response_text: str, response_url: str) -> bool:
    return "/user/login/login.mhtml" in (response_url or "") or "Please login" in response_text


def login(username, password, login_url):
    username = sanitize_credential(username)
    password = sanitize_credential(password)

    if not username or not password:
        raise RuntimeError("Missing Tabroom credentials. Set DEBATE_EMAIL_USER and DEBATE_EMAIL_PASS.")

    browser = mechanicalsoup.StatefulBrowser(soup_config={"features": "lxml"})
    browser.open(login_url)

    page = browser.page
    login_form = choose_login_form(page)
    if login_form is None:
        raise RuntimeError("Could not find the Tabroom login form on the login page.")

    payload = extract_login_form_payload(login_form)
    payload["username"] = username
    payload["password"] = password

    action_url = urljoin(login_url, login_form.get("action", ""))
    browser.session.post(
        action_url,
        data=payload,
        headers={"Referer": login_url},
        allow_redirects=True,
    )

    # Verify using a protected page the scraper actually needs.
    verify_url = urljoin(login_url, "/index/paradigm.mhtml?judge_person_id=1")
    verification = browser.session.get(verify_url, allow_redirects=True)
    if is_login_gate_response(verification.text, getattr(verification, "url", "")):
        final_url = getattr(verification, "url", "")
        raise RuntimeError(
            "Login failed - Tabroom returned a login gate on the protected paradigm page. "
            f"Final URL: {final_url}"
        )

    return browser


def normalized_text(value: str) -> str:
    return " ".join(value.split())


def extract_visible_cell_text(cell) -> str:
    clone = BeautifulSoup(str(cell), "lxml")
    for hidden in clone.select(".hidden, .hiddencsv, .hide_fromcsv, .hidden_fromcsv"):
        hidden.decompose()
    return normalized_text(clone.get_text(" ", strip=True))


def extract_links(cell, base_url: str) -> list[dict]:
    links = []
    for anchor in cell.find_all("a", href=True):
        href = anchor.get("href", "").strip()
        links.append(
            {
                "text": normalized_text(anchor.get_text(" ", strip=True)),
                "href": urljoin(base_url, href),
                "href_raw": href,
                "target": anchor.get("target"),
                "title": anchor.get("title"),
            }
        )
    return links


def build_results_html(page: BeautifulSoup) -> str:
    table = page.select_one("table#judgerecord")
    if not table:
        return ""

    fragments = []
    heading = page.find(lambda tag: tag.name in {"h4", "h5"} and "Full Judging Record" in tag.get_text(" ", strip=True))
    if heading and heading.parent:
        fragments.append(str(heading.parent))

    button_area = page.select_one("#judgerecord_buttonarea")
    if button_area:
        fragments.append(str(button_area))

    resizable_container = page.select_one("div.tablesorter-resizable-container")
    if resizable_container:
        fragments.append(str(resizable_container))

    fragments.append(str(table))
    return "\n".join(fragments)


def parse_record_table(page: BeautifulSoup, base_url: str) -> tuple[pd.DataFrame, str, list[dict], list[dict], str]:
    table = page.select_one("table#judgerecord")
    if not table:
        return pd.DataFrame(), "", [], [], ""

    headers = [normalized_text(th.get_text(" ", strip=True)) for th in table.select("thead th")]
    rows = []
    results_links = []

    for row_index, row in enumerate(table.select("tbody tr")):
        cells = row.find_all("td")
        if not cells:
            continue

        parsed = {"row_index": row_index}
        all_row_links = []

        for header, cell in zip(headers, cells):
            parsed[header] = extract_visible_cell_text(cell)

            hidden_value = ""
            hidden_tag = cell.select_one(".hidden")
            if hidden_tag:
                hidden_value = normalized_text(hidden_tag.get_text(" ", strip=True))

            links = extract_links(cell, base_url)
            parsed[f"{header}_links"] = links

            if links:
                parsed[f"{header}_url"] = links[0]["href"]
                for link in links:
                    all_row_links.append({"column": header, **link})
            else:
                parsed[f"{header}_url"] = ""

            if header == "Date":
                parsed["_date_sort_key"] = hidden_value
            elif header == "Rd":
                parsed["_round_sort_key"] = hidden_value

        parsed["_all_links"] = all_row_links
        rows.append(parsed)
        results_links.extend(all_row_links)

    if not rows:
        return pd.DataFrame(), "", [], [], build_results_html(page)

    export_columns = [header for header in headers if header in rows[0]]
    df = pd.DataFrame([{column: row.get(column, "") for column in export_columns} for row in rows])
    csv_buffer = io.StringIO()
    df.to_csv(csv_buffer, index=False)

    return df, csv_buffer.getvalue(), rows, results_links, build_results_html(page)


def parse_judge_page(html_text: str, source_url: str, base_url: str) -> dict:
    page = BeautifulSoup(html_text, "lxml")
    name_tag = page.select_one("div.main h3")
    judge_name = normalized_text(name_tag.get_text(strip=True)) if name_tag else ""
    judge_name = judge_name or source_url.rsplit("=", 1)[-1]
    safe_name = re.sub(r"[^A-Za-z0-9]+", "", judge_name) or f"Judge{source_url.rsplit('=', 1)[-1]}"

    paradigm_html = ""
    paradigm_div = page.find("div", class_="paradigm ltborderbottom")
    if paradigm_div:
        paradigm_html = paradigm_div.decode_contents()

    record_df, record_csv, record_rows, results_links, results_html = parse_record_table(page, base_url)

    return {
        "judge_name": judge_name,
        "safe_name": safe_name,
        "paradigm_html": paradigm_html,
        "results_html": results_html,
        "record_df": record_df,
        "record_csv": record_csv,
        "record_rows": record_rows,
        "results_links": results_links,
        "raw_page_html": html_text,
        "source_url": source_url,
    }


def ensure_page_contains_judge_content(parsed: dict, source_url: str) -> None:
    has_record = bool(parsed["record_rows"])
    has_paradigm = bool(parsed["paradigm_html"])

    if has_record or has_paradigm:
        return

    raise RuntimeError(f"No paradigm or judging record content found for {source_url}")


def fetch_judge_data(
    pid,
    browser,
    paradigm_url,
    base_url,
    storage,
    parad_dir,
    rec_dir,
    max_retries,
    retry_backoff,
    write_legacy_files=True,
):
    resp = None
    source_url = f"{paradigm_url}?judge_person_id={pid}"

    for attempt in range(1, max_retries + 1):
        try:
            resp = browser.session.get(source_url)
            break
        except Exception:
            if attempt == max_retries:
                raise
            time.sleep(retry_backoff)

    if resp is None or resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code if resp is not None else 'unknown'}")

    parsed = parse_judge_page(resp.text, source_url, base_url)
    ensure_page_contains_judge_content(parsed, source_url)

    storage.save_judge_page(
        judge_person_id=pid,
        judge_name=parsed["judge_name"],
        safe_name=parsed["safe_name"],
        source_url=parsed["source_url"],
        raw_page_html=parsed["raw_page_html"],
        paradigm_html=parsed["paradigm_html"],
        results_html=parsed["results_html"],
        record_csv=parsed["record_csv"],
        record_rows=parsed["record_rows"],
        results_links=parsed["results_links"],
    )

    parad_file = ""
    rec_file = ""

    if write_legacy_files:
        if parsed["paradigm_html"]:
            parad_file = os.path.join(parad_dir, f"{pid}_{parsed['safe_name']}.html")
            with open(parad_file, "w", encoding="utf-8") as handle:
                handle.write(parsed["paradigm_html"])

        if not parsed["record_df"].empty:
            rec_file = os.path.join(rec_dir, f"{pid}_{parsed['safe_name']}.csv")
            parsed["record_df"].to_csv(rec_file, index=False, encoding="utf-8-sig")

    return parsed["judge_name"], parad_file, rec_file, len(parsed["record_rows"])


def init_progress_writer(progress_file, write_header):
    handle = open(progress_file, "a", newline="", encoding="utf-8-sig")
    writer = csv.DictWriter(
        handle,
        fieldnames=["judge_person_id", "judge_name", "paradigm_file", "record_file"],
    )
    if write_header:
        writer.writeheader()
    return handle, writer


def progress_ref(file_path: str, write_legacy_files: bool) -> str:
    if file_path:
        return file_path
    if write_legacy_files:
        return "db-only"
    return "db"


def scrape_judges(
    username,
    password,
    base_url,
    id_csv,
    process_limit,
    sleep_min,
    sleep_max,
    output_dir,
    progress_file,
    failures_file,
    disable_progress,
    smoothing,
    max_retries,
    retry_backoff,
    database_path=None,
    write_legacy_files=True,
    stop_flag_path: str | None = None,
    enable_signal_stop: bool = True,
    stops_file: str | None = None,
):
    login_url = f"{base_url}/user/login/login.mhtml"
    paradigm_url = f"{base_url}/index/paradigm.mhtml"
    database_path = database_path or default_database_path(output_dir)
    stops_file = stops_file or os.path.join(output_dir, "judges", "stops.csv")

    parad_dir, rec_dir = init_output_dirs(output_dir)
    storage = SQLiteJudgeStorage(database_path)
    stop_controller = StopController(stop_flag_path=stop_flag_path, enable_signal_stop=enable_signal_stop)
    stopped = False
    stop_reason = ""
    stop_unit_key = ""
    prog_handle = None
    pbar = None

    try:
        processed_ids, write_header = load_processed_ids(progress_file, storage)
        init_failures_log(failures_file)
        init_stops_log(stops_file)
        write_failures_csv(failures_file, storage.fetch_failures())
        write_stops_csv(stops_file, storage.fetch_stop_events())

        df_ids = pd.read_csv(id_csv, header=None, dtype=str)
        all_ids = [int(x) for x in df_ids[0].tolist()]
        if process_limit:
            all_ids = all_ids[:process_limit]
        total_all = len(all_ids)

        judge_ids = [jid for jid in all_ids if jid not in processed_ids]
        remaining = len(judge_ids)
        initial_done = total_all - remaining

        print(f"Processing {remaining} remaining of {total_all} judges (limit={process_limit})")
        print("Estimated total time for remaining: warming up from live run timings...")
        print(f"SQLite database: {database_path}")

        browser = login(username, password, login_url)
        print("Logged in successfully")

        prog_handle, prog_writer = init_progress_writer(progress_file, write_header)

        pbar = tqdm(
            judge_ids,
            total=total_all,
            initial=initial_done,
            desc="Judges",
            unit="judge",
            disable=disable_progress,
            smoothing=smoothing,
        )

        eta_samples = 0
        eta_avg_seconds_per_judge = 0.0
        last_iteration_done_at = time.perf_counter()

        for pid in pbar:
            if stop_controller.stop_requested():
                stopped = True
                stop_reason = stop_controller.stop_reason()
                stop_unit_key = f"judge_person_id={pid}"
                break

            idx = pbar.n
            start_time = time.time()
            try:
                name, pfile, rfile, round_count = fetch_judge_data(
                    pid,
                    browser,
                    paradigm_url,
                    base_url,
                    storage,
                    parad_dir,
                    rec_dir,
                    max_retries,
                    retry_backoff,
                    write_legacy_files=write_legacy_files,
                )

                prog_writer.writerow(
                    {
                        "judge_person_id": pid,
                        "judge_name": name,
                        "paradigm_file": progress_ref(pfile, write_legacy_files),
                        "record_file": progress_ref(rfile, write_legacy_files),
                    }
                )
                prog_handle.flush()
                resolved_count = storage.mark_failure_resolved(pid)
                if resolved_count > 0:
                    write_failures_csv(failures_file, storage.fetch_failures())

                p_yes = "yes" if pfile else "db-only" if write_legacy_files else "db"
                r_yes = "yes" if rfile else "db-only" if write_legacy_files else "db"
                elapsed = time.time() - start_time
                tqdm.write(
                    f"[{idx}/{total_all}] ID={pid}: {name} | "
                    f"paradigm={p_yes} | record={r_yes} | rounds={round_count} | time={elapsed:.1f}s"
                )
            except Exception as exc:
                elapsed = time.time() - start_time
                storage.log_failure(pid, str(exc))
                write_failures_csv(failures_file, storage.fetch_failures())
                tqdm.write(
                    f"[{idx}/{total_all}] ID={pid} error: {exc} | time={elapsed:.1f}s"
                )

            time.sleep(random.uniform(sleep_min, sleep_max))

            now = time.perf_counter()
            interval = max(0.0, now - last_iteration_done_at)
            last_iteration_done_at = now
            if interval > 0:
                eta_samples += 1
                # True running average over all processed judges for stable ETA.
                eta_avg_seconds_per_judge = (
                    ((eta_avg_seconds_per_judge * (eta_samples - 1)) + interval) / eta_samples
                )

                remaining_judges = max(0, total_all - pbar.n)
                eta_seconds = remaining_judges * eta_avg_seconds_per_judge
                pbar.set_postfix_str(
                    f"ETA~{format_duration(eta_seconds)} avg={eta_avg_seconds_per_judge:.2f}s/judge",
                    refresh=False,
                )

        if stopped:
            requested_at = stop_controller.requested_at() or utc_now_iso()
            storage.log_stop_event(
                phase="judge",
                unit_key=stop_unit_key,
                reason=stop_reason,
                requested_at=requested_at,
                stopped_at=utc_now_iso(),
            )
            write_stops_csv(stops_file, storage.fetch_stop_events())
            print(f"Judge scraping stopped gracefully ({stop_reason}) at {stop_unit_key}")
        print("All done. Progress, failures, and judge data saved.")
        return {
            "stopped": stopped,
            "stop_reason": stop_reason,
            "stop_unit_key": stop_unit_key,
            "stops_file": stops_file,
            "progress_file": progress_file,
            "failures_file": failures_file,
            "database_path": database_path,
        }
    finally:
        if pbar is not None:
            pbar.close()
        if prog_handle is not None:
            prog_handle.close()
        stop_controller.close()
        storage.close()
