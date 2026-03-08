# main.py

from src.scraper import scrape_judges
from src.compiler import compile_judge_data, calculate_sit_rate_by_year_range
from src.plotter import plot_all_model_fits

import os

# ─── CONFIG ────────────────────────────────────────────────────────────────
USERNAME = os.environ.get("DEBATE_EMAIL_USER")
PASSWORD = os.environ.get("DEBATE_EMAIL_PASS")
BASE_URL        = "https://www.tabroom.com"
LOGIN_URL       = f"{BASE_URL}/user/login/login.mhtml"
PARA_URL        = f"{BASE_URL}/index/paradigm.mhtml"
ID_CSV          = "ids.csv"           # CSV with judge_person_id in first column
PROCESS_LIMIT   = None                 # set to an int to limit, or None for all
SLEEP_MIN       = 1                    # min delay (seconds)
SLEEP_MAX       = 5                    # max delay (seconds)
OUTPUT_DIR      = "output"  # base directory to save files
PROGRESS_FILE   = os.path.join(OUTPUT_DIR, "progress.csv")  # track completed IDs
FAILURES_FILE   = os.path.join(OUTPUT_DIR, "failures.csv")  # separate failures log
# Progress bar options
DISABLE_PROGRESS = False               # set True to disable tqdm bar
SMOOTHING        = 1.0                 # use global average for ETA
MAX_RETRIES      = 3                   # number of fetch retries on error
RETRY_BACKOFF    = 5                   # seconds to wait between retries
# ────────────────────────────────────────────────────────────────────────────

'''
scrape_judges(USERNAME, PASSWORD, BASE_URL, ID_CSV, PROCESS_LIMIT, SLEEP_MIN, SLEEP_MAX, OUTPUT_DIR,
              PROGRESS_FILE, FAILURES_FILE, DISABLE_PROGRESS, SMOOTHING, MAX_RETRIES, RETRY_BACKOFF)
'''

cx = {
    'exact': {"shir", "pel"},
    'fuzzy': {"cx", "pd", "pol"}
}

pf = {
    'exact': set(),
    'fuzzy': {"pf"}
}

ld = {
    'exact': set(),
    'fuzzy': {"ld"}
}

calculate_sit_rate_by_year_range(events=ld)

'''
compile_judge_data(
    input_folder="output/records",
    output_file="output/compiled_data.csv",
    events=ld,
    cumulative_only=True
)
'''

#plot_all_model_fits(compiled_file="output/compiled_data.csv", y_col="Aff/Neg Split (%)", x_col="Sit Rate (%)")
