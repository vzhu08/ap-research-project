import os

from src.debater_scraper import scrape_debater_ids_from_judge_db
from src.judge_scraper import default_database_path, scrape_judges


USERNAME = os.environ.get("DEBATE_EMAIL_USER")
PASSWORD = os.environ.get("DEBATE_EMAIL_PASS")

BASE_URL = "https://www.tabroom.com"
ID_CSV = "ids.csv"
PROCESS_LIMIT = None

SLEEP_MIN = 0.5
SLEEP_MAX = 1.5

OUTPUT_DIR = "output"
DATABASE_PATH = default_database_path(OUTPUT_DIR)
PROGRESS_FILE = os.path.join(OUTPUT_DIR, "progress.csv")
FAILURES_FILE = os.path.join(OUTPUT_DIR, "failures.csv")

DISABLE_PROGRESS = False
SMOOTHING = 1.0
MAX_RETRIES = 3
RETRY_BACKOFF = 5

RUN_JUDGE_SCRAPER = False
RUN_DEBATER_SCRAPER = True

# Keep legacy file exports off by default so the scrape stays DB-first.
WRITE_LEGACY_FILES = False


def validate_config() -> None:
    if not RUN_JUDGE_SCRAPER and not RUN_DEBATER_SCRAPER:
        raise RuntimeError("Enable at least one scraper: RUN_JUDGE_SCRAPER or RUN_DEBATER_SCRAPER.")

    if not USERNAME or not PASSWORD:
        raise RuntimeError(
            "Missing Tabroom credentials. Set DEBATE_EMAIL_USER and DEBATE_EMAIL_PASS."
        )

    if RUN_JUDGE_SCRAPER and not os.path.exists(ID_CSV):
        raise RuntimeError(f"Judge ID file not found: {ID_CSV}")

    if RUN_DEBATER_SCRAPER and not RUN_JUDGE_SCRAPER and not os.path.exists(DATABASE_PATH):
        raise RuntimeError(f"Judge database not found: {DATABASE_PATH}")


def main() -> None:
    validate_config()

    if RUN_JUDGE_SCRAPER:
        scrape_judges(
            username=USERNAME,
            password=PASSWORD,
            base_url=BASE_URL,
            id_csv=ID_CSV,
            process_limit=PROCESS_LIMIT,
            sleep_min=SLEEP_MIN,
            sleep_max=SLEEP_MAX,
            output_dir=OUTPUT_DIR,
            progress_file=PROGRESS_FILE,
            failures_file=FAILURES_FILE,
            disable_progress=DISABLE_PROGRESS,
            smoothing=SMOOTHING,
            max_retries=MAX_RETRIES,
            retry_backoff=RETRY_BACKOFF,
            database_path=DATABASE_PATH,
            write_legacy_files=WRITE_LEGACY_FILES,
        )

    if RUN_DEBATER_SCRAPER:
        scrape_debater_ids_from_judge_db(
            username=USERNAME,
            password=PASSWORD,
            base_url=BASE_URL,
            database_path=DATABASE_PATH,
            output_dir=OUTPUT_DIR,
            sleep_min=SLEEP_MIN,
            sleep_max=SLEEP_MAX,
            max_retries=MAX_RETRIES,
            retry_backoff=RETRY_BACKOFF,
        )


if __name__ == "__main__":
    main()
