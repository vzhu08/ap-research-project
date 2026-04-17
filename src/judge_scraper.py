import csv
import io
import os
import random
import re
import time
from urllib.parse import urljoin

import mechanicalsoup
import pandas as pd
from bs4 import BeautifulSoup
from tqdm import tqdm

from src.storage import SQLiteJudgeStorage


def init_output_dirs(output_dir):
    os.makedirs(output_dir, exist_ok=True)
    parad_dir = os.path.join(output_dir, "paradigms")
    rec_dir = os.path.join(output_dir, "records")
    os.makedirs(parad_dir, exist_ok=True)
    os.makedirs(rec_dir, exist_ok=True)
    return parad_dir, rec_dir


def default_database_path(output_dir: str) -> str:
    return os.path.join(output_dir, "judge_data.sqlite3")


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
    write_header = not os.path.exists(failures_file)
    handle = open(failures_file, "a", newline="", encoding="utf-8-sig")
    writer = csv.DictWriter(handle, fieldnames=["judge_person_id", "error"])
    if write_header:
        writer.writeheader()
    return handle, writer


def load_judge_ids(id_csv, process_limit, processed_ids):
    if not os.path.exists(id_csv):
        raise RuntimeError(f"ID file '{id_csv}' not found.")
    df = pd.read_csv(id_csv, header=None, dtype=str)
    all_ids = [int(x) for x in df[0].tolist()]
    if process_limit:
        all_ids = all_ids[:process_limit]
    return [jid for jid in all_ids if jid not in processed_ids]


def login(username, password, login_url):
    browser = mechanicalsoup.StatefulBrowser(soup_config={"features": "lxml"})
    browser.open(login_url)
    browser.select_form('form[action*="login"]')
    browser["username"] = username
    browser["password"] = password
    resp = browser.submit_selected()
    if "Please login" in resp.text:
        raise RuntimeError("Login failed - check your credentials")
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

    return parsed["judge_name"], parad_file, rec_file


def init_progress_writer(progress_file, write_header):
    handle = open(progress_file, "a", newline="", encoding="utf-8-sig")
    writer = csv.DictWriter(
        handle,
        fieldnames=["judge_person_id", "judge_name", "paradigm_file", "record_file"],
    )
    if write_header:
        writer.writeheader()
    return handle, writer


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
):
    login_url = f"{base_url}/user/login/login.mhtml"
    paradigm_url = f"{base_url}/index/paradigm.mhtml"
    database_path = database_path or default_database_path(output_dir)

    parad_dir, rec_dir = init_output_dirs(output_dir)
    storage = SQLiteJudgeStorage(database_path)

    try:
        processed_ids, write_header = load_processed_ids(progress_file, storage)
        fail_handle, fail_writer = init_failures_log(failures_file)

        df_ids = pd.read_csv(id_csv, header=None, dtype=str)
        all_ids = [int(x) for x in df_ids[0].tolist()]
        if process_limit:
            all_ids = all_ids[:process_limit]
        total_all = len(all_ids)

        judge_ids = [jid for jid in all_ids if jid not in processed_ids]
        remaining = len(judge_ids)
        initial_done = total_all - remaining

        avg_sleep = (sleep_min + sleep_max) / 2
        eta_sec = remaining * (avg_sleep + 0.5)
        hrs = int(eta_sec // 3600)
        mins = int((eta_sec % 3600) // 60)
        secs = int(eta_sec % 60)

        print(f"Processing {remaining} remaining of {total_all} judges (limit={process_limit})")
        print(f"Estimated total time for remaining: {hrs}h {mins}m {secs}s")
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

        for pid in pbar:
            idx = pbar.n
            start_time = time.time()
            try:
                name, pfile, rfile = fetch_judge_data(
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
                        "paradigm_file": pfile,
                        "record_file": rfile,
                    }
                )
                prog_handle.flush()

                p_yes = "yes" if pfile else "db-only" if write_legacy_files else "db"
                r_yes = "yes" if rfile else "db-only" if write_legacy_files else "db"
                elapsed = time.time() - start_time
                tqdm.write(
                    f"[{idx}/{total_all}] ID={pid}: {name} | "
                    f"paradigm={p_yes} | record={r_yes} | time={elapsed:.1f}s"
                )
            except Exception as exc:
                elapsed = time.time() - start_time
                storage.log_failure(pid, str(exc))
                fail_writer.writerow({"judge_person_id": pid, "error": str(exc)})
                fail_handle.flush()
                tqdm.write(
                    f"[{idx}/{total_all}] ID={pid} error: {exc} | time={elapsed:.1f}s"
                )

            time.sleep(random.uniform(sleep_min, sleep_max))

        prog_handle.close()
        fail_handle.close()
        print("All done. Progress, failures, and judge data saved.")
    finally:
        storage.close()
