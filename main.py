import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from src.debater_scraper import default_debater_database_path, scrape_debater_ids_from_judge_db
from src.debater_identity_filter import rebuild_debater_identity_for_events
from src.debater_identity_name_enricher import enrich_debater_identity_names
from src.debater_identity_race_imputer import impute_debater_identity_race_probabilities
from src.db_viewer import view_databases
from src.judge_database_filter import build_filtered_judge_database
from src.judge_debater_matcher import match_debaters_into_judge_db
from src.judge_paradigm_classifier import classify_judge_paradigms_with_ollama
from src.judge_scraper import default_database_path, scrape_judges
from src.rounds_compiler import compile_hs_rounds_database, default_round_database_path
from src.stop_controller import StopController
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
PARALLEL_STOP_FLAG_PATH = os.path.join(OUTPUT_DIR, "parallel.stop.flag")
ENABLE_SIGNAL_STOP = True

DISABLE_PROGRESS = False
SMOOTHING = 1.0
MAX_RETRIES = 3
RETRY_BACKOFF = 5

RUN_JUDGE_SCRAPER = False
RUN_DEBATER_SCRAPER = False
RUN_ROUNDS_COMPILER = False
RUN_TOURNAMENT_STATS = False
RUN_DEBATER_IDENTITY_FILTER = False
RUN_DEBATER_IDENTITY_NAME_ENRICHER = True
RUN_DEBATER_IDENTITY_RACE_IMPUTER = True
RUN_JUDGE_PARADIGM_CLASSIFIER = False
RUN_JUDGE_DATABASE_FILTER = False
DEBATER_IDENTITY_EVENT_ALLOWLIST = [
    "PF", "VPF", "NPF", "JVPF", "OPF", "PF-V", "PFV", "PF-N", "PF-O", "PFN", "PFJV", "PFD",
    "PF-T", "PF-JV", "PFO", "PFRR", "PF-G", "VPFD", "JPF", "PF-ONL", "PF-RS", "PF-S", "CPF",
    "PFS", "PFG", "N-PF", "POFO", "HSVPF", "O-PF", "PF-I", "OL-VPF", "VARPF", "PFONBG", "SPF",
    "HSPF", "3NPF", "PFONOP", "HSJVPF", "IPF", "GPF", "PF-VO", "1VPF", "TPF", "PF-INP", "PF1",
    "NOVPF", "WPF",
]

RUN_DEBATER_JUDGE_MATCHER = False

DEBATER_FORCE_REPROCESS = False
ROUNDS_FORCE_REPROCESS = False
DEBATER_IDENTITY_MIN_TOURNAMENT_HS_ROUNDS = 500
DEBATER_IDENTITY_NAME_RATE_LIMIT_SECONDS = 0.25
DEBATER_IDENTITY_NAME_URL_TEMPLATE = "https://www.tabroom.com/index/results/team_results.mhtml?id1={debater_id}"
DEBATER_IDENTITY_NAME_ONLY_BLANKS = True
DEBATER_IDENTITY_RACE_ONLY_BLANK_VECTORS = True
DEBATER_IDENTITY_RACE_FORCE_REPROCESS = False
DEBATER_IDENTITY_RACE_WAIT_FOR_NAME_POLL_SECONDS = 2.0
JUDGE_PARADIGM_MODEL_NAME = "qwen2.5:7b"
JUDGE_PARADIGM_OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"
JUDGE_PARADIGM_ONLY_BLANK_CATEGORIES = False
JUDGE_PARADIGM_FORCE_REPROCESS = True
JUDGE_PARADIGM_REQUEST_TIMEOUT_SECONDS = 90.0
JUDGE_PARADIGM_MAX_INPUT_CHARS = 1800
JUDGE_PARADIGM_MAX_PARALLEL_REQUESTS = 8
JUDGE_PARADIGM_NUM_CTX = 1024
JUDGE_PARADIGM_MAX_RETRIES = 1
JUDGE_PARADIGM_RETRY_BACKOFF = 0.5
FILTERED_JUDGE_DATABASE_PATH = os.path.join(OUTPUT_DIR, "judges", "judge_data.filtered.sqlite3")
JUDGE_PARADIGM_DATABASE_PATH = FILTERED_JUDGE_DATABASE_PATH

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
        and not RUN_DEBATER_IDENTITY_FILTER
        and not RUN_DEBATER_IDENTITY_NAME_ENRICHER
        and not RUN_DEBATER_IDENTITY_RACE_IMPUTER
        and not RUN_JUDGE_PARADIGM_CLASSIFIER
        and not RUN_JUDGE_DATABASE_FILTER
        and not VIEW_DATABASES
    ):
        raise RuntimeError(
            "Enable at least one mode: RUN_JUDGE_SCRAPER, RUN_DEBATER_SCRAPER, "
            "RUN_DEBATER_JUDGE_MATCHER, RUN_ROUNDS_COMPILER, RUN_TOURNAMENT_STATS, "
            "RUN_DEBATER_IDENTITY_FILTER, RUN_DEBATER_IDENTITY_NAME_ENRICHER, "
            "RUN_DEBATER_IDENTITY_RACE_IMPUTER, RUN_JUDGE_PARADIGM_CLASSIFIER, "
            "RUN_JUDGE_DATABASE_FILTER, "
            "or VIEW_DATABASES."
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

    if RUN_DEBATER_IDENTITY_FILTER and not os.path.exists(DEBATER_DATABASE_PATH):
        raise RuntimeError(f"Debater database not found: {DEBATER_DATABASE_PATH}")
    if RUN_DEBATER_IDENTITY_FILTER and not os.path.exists(DATABASE_PATH):
        raise RuntimeError(f"Judge database not found: {DATABASE_PATH}")

    if RUN_DEBATER_IDENTITY_NAME_ENRICHER and not os.path.exists(DEBATER_DATABASE_PATH):
        raise RuntimeError(f"Debater database not found: {DEBATER_DATABASE_PATH}")
    if RUN_DEBATER_IDENTITY_RACE_IMPUTER and not os.path.exists(DEBATER_DATABASE_PATH):
        raise RuntimeError(f"Debater database not found: {DEBATER_DATABASE_PATH}")

    if RUN_JUDGE_PARADIGM_CLASSIFIER:
        classifier_db_path = FILTERED_JUDGE_DATABASE_PATH if RUN_JUDGE_DATABASE_FILTER else JUDGE_PARADIGM_DATABASE_PATH
        if not os.path.exists(classifier_db_path):
            is_expected_after_filter = (
                RUN_JUDGE_DATABASE_FILTER
                and os.path.abspath(classifier_db_path) == os.path.abspath(FILTERED_JUDGE_DATABASE_PATH)
            )
            if not is_expected_after_filter:
                raise RuntimeError(f"Judge database not found: {classifier_db_path}")
    if RUN_JUDGE_DATABASE_FILTER and not os.path.exists(DATABASE_PATH):
        raise RuntimeError(f"Judge database not found: {DATABASE_PATH}")


def handle_startup_stop_flag(stop_flag_path: str) -> None:
    if not stop_flag_path or not os.path.exists(stop_flag_path):
        return

    modified_at = ""
    try:
        modified_at = datetime.fromtimestamp(os.path.getmtime(stop_flag_path)).isoformat(timespec="seconds")
    except Exception:
        modified_at = "unknown"

    print("\n" + "=" * 88)
    print("WARNING: stop flag exists at startup. This run may stop immediately.")
    print(f"Stop flag path: {stop_flag_path}")
    print(f"Last modified: {modified_at}")
    print("If this is stale, remove it before rerunning:")
    print("  Remove-Item output\\stop.flag -ErrorAction SilentlyContinue")
    print("=" * 88)

    while True:
        try:
            choice = input("Startup stop flag detected. [C]lear and continue (recommended), [K]eep, or [A]bort? ").strip().lower()
        except EOFError:
            choice = "k"

        if not choice:
            choice = "c"

        if choice in {"c", "clear"}:
            try:
                os.remove(stop_flag_path)
                print(f"Removed startup stop flag: {stop_flag_path}\n")
            except FileNotFoundError:
                print("Stop flag was already removed.\n")
            except Exception as exc:
                print(f"Could not remove stop flag automatically: {exc}\n")
            return

        if choice in {"k", "keep"}:
            print("Keeping stop flag. Stages that honor graceful stop may exit quickly.\n")
            return

        if choice in {"a", "abort"}:
            raise RuntimeError("Aborted by user due to startup stop flag.")

        print("Please enter C, K, or A.")


def clear_stop_flag_after_graceful_stop(stop_flag_path: str, pipeline_stopped: bool, stop_reason: str) -> None:
    if not pipeline_stopped:
        return
    if stop_reason not in {"signal", "flag_file"}:
        return
    if not stop_flag_path or not os.path.exists(stop_flag_path):
        return
    try:
        os.remove(stop_flag_path)
        print("Auto-cleared stop flag after graceful stop.")
    except Exception as exc:
        print(f"Warning: could not auto-clear stop flag ({stop_flag_path}): {exc}")


def clear_parallel_stop_flag_at_startup(parallel_stop_flag_path: str) -> None:
    if not parallel_stop_flag_path or not os.path.exists(parallel_stop_flag_path):
        return
    try:
        os.remove(parallel_stop_flag_path)
        print(f"Removed stale parallel stop flag: {parallel_stop_flag_path}")
    except Exception as exc:
        print(f"Warning: could not clear stale parallel stop flag ({parallel_stop_flag_path}): {exc}")


def main() -> None:
    validate_config()
    handle_startup_stop_flag(STOP_FLAG_PATH)
    clear_parallel_stop_flag_at_startup(PARALLEL_STOP_FLAG_PATH)
    effective_judge_paradigm_db_path = (
        FILTERED_JUDGE_DATABASE_PATH if RUN_JUDGE_DATABASE_FILTER else JUDGE_PARADIGM_DATABASE_PATH
    )
    username = ""
    password = ""
    pipeline_stopped = False
    stop_stage = ""
    stop_reason = ""
    stop_unit_key = ""

    if RUN_JUDGE_SCRAPER or RUN_DEBATER_SCRAPER or RUN_DEBATER_IDENTITY_NAME_ENRICHER:
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

    if RUN_DEBATER_IDENTITY_FILTER and not pipeline_stopped:
        identity_filter_result = rebuild_debater_identity_for_events(
            debater_db_path=DEBATER_DATABASE_PATH,
            allowed_event_labels=DEBATER_IDENTITY_EVENT_ALLOWLIST,
            output_dir=OUTPUT_DIR,
            judge_db_path=DATABASE_PATH,
            min_tournament_hs_rounds=DEBATER_IDENTITY_MIN_TOURNAMENT_HS_ROUNDS,
        )
        print(
            "Debater identity filter complete | "
            f"allowlist={identity_filter_result['allowlist_size']} | "
            f"qualifying_tournaments={identity_filter_result['qualifying_tournament_count']} | "
            f"kept_ids={identity_filter_result['kept_ids']} | "
            f"removed_ids={identity_filter_result['removed_ids']}"
        )

    if RUN_JUDGE_DATABASE_FILTER and not pipeline_stopped:
        judge_filter_result = build_filtered_judge_database(
            source_judge_db_path=DATABASE_PATH,
            filtered_judge_db_path=FILTERED_JUDGE_DATABASE_PATH,
            allowed_event_labels=DEBATER_IDENTITY_EVENT_ALLOWLIST,
            output_dir=OUTPUT_DIR,
            min_tournament_hs_rounds=DEBATER_IDENTITY_MIN_TOURNAMENT_HS_ROUNDS,
        )
        print(
            "Filtered judge DB complete | "
            f"all_unique_judges={judge_filter_result['all_unique_judges']} | "
            f"qualifying_tournaments={judge_filter_result['qualifying_tournament_count']} | "
            f"kept_judges={judge_filter_result['kept_judges']} | "
            f"removed_judges={judge_filter_result['removed_judges']} | "
            f"path={judge_filter_result['filtered_judge_db_path']}"
        )

    identity_parallel_task_count = (
        int(bool(RUN_DEBATER_IDENTITY_NAME_ENRICHER))
        + int(bool(RUN_DEBATER_IDENTITY_RACE_IMPUTER))
        + int(bool(RUN_JUDGE_PARADIGM_CLASSIFIER))
    )
    if identity_parallel_task_count >= 2 and not pipeline_stopped:
        parallel_stop_controller = StopController(
            stop_flag_path=STOP_FLAG_PATH,
            enable_signal_stop=ENABLE_SIGNAL_STOP,
        )
        created_parallel_stop_flag = False
        identity_name_result = None
        identity_race_result = None
        paradigm_result = None
        try:
            with ThreadPoolExecutor(max_workers=identity_parallel_task_count) as executor:
                identity_name_future = None
                identity_race_future = None
                paradigm_future = None

                progress_position = 0
                if RUN_DEBATER_IDENTITY_NAME_ENRICHER:
                    identity_name_future = executor.submit(
                        enrich_debater_identity_names,
                        username=username,
                        password=password,
                        base_url=BASE_URL,
                        debater_db_path=DEBATER_DATABASE_PATH,
                        url_template=DEBATER_IDENTITY_NAME_URL_TEMPLATE,
                        sleep_seconds=DEBATER_IDENTITY_NAME_RATE_LIMIT_SECONDS,
                        max_retries=MAX_RETRIES,
                        retry_backoff=RETRY_BACKOFF,
                        only_blank_names=DEBATER_IDENTITY_NAME_ONLY_BLANKS,
                        stop_flag_path=PARALLEL_STOP_FLAG_PATH,
                        enable_signal_stop=False,
                        stops_file=DEBATER_STOPS_FILE,
                        tqdm_position=progress_position,
                        emit_console_logs=False,
                    )
                    progress_position += 1

                if RUN_DEBATER_IDENTITY_RACE_IMPUTER:
                    identity_race_future = executor.submit(
                        impute_debater_identity_race_probabilities,
                        debater_db_path=DEBATER_DATABASE_PATH,
                        only_blank_vectors=DEBATER_IDENTITY_RACE_ONLY_BLANK_VECTORS,
                        force_reprocess=DEBATER_IDENTITY_RACE_FORCE_REPROCESS,
                        wait_for_name_poll_seconds=DEBATER_IDENTITY_RACE_WAIT_FOR_NAME_POLL_SECONDS,
                        stop_flag_path=PARALLEL_STOP_FLAG_PATH,
                        enable_signal_stop=False,
                        stops_file=DEBATER_STOPS_FILE,
                        tqdm_position=progress_position,
                        emit_console_logs=False,
                    )
                    progress_position += 1

                if RUN_JUDGE_PARADIGM_CLASSIFIER:
                    paradigm_future = executor.submit(
                        classify_judge_paradigms_with_ollama,
                        judge_db_path=effective_judge_paradigm_db_path,
                        model_name=JUDGE_PARADIGM_MODEL_NAME,
                        ollama_chat_url=JUDGE_PARADIGM_OLLAMA_CHAT_URL,
                        only_blank_categories=JUDGE_PARADIGM_ONLY_BLANK_CATEGORIES,
                        force_reprocess=JUDGE_PARADIGM_FORCE_REPROCESS,
                        request_timeout_seconds=JUDGE_PARADIGM_REQUEST_TIMEOUT_SECONDS,
                        max_retries=JUDGE_PARADIGM_MAX_RETRIES,
                        retry_backoff=JUDGE_PARADIGM_RETRY_BACKOFF,
                        max_input_chars=JUDGE_PARADIGM_MAX_INPUT_CHARS,
                        max_parallel_requests=JUDGE_PARADIGM_MAX_PARALLEL_REQUESTS,
                        num_ctx=JUDGE_PARADIGM_NUM_CTX,
                        stop_flag_path=PARALLEL_STOP_FLAG_PATH,
                        enable_signal_stop=False,
                        stops_file=JUDGE_STOPS_FILE,
                        tqdm_position=progress_position,
                        emit_console_logs=False,
                    )

                futures = [f for f in (identity_name_future, identity_race_future, paradigm_future) if f is not None]
                while not all(f.done() for f in futures):
                    if parallel_stop_controller.stop_requested():
                        if not os.path.exists(PARALLEL_STOP_FLAG_PATH):
                            stop_parent = os.path.dirname(PARALLEL_STOP_FLAG_PATH)
                            if stop_parent:
                                os.makedirs(stop_parent, exist_ok=True)
                            with open(PARALLEL_STOP_FLAG_PATH, "a", encoding="utf-8"):
                                pass
                            created_parallel_stop_flag = True
                    time.sleep(0.2)

                if identity_name_future is not None:
                    identity_name_result = identity_name_future.result()
                if identity_race_future is not None:
                    identity_race_result = identity_race_future.result()
                if paradigm_future is not None:
                    paradigm_result = paradigm_future.result()
        finally:
            parallel_stop_controller.close()
            if created_parallel_stop_flag and os.path.exists(PARALLEL_STOP_FLAG_PATH):
                try:
                    os.remove(PARALLEL_STOP_FLAG_PATH)
                    print("Auto-cleared parallel stop flag created by this parallel run.")
                except Exception as exc:
                    print(f"Warning: could not auto-clear stop flag ({PARALLEL_STOP_FLAG_PATH}): {exc}")

        if identity_name_result is not None and identity_name_result.get("stopped"):
            pipeline_stopped = True
            stop_stage = "debater_identity_name_enricher"
            stop_reason = identity_name_result.get("stop_reason", "")
            stop_unit_key = identity_name_result.get("stop_unit_key", "")
            print(f"Pipeline stop after identity-name stage | reason={stop_reason} | unit={stop_unit_key}")
        if identity_race_result is not None and identity_race_result.get("stopped") and not pipeline_stopped:
            pipeline_stopped = True
            stop_stage = "debater_identity_race_imputer"
            stop_reason = identity_race_result.get("stop_reason", "")
            stop_unit_key = identity_race_result.get("stop_unit_key", "")
            print(f"Pipeline stop after identity-race stage | reason={stop_reason} | unit={stop_unit_key}")
        if paradigm_result is not None and paradigm_result.get("stopped") and not pipeline_stopped:
            pipeline_stopped = True
            stop_stage = "judge_paradigm_classifier"
            stop_reason = paradigm_result.get("stop_reason", "")
            stop_unit_key = paradigm_result.get("stop_unit_key", "")
            print(f"Pipeline stop after paradigm stage | reason={stop_reason} | unit={stop_unit_key}")

        if identity_name_result is not None:
            print(
                "Debater identity name-enrichment complete | "
                f"processed={identity_name_result['processed_count']} | "
                f"updated={identity_name_result['updated_count']} | "
                f"failed={identity_name_result['failed_count']} | "
                f"remaining_blank={identity_name_result['remaining_blank_count']}"
            )
        if identity_race_result is not None:
            print(
                "Debater identity race-imputation complete | "
                f"processed={identity_race_result['processed_count']} | "
                f"updated={identity_race_result['updated_count']} | "
                f"failed={identity_race_result['failed_count']} | "
                f"remaining_blank={identity_race_result['remaining_blank_vector_count']} | "
                f"model={identity_race_result['model']}"
            )
        if paradigm_result is not None:
            print(
                "Judge paradigm classification complete | "
                f"processed={paradigm_result['processed_count']} | "
                f"updated={paradigm_result['updated_count']} | "
                f"failed={paradigm_result['failed_count']} | "
                f"remaining={paradigm_result['remaining_unclassified_count']} | "
                f"model={paradigm_result['model']} | "
                f"db={effective_judge_paradigm_db_path}"
            )

    elif RUN_DEBATER_IDENTITY_NAME_ENRICHER and not pipeline_stopped:
        identity_name_result = enrich_debater_identity_names(
            username=username,
            password=password,
            base_url=BASE_URL,
            debater_db_path=DEBATER_DATABASE_PATH,
            url_template=DEBATER_IDENTITY_NAME_URL_TEMPLATE,
            sleep_seconds=DEBATER_IDENTITY_NAME_RATE_LIMIT_SECONDS,
            max_retries=MAX_RETRIES,
            retry_backoff=RETRY_BACKOFF,
            only_blank_names=DEBATER_IDENTITY_NAME_ONLY_BLANKS,
            stop_flag_path=STOP_FLAG_PATH,
            enable_signal_stop=ENABLE_SIGNAL_STOP,
            stops_file=DEBATER_STOPS_FILE,
            tqdm_position=0,
            emit_console_logs=False,
        )
        if identity_name_result.get("stopped"):
            pipeline_stopped = True
            stop_stage = "debater_identity_name_enricher"
            stop_reason = identity_name_result.get("stop_reason", "")
            stop_unit_key = identity_name_result.get("stop_unit_key", "")
            print(f"Pipeline stop after identity-name stage | reason={stop_reason} | unit={stop_unit_key}")
        print(
            "Debater identity name-enrichment complete | "
            f"processed={identity_name_result['processed_count']} | "
            f"updated={identity_name_result['updated_count']} | "
            f"failed={identity_name_result['failed_count']} | "
            f"remaining_blank={identity_name_result['remaining_blank_count']}"
        )

    elif RUN_DEBATER_IDENTITY_RACE_IMPUTER and not pipeline_stopped:
        identity_race_result = impute_debater_identity_race_probabilities(
            debater_db_path=DEBATER_DATABASE_PATH,
            only_blank_vectors=DEBATER_IDENTITY_RACE_ONLY_BLANK_VECTORS,
            force_reprocess=DEBATER_IDENTITY_RACE_FORCE_REPROCESS,
            wait_for_name_poll_seconds=DEBATER_IDENTITY_RACE_WAIT_FOR_NAME_POLL_SECONDS,
            stop_flag_path=STOP_FLAG_PATH,
            enable_signal_stop=ENABLE_SIGNAL_STOP,
            stops_file=DEBATER_STOPS_FILE,
            tqdm_position=0,
            emit_console_logs=False,
        )
        if identity_race_result.get("stopped"):
            pipeline_stopped = True
            stop_stage = "debater_identity_race_imputer"
            stop_reason = identity_race_result.get("stop_reason", "")
            stop_unit_key = identity_race_result.get("stop_unit_key", "")
            print(f"Pipeline stop after identity-race stage | reason={stop_reason} | unit={stop_unit_key}")
        print(
            "Debater identity race-imputation complete | "
            f"processed={identity_race_result['processed_count']} | "
            f"updated={identity_race_result['updated_count']} | "
            f"failed={identity_race_result['failed_count']} | "
            f"remaining_blank={identity_race_result['remaining_blank_vector_count']} | "
            f"model={identity_race_result['model']}"
        )

    elif RUN_JUDGE_PARADIGM_CLASSIFIER and not pipeline_stopped:
        paradigm_result = classify_judge_paradigms_with_ollama(
            judge_db_path=effective_judge_paradigm_db_path,
            model_name=JUDGE_PARADIGM_MODEL_NAME,
            ollama_chat_url=JUDGE_PARADIGM_OLLAMA_CHAT_URL,
            only_blank_categories=JUDGE_PARADIGM_ONLY_BLANK_CATEGORIES,
            force_reprocess=JUDGE_PARADIGM_FORCE_REPROCESS,
            request_timeout_seconds=JUDGE_PARADIGM_REQUEST_TIMEOUT_SECONDS,
            max_retries=JUDGE_PARADIGM_MAX_RETRIES,
            retry_backoff=JUDGE_PARADIGM_RETRY_BACKOFF,
            max_input_chars=JUDGE_PARADIGM_MAX_INPUT_CHARS,
            max_parallel_requests=JUDGE_PARADIGM_MAX_PARALLEL_REQUESTS,
            num_ctx=JUDGE_PARADIGM_NUM_CTX,
            stop_flag_path=STOP_FLAG_PATH,
            enable_signal_stop=ENABLE_SIGNAL_STOP,
            stops_file=JUDGE_STOPS_FILE,
            tqdm_position=0,
            emit_console_logs=False,
        )
        print(
            "Judge paradigm classification complete | "
            f"processed={paradigm_result['processed_count']} | "
            f"updated={paradigm_result['updated_count']} | "
            f"failed={paradigm_result['failed_count']} | "
            f"remaining={paradigm_result['remaining_unclassified_count']} | "
            f"model={paradigm_result['model']} | "
            f"db={effective_judge_paradigm_db_path}"
        )
        if paradigm_result.get("stopped"):
            pipeline_stopped = True
            stop_stage = "judge_paradigm_classifier"
            stop_reason = paradigm_result.get("stop_reason", "")
            stop_unit_key = paradigm_result.get("stop_unit_key", "")
            print(f"Pipeline stop after paradigm stage | reason={stop_reason} | unit={stop_unit_key}")

    if VIEW_DATABASES:
        view_databases(
            judge_database_path=DATABASE_PATH,
            debater_database_path=DEBATER_DATABASE_PATH,
            sample_rows=VIEW_DB_SAMPLE_ROWS,
            show_raw=VIEW_DATABASES_RAW,
            raw_limit_rows=VIEW_DATABASES_RAW_LIMIT,
        )

    clear_stop_flag_after_graceful_stop(
        stop_flag_path=STOP_FLAG_PATH,
        pipeline_stopped=pipeline_stopped,
        stop_reason=stop_reason,
    )

    if pipeline_stopped:
        print(f"Pipeline stopped gracefully at stage={stop_stage} | reason={stop_reason} | unit={stop_unit_key}")


if __name__ == "__main__":
    main()
