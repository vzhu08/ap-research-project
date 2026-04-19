import os
import sqlite3
import textwrap
from json import JSONDecodeError, dumps, loads
from contextlib import closing


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def fetch_count(conn: sqlite3.Connection, query: str, params: tuple = ()) -> int:
    row = conn.execute(query, params).fetchone()
    if row is None:
        return 0
    value = row[0]
    return int(value) if value is not None else 0


def print_header(title: str) -> None:
    print("")
    print("=" * 72)
    print(title)
    print("=" * 72)


def format_value_lines(value) -> list[str]:
    if value is None:
        return ["NULL"]

    text = str(value)
    if text == "":
        return ["''"]

    # Pretty-print JSON payloads so raw blobs are readable.
    if text and text[0] in {"{", "["}:
        try:
            parsed = loads(text)
            pretty_json = dumps(parsed, indent=2, ensure_ascii=False)
            return pretty_json.splitlines()
        except JSONDecodeError:
            pass

    return text.splitlines() or [text]


def print_raw_table_rows(conn: sqlite3.Connection, table_name: str, limit_rows: int) -> None:
    if not table_exists(conn, table_name):
        print(f"{table_name}: table not found")
        return

    count = fetch_count(conn, f"SELECT COUNT(*) FROM {table_name}")
    print("-" * 72)
    print(f"Table: {table_name}")
    print(f"Rows: {count} total")
    if count == 0:
        print("- none")
        return

    columns = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    column_names = [row["name"] for row in columns]
    print(f"columns: {', '.join(column_names)}")
    rows = conn.execute(f"SELECT * FROM {table_name} LIMIT ?", (limit_rows,)).fetchall()
    name_width = max(len(name) for name in column_names)

    for idx, row in enumerate(rows, start=1):
        print("")
        print(f"[Row {idx} of up to {limit_rows}]")
        print("." * 72)
        for name in column_names:
            value_lines = format_value_lines(row[name])
            wrapped_first = textwrap.wrap(value_lines[0], width=120) or [""]
            print(f"{name:<{name_width}} : {wrapped_first[0]}")
            for extra in wrapped_first[1:]:
                print(f"{' ' * (name_width + 3)}{extra}")
            for line in value_lines[1:]:
                wrapped = textwrap.wrap(line, width=120) or [""]
                for subline in wrapped:
                    print(f"{' ' * (name_width + 3)}{subline}")


def view_judge_database(database_path: str, sample_rows: int = 10, show_raw: bool = False, raw_limit_rows: int = 5) -> None:
    print_header(f"Judge DB: {database_path}")
    if not os.path.exists(database_path):
        print("Database file not found.")
        return

    with closing(sqlite3.connect(database_path)) as conn:
        conn.row_factory = sqlite3.Row

        if not table_exists(conn, "judge_pages"):
            print("No judge tables found in this database.")
            return

        total_pages = fetch_count(conn, "SELECT COUNT(*) FROM judge_pages")
        total_rounds = fetch_count(conn, "SELECT COUNT(*) FROM judge_rounds")
        total_progress = fetch_count(conn, "SELECT COUNT(*) FROM scrape_progress")
        total_failures = fetch_count(conn, "SELECT COUNT(*) FROM scrape_failures")
        with_paradigms = fetch_count(conn, "SELECT COUNT(*) FROM scrape_progress WHERE has_paradigm = 1")
        with_records = fetch_count(conn, "SELECT COUNT(*) FROM scrape_progress WHERE has_record = 1")

        print(f"judge_pages: {total_pages}")
        print(f"judge_rounds: {total_rounds}")
        print(f"scrape_progress: {total_progress}")
        print(f"scrape_failures: {total_failures}")
        print(f"progress with paradigms: {with_paradigms}")
        print(f"progress with records: {with_records}")

        print("")
        print("Recent judge failures:")
        if total_failures == 0:
            print("- none")
        else:
            rows = conn.execute(
                """
                SELECT judge_person_id, error, failed_at
                FROM scrape_failures
                ORDER BY id DESC
                LIMIT 10
                """
            ).fetchall()
            for row in rows:
                print(f"- judge_id={row['judge_person_id']} at {row['failed_at']} | {row['error']}")

        print("")
        print(f"Sample judge rounds (up to {sample_rows}):")
        if total_rounds == 0:
            print("- none")
        else:
            rows = conn.execute(
                """
                SELECT judge_person_id, tournament, event, round_label, aff, neg, vote, result
                FROM judge_rounds
                ORDER BY judge_person_id, row_index
                LIMIT ?
                """,
                (sample_rows,),
            ).fetchall()
            for row in rows:
                print(
                    f"- judge={row['judge_person_id']} | {row['tournament']} | {row['event']} | "
                    f"{row['round_label']} | {row['aff']} vs {row['neg']} | vote={row['vote']} | {row['result']}"
                )

        if show_raw:
            print("")
            print(f"Raw judge tables (up to {raw_limit_rows} rows each):")
            print_raw_table_rows(conn, "judge_pages", raw_limit_rows)
            print("")
            print_raw_table_rows(conn, "judge_rounds", raw_limit_rows)
            print("")
            print_raw_table_rows(conn, "scrape_progress", raw_limit_rows)
            print("")
            print_raw_table_rows(conn, "scrape_failures", raw_limit_rows)


def view_debater_database(database_path: str, sample_rows: int = 10, show_raw: bool = False, raw_limit_rows: int = 5) -> None:
    print_header(f"Debater DB: {database_path}")
    if not os.path.exists(database_path):
        print("Database file not found.")
        return

    with closing(sqlite3.connect(database_path)) as conn:
        conn.row_factory = sqlite3.Row

        if not table_exists(conn, "debater_entries"):
            print("No debater tables found in this database.")
            return

        total_entries = fetch_count(conn, "SELECT COUNT(*) FROM debater_entries")
        unique_debaters = fetch_count(conn, "SELECT COUNT(DISTINCT debater_id) FROM debater_entries")
        unique_tournaments = fetch_count(conn, "SELECT COUNT(DISTINCT tourn_id) FROM debater_entries")
        mapped_events = fetch_count(conn, "SELECT COUNT(*) FROM debater_tournament_events")
        profile_rows = fetch_count(conn, "SELECT COUNT(*) FROM debater_profiles")
        progress_rows = fetch_count(conn, "SELECT COUNT(*) FROM debater_progress")
        failure_rows = fetch_count(conn, "SELECT COUNT(*) FROM debater_failures")

        print(f"debater_entries: {total_entries}")
        print(f"unique debater_ids: {unique_debaters}")
        print(f"unique tournaments: {unique_tournaments}")
        print(f"debater_tournament_events: {mapped_events}")
        print(f"debater_profiles: {profile_rows}")
        print(f"debater_progress: {progress_rows}")
        print(f"debater_failures: {failure_rows}")

        print("")
        print("Debater progress by status:")
        if progress_rows == 0:
            print("- none")
        else:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS ct
                FROM debater_progress
                GROUP BY status
                ORDER BY ct DESC, status ASC
                """
            ).fetchall()
            for row in rows:
                print(f"- {row['status']}: {row['ct']}")

        print("")
        print("Recent debater failures:")
        if failure_rows == 0:
            print("- none")
        else:
            rows = conn.execute(
                """
                SELECT tourn_id, judge_event_label, event_id, error, failed_at
                FROM debater_failures
                ORDER BY id DESC
                LIMIT 10
                """
            ).fetchall()
            for row in rows:
                print(
                    f"- tourn_id={row['tourn_id']} event={row['judge_event_label']} "
                    f"(event_id={row['event_id']}) at {row['failed_at']} | {row['error']}"
                )

        print("")
        print(f"Sample debater rows (up to {sample_rows}):")
        if total_entries == 0:
            print("- none")
        else:
            rows = conn.execute(
                """
                SELECT debater_id, team_code, tourn_id, tournament_name, judge_event_label, event_id
                FROM debater_entries
                ORDER BY tourn_id, judge_event_label, team_code, debater_id
                LIMIT ?
                """,
                (sample_rows,),
            ).fetchall()
            for row in rows:
                print(
                    f"- debater_id={row['debater_id']} | team={row['team_code']} | "
                    f"tourn_id={row['tourn_id']} | {row['tournament_name']} | "
                    f"event={row['judge_event_label']} (event_id={row['event_id']})"
                )


        if show_raw:
            print("")
            print(f"Raw debater tables (up to {raw_limit_rows} rows each):")
            print_raw_table_rows(conn, "debater_entries", raw_limit_rows)
            print("")
            print_raw_table_rows(conn, "debater_tournament_events", raw_limit_rows)
            print("")
            print_raw_table_rows(conn, "debater_profiles", raw_limit_rows)
            print("")
            print_raw_table_rows(conn, "debater_progress", raw_limit_rows)
            print("")
            print_raw_table_rows(conn, "debater_failures", raw_limit_rows)


def view_databases(
    judge_database_path: str,
    debater_database_path: str,
    sample_rows: int = 10,
    show_raw: bool = False,
    raw_limit_rows: int = 5,
) -> None:
    view_judge_database(
        judge_database_path,
        sample_rows=sample_rows,
        show_raw=show_raw,
        raw_limit_rows=raw_limit_rows,
    )
    view_debater_database(
        debater_database_path,
        sample_rows=sample_rows,
        show_raw=show_raw,
        raw_limit_rows=raw_limit_rows,
    )
