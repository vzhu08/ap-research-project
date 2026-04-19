import os

from src.debater_scraper import default_debater_database_path, scrape_debater_ids_from_judge_db
from src.db_viewer import view_databases
from src.judge_debater_matcher import match_debaters_into_judge_db
from src.judge_scraper import default_database_path, scrape_judges
from src.rounds_compiler import compile_hs_rounds_database, default_round_database_path
from src.tournament_stats import default_tournament_stats_output_dir, summarize_tournaments_from_judge_db

BASE_URL = "https://www.tabroom.com"
ID_CSV = "ids.csv"
PROCESS_LIMIT = None

SLEEP_MIN = 0.5
SLEEP_MAX = 1.5

OUTPUT_DIR = "output"
DATABASE_PATH = default_database_path(OUTPUT_DIR)
PROGRESS_FILE = os.path.join(OUTPUT_DIR, "judges", "progress.csv")
FAILURES_FILE = os.path.join(OUTPUT_DIR, "judges", "failures.csv")
DEBATER_DATABASE_PATH = default_debater_database_path(OUTPUT_DIR)
ROUNDS_DATABASE_PATH = default_round_database_path(OUTPUT_DIR)
ROUNDS_PROGRESS_FILE = os.path.join(OUTPUT_DIR, "rounds", "progress.csv")
TOURNAMENT_STATS_OUTPUT_DIR = default_tournament_stats_output_dir(OUTPUT_DIR)
DEBATER_PROGRESS_FILE = os.path.join(OUTPUT_DIR, "debaters", "progress.csv")
DEBATER_FAILURES_FILE = os.path.join(OUTPUT_DIR, "debaters", "failures.csv")
JUDGE_STOPS_FILE = os.path.join(OUTPUT_DIR, "judges", "stops.csv")
DEBATER_STOPS_FILE = os.path.join(OUTPUT_DIR, "debaters", "stops.csv")
STOP_FLAG_PATH = os.path.join(OUTPUT_DIR, "stop.flag")
ENABLE_SIGNAL_STOP = True

DISABLE_PROGRESS = False
SMOOTHING = 1.0
MAX_RETRIES = 3
RETRY_BACKOFF = 5

RUN_JUDGE_SCRAPER = False
RUN_DEBATER_SCRAPER = False
RUN_ROUNDS_COMPILER = False
RUN_TOURNAMENT_STATS = True

RUN_DEBATER_JUDGE_MATCHER = False

DEBATER_FORCE_REPROCESS = False
ROUNDS_FORCE_REPROCESS = False

VIEW_DATABASES = False
VIEW_DB_SAMPLE_ROWS = 20
VIEW_DATABASES_RAW = False
VIEW_DATABASES_RAW_LIMIT = 5

# Keep legacy file exports off by default so the scrape stays DB-first.
WRITE_LEGACY_FILES = False
# Graceful stop helpers:
# - Terminal: press Ctrl+C once to finish current unit and stop cleanly.
# - External: New-Item output\stop.flag -ItemType File
# - Resume: Remove-Item output\stop.flag ; rerun.


def sanitize_credential(value: str | None) -> str:
    cleaned = (value or "").strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
        cleaned = cleaned[1:-1].strip()
    return cleaned


def load_credentials() -> tuple[str, str]:
    username = sanitize_credential(os.environ.get("DEBATE_EMAIL_USER"))
    password = sanitize_credential(os.environ.get("DEBATE_EMAIL_PASS"))
    masked = "*" * min(len(password), 8)
    print(f"Using credentials user={username!r} pass={masked}")

    if not username or not password:
        raise RuntimeError(
            "Missing Tabroom credentials in this shell session. Set DEBATE_EMAIL_USER and DEBATE_EMAIL_PASS."
        )

    return username, password


def validate_config() -> None:
    if (
        not RUN_JUDGE_SCRAPER
        and not RUN_DEBATER_SCRAPER
        and not RUN_DEBATER_JUDGE_MATCHER
        and not RUN_ROUNDS_COMPILER
        and not RUN_TOURNAMENT_STATS
        and not VIEW_DATABASES
    ):
        raise RuntimeError(
            "Enable at least one mode: RUN_JUDGE_SCRAPER, RUN_DEBATER_SCRAPER, "
            "RUN_DEBATER_JUDGE_MATCHER, RUN_ROUNDS_COMPILER, RUN_TOURNAMENT_STATS, or VIEW_DATABASES."
        )

    if RUN_JUDGE_SCRAPER and not os.path.exists(ID_CSV):
        raise RuntimeError(f"Judge ID file not found: {ID_CSV}")

    if RUN_DEBATER_SCRAPER and not RUN_JUDGE_SCRAPER and not os.path.exists(DATABASE_PATH):
        raise RuntimeError(f"Judge database not found: {DATABASE_PATH}")

    if RUN_DEBATER_JUDGE_MATCHER and not os.path.exists(DATABASE_PATH):
        raise RuntimeError(f"Judge database not found: {DATABASE_PATH}")

    if RUN_DEBATER_JUDGE_MATCHER and not os.path.exists(DEBATER_DATABASE_PATH):
        raise RuntimeError(f"Debater database not found: {DEBATER_DATABASE_PATH}")

    if RUN_ROUNDS_COMPILER and not RUN_JUDGE_SCRAPER and not os.path.exists(DATABASE_PATH):
        raise RuntimeError(f"Judge database not found: {DATABASE_PATH}")

    if RUN_TOURNAMENT_STATS and not RUN_JUDGE_SCRAPER and not os.path.exists(DATABASE_PATH):
        raise RuntimeError(f"Judge database not found: {DATABASE_PATH}")


def main() -> None:
    validate_config()
    username = ""
    password = ""
    pipeline_stopped = False
    stop_stage = ""
    stop_reason = ""
    stop_unit_key = ""

    if RUN_JUDGE_SCRAPER or RUN_DEBATER_SCRAPER:
        username, password = load_credentials()

    if RUN_JUDGE_SCRAPER:
        judge_result = scrape_judges(
            username=username,
            password=password,
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
            stop_flag_path=STOP_FLAG_PATH,
            enable_signal_stop=ENABLE_SIGNAL_STOP,
            stops_file=JUDGE_STOPS_FILE,
        )
        if judge_result.get("stopped"):
            pipeline_stopped = True
            stop_stage = "judge"
            stop_reason = judge_result.get("stop_reason", "")
            stop_unit_key = judge_result.get("stop_unit_key", "")
            print(f"Pipeline stop after judge stage | reason={stop_reason} | unit={stop_unit_key}")

    if RUN_DEBATER_SCRAPER and not pipeline_stopped:
        debater_result = scrape_debater_ids_from_judge_db(
            username=username,
            password=password,
            base_url=BASE_URL,
            database_path=DATABASE_PATH,
            output_dir=OUTPUT_DIR,
            sleep_min=SLEEP_MIN,
            sleep_max=SLEEP_MAX,
            max_retries=MAX_RETRIES,
            retry_backoff=RETRY_BACKOFF,
            debater_database_path=DEBATER_DATABASE_PATH,
            progress_file=DEBATER_PROGRESS_FILE,
            failures_file=DEBATER_FAILURES_FILE,
            stop_flag_path=STOP_FLAG_PATH,
            enable_signal_stop=ENABLE_SIGNAL_STOP,
            stops_file=DEBATER_STOPS_FILE,
            force_reprocess=DEBATER_FORCE_REPROCESS,
        )
        if debater_result.get("stopped"):
            pipeline_stopped = True
            stop_stage = "debater"
            stop_reason = debater_result.get("stop_reason", "")
            stop_unit_key = debater_result.get("stop_unit_key", "")
            print(f"Pipeline stop after debater stage | reason={stop_reason} | unit={stop_unit_key}")

    if RUN_DEBATER_JUDGE_MATCHER and not pipeline_stopped:
        match_debaters_into_judge_db(
            judge_db_path=DATABASE_PATH,
            debater_db_path=DEBATER_DATABASE_PATH,
            output_dir=OUTPUT_DIR,
        )

    if RUN_ROUNDS_COMPILER and not pipeline_stopped:
        rounds_compile_result = compile_hs_rounds_database(
            judge_db_path=DATABASE_PATH,
            output_dir=OUTPUT_DIR,
            debater_db_path=DEBATER_DATABASE_PATH if os.path.exists(DEBATER_DATABASE_PATH) else None,
            rounds_db_path=ROUNDS_DATABASE_PATH,
            progress_file=ROUNDS_PROGRESS_FILE,
            force_reprocess=ROUNDS_FORCE_REPROCESS,
        )
        print(
            "Rounds database compiled | "
            f"hs_rounds={rounds_compile_result['hs_round_count']} | "
            f"filtered_non_hs={rounds_compile_result['removed_non_hs_round_count']} | "
            f"path={rounds_compile_result['rounds_database_path']}"
        )

    if RUN_TOURNAMENT_STATS and not pipeline_stopped:
        stats_result = summarize_tournaments_from_judge_db(
            judge_db_path=DATABASE_PATH,
            output_dir=OUTPUT_DIR,
            results_dir=TOURNAMENT_STATS_OUTPUT_DIR,
        )
        print(
            "Tournament stats complete | "
            f"tournaments_all={stats_result['tournament_count_all_levels']} | "
            f"tournaments_hs={stats_result['tournament_count_hs']} | "
            f"hs_event_pairs={stats_result['hs_tournament_event_pair_count']} | "
            f"hs_rounds={stats_result['hs_round_total']}"
        )

    if VIEW_DATABASES:
        view_databases(
            judge_database_path=DATABASE_PATH,
            debater_database_path=DEBATER_DATABASE_PATH,
            sample_rows=VIEW_DB_SAMPLE_ROWS,
            show_raw=VIEW_DATABASES_RAW,
            raw_limit_rows=VIEW_DATABASES_RAW_LIMIT,
        )

    if pipeline_stopped:
        print(f"Pipeline stopped gracefully at stage={stop_stage} | reason={stop_reason} | unit={stop_unit_key}")


if __name__ == "__main__":
    main()
