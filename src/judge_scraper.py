# src/scraper.py

import os
import re
import time
import random
import csv
import io

import mechanicalsoup
import pandas as pd
from bs4 import BeautifulSoup
from tqdm import tqdm


def init_output_dirs(output_dir):
    os.makedirs(output_dir, exist_ok=True)
    parad_dir = os.path.join(output_dir, "paradigms")
    rec_dir = os.path.join(output_dir, "records")
    os.makedirs(parad_dir, exist_ok=True)
    os.makedirs(rec_dir, exist_ok=True)
    return parad_dir, rec_dir


def load_processed_ids(progress_file):
    processed = set()
    write_header = True
    if os.path.exists(progress_file):
        with open(progress_file, 'r', encoding='utf-8-sig') as pf:
            reader = csv.DictReader(pf)
            for row in reader:
                try:
                    processed.add(int(row['judge_person_id']))
                except:
                    pass
        write_header = False
    return processed, write_header


def init_failures_log(failures_file):
    write_header = not os.path.exists(failures_file)
    f = open(failures_file, 'a', newline='', encoding='utf-8-sig')
    w = csv.DictWriter(f, fieldnames=['judge_person_id', 'error'])
    if write_header:
        w.writeheader()
    return f, w


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
    browser['username'] = username
    browser['password'] = password
    resp = browser.submit_selected()
    if 'Please login' in resp.text:
        raise RuntimeError('Login failed — check your credentials')
    return browser


def fetch_judge_data(pid, browser, paradigm_url, parad_dir, rec_dir, max_retries, retry_backoff):
    resp = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = browser.session.get(f"{paradigm_url}?judge_person_id={pid}")
            break
        except Exception:
            if attempt == max_retries:
                raise
            time.sleep(retry_backoff)

    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")

    page = BeautifulSoup(resp.text, 'lxml')
    name_tag = page.select_one('div.main h3')
    judge_name = name_tag.get_text(strip=True) if name_tag else f"ID_{pid}"
    safe_name = re.sub(r"[^A-Za-z0-9]+", "", judge_name)

    # save paradigm HTML
    parad_file = ""
    div = page.find('div', class_='paradigm ltborderbottom')
    if div:
        parad_file = os.path.join(parad_dir, f"{pid}_{safe_name}.html")
        with open(parad_file, 'w', encoding='utf-8') as f:
            f.write(div.decode_contents())

    # save record CSV
    rec_file = ""
    tables = pd.read_html(io.StringIO(resp.text), attrs={'id': 'judgerecord'})
    if tables:
        df_rec = tables[0]
        rec_file = os.path.join(rec_dir, f"{pid}_{safe_name}.csv")
        df_rec.to_csv(rec_file, index=False, encoding='utf-8-sig')

    return judge_name, parad_file, rec_file


def init_progress_writer(progress_file, write_header):
    f = open(progress_file, 'a', newline='', encoding='utf-8-sig')
    w = csv.DictWriter(f, fieldnames=[
        'judge_person_id', 'judge_name', 'paradigm_file', 'record_file'
    ])
    if write_header:
        w.writeheader()
    return f, w


def scrape_judges(
    username, password,
    base_url, id_csv, process_limit,
    sleep_min, sleep_max,
    output_dir,
    progress_file, failures_file,
    disable_progress, smoothing,
    max_retries, retry_backoff
):
    login_url    = f"{base_url}/user/login/login.mhtml"
    paradigm_url = f"{base_url}/index/paradigm.mhtml"

    parad_dir, rec_dir = init_output_dirs(output_dir)
    processed_ids, write_header = load_processed_ids(progress_file)
    fail_f, fail_writer       = init_failures_log(failures_file)

    # ─── Load ALL IDs for full progress bar ────────────────────────────────
    df_ids    = pd.read_csv(id_csv, header=None, dtype=str)
    all_ids   = [int(x) for x in df_ids[0].tolist()]
    if process_limit:
        all_ids = all_ids[:process_limit]
    total_all    = len(all_ids)

    # Filter to only those not yet processed
    judge_ids    = [jid for jid in all_ids if jid not in processed_ids]
    remaining    = len(judge_ids)
    initial_done = total_all - remaining

    # Estimate ETA for remaining
    avg_sleep = (sleep_min + sleep_max) / 2
    eta_sec   = remaining * (avg_sleep + 0.5)
    hrs       = int(eta_sec // 3600)
    mins      = int((eta_sec % 3600) // 60)
    secs      = int(eta_sec % 60)

    print(f"Processing {remaining} remaining of {total_all} judges (limit={process_limit})")
    print(f"Estimated total time for remaining: {hrs}h {mins}m {secs}s")

    browser = login(username, password, login_url)
    print("✅ Logged in successfully")

    prog_f, prog_writer = init_progress_writer(progress_file, write_header)

    # ─── Use a tqdm bar spanning the full set, starting at initial_done ─────────
    pbar = tqdm(
        judge_ids,
        total=total_all,
        initial=initial_done,
        desc="Judges",
        unit="judge",
        disable=disable_progress,
        smoothing=smoothing
    )

    for pid in pbar:
        idx = pbar.n  # current overall count
        start_time = time.time()
        try:
            name, pfile, rfile = fetch_judge_data(
                pid, browser, paradigm_url,
                parad_dir, rec_dir,
                max_retries, retry_backoff
            )
            # Log progress
            prog_writer.writerow({
                'judge_person_id': pid,
                'judge_name':       name,
                'paradigm_file':    pfile,
                'record_file':      rfile
            })
            prog_f.flush()

            p_yes  = 'yes' if pfile else 'no'
            r_yes  = 'yes' if rfile else 'no'
            elapsed = time.time() - start_time
            tqdm.write(
                f"[{idx}/{total_all}] ID={pid}: {name} | "
                f"paradigm={p_yes} | record={r_yes} | time={elapsed:.1f}s"
            )

        except Exception as e:
            elapsed = time.time() - start_time
            fail_writer.writerow({'judge_person_id': pid, 'error': str(e)})
            fail_f.flush()
            tqdm.write(
                f"[{idx}/{total_all}] ID={pid} error: {e} | time={elapsed:.1f}s"
            )

        time.sleep(random.uniform(sleep_min, sleep_max))

    prog_f.close()
    fail_f.close()
    print("✅ All done. Progress & failures saved.")
