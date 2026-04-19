import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteJudgeStorage:
    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS judge_pages (
                    judge_person_id INTEGER PRIMARY KEY,
                    judge_name TEXT,
                    safe_name TEXT,
                    fetched_at TEXT NOT NULL,
                    source_url TEXT,
                    raw_page_html TEXT,
                    paradigm_html TEXT,
                    results_html TEXT,
                    record_csv TEXT,
                    record_json TEXT,
                    results_links_json TEXT
                );

                CREATE TABLE IF NOT EXISTS judge_rounds (
                    judge_person_id INTEGER NOT NULL,
                    row_index INTEGER NOT NULL,
                    tournament TEXT,
                    level TEXT,
                    date_text TEXT,
                    date_sort_key TEXT,
                    event TEXT,
                    round_label TEXT,
                    round_sort_key TEXT,
                    aff TEXT,
                    neg TEXT,
                    vote TEXT,
                    result TEXT,
                    tournament_url TEXT,
                    round_url TEXT,
                    aff_url TEXT,
                    neg_url TEXT,
                    all_links_json TEXT,
                    PRIMARY KEY (judge_person_id, row_index),
                    FOREIGN KEY (judge_person_id) REFERENCES judge_pages (judge_person_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS scrape_progress (
                    judge_person_id INTEGER PRIMARY KEY,
                    judge_name TEXT,
                    stored_at TEXT NOT NULL,
                    has_paradigm INTEGER NOT NULL,
                    has_record INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scrape_failures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    judge_person_id INTEGER NOT NULL,
                    error TEXT NOT NULL,
                    failed_at TEXT NOT NULL,
                    is_resolved INTEGER NOT NULL DEFAULT 0,
                    resolved_at TEXT
                );

                CREATE TABLE IF NOT EXISTS judge_scrape_stops (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phase TEXT NOT NULL,
                    unit_key TEXT,
                    reason TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    stopped_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_judge_rounds_judge_person_id
                    ON judge_rounds (judge_person_id);
                """
            )
        self._ensure_column("scrape_failures", "is_resolved", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("scrape_failures", "resolved_at", "TEXT")

    def _ensure_column(self, table_name: str, column_name: str, column_sql: str) -> None:
        columns = self.conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        existing = {row["name"] for row in columns}
        if column_name not in existing:
            with self.conn:
                self.conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")

    def close(self) -> None:
        self.conn.close()

    def load_processed_ids(self) -> set[int]:
        with closing(self.conn.cursor()) as cursor:
            cursor.execute("SELECT judge_person_id FROM scrape_progress")
            return {int(row["judge_person_id"]) for row in cursor.fetchall()}

    def save_judge_page(
        self,
        judge_person_id: int,
        judge_name: str,
        safe_name: str,
        source_url: str,
        raw_page_html: str,
        paradigm_html: str,
        results_html: str,
        record_csv: str,
        record_rows: list[dict],
        results_links: list[dict],
    ) -> None:
        fetched_at = utc_now_iso()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO judge_pages (
                    judge_person_id, judge_name, safe_name, fetched_at, source_url,
                    raw_page_html, paradigm_html, results_html, record_csv, record_json, results_links_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(judge_person_id) DO UPDATE SET
                    judge_name = excluded.judge_name,
                    safe_name = excluded.safe_name,
                    fetched_at = excluded.fetched_at,
                    source_url = excluded.source_url,
                    raw_page_html = excluded.raw_page_html,
                    paradigm_html = excluded.paradigm_html,
                    results_html = excluded.results_html,
                    record_csv = excluded.record_csv,
                    record_json = excluded.record_json,
                    results_links_json = excluded.results_links_json
                """,
                (
                    judge_person_id,
                    judge_name,
                    safe_name,
                    fetched_at,
                    source_url,
                    raw_page_html,
                    paradigm_html,
                    results_html,
                    record_csv,
                    json.dumps(record_rows, ensure_ascii=False),
                    json.dumps(results_links, ensure_ascii=False),
                ),
            )

            self.conn.execute(
                "DELETE FROM judge_rounds WHERE judge_person_id = ?",
                (judge_person_id,),
            )

            if record_rows:
                self.conn.executemany(
                    """
                    INSERT INTO judge_rounds (
                        judge_person_id, row_index, tournament, level, date_text, date_sort_key,
                        event, round_label, round_sort_key, aff, neg, vote, result,
                        tournament_url, round_url, aff_url, neg_url, all_links_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            judge_person_id,
                            row["row_index"],
                            row.get("Tournament", ""),
                            row.get("Lv", ""),
                            row.get("Date", ""),
                            row.get("_date_sort_key", ""),
                            row.get("Ev", ""),
                            row.get("Rd", ""),
                            row.get("_round_sort_key", ""),
                            row.get("Aff", ""),
                            row.get("Neg", ""),
                            row.get("Vote", ""),
                            row.get("Result", ""),
                            row.get("Tournament_url", ""),
                            row.get("Rd_url", ""),
                            row.get("Aff_url", ""),
                            row.get("Neg_url", ""),
                            json.dumps(row.get("_all_links", []), ensure_ascii=False),
                        )
                        for row in record_rows
                    ],
                )

            self.conn.execute(
                """
                INSERT INTO scrape_progress (
                    judge_person_id, judge_name, stored_at, has_paradigm, has_record
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(judge_person_id) DO UPDATE SET
                    judge_name = excluded.judge_name,
                    stored_at = excluded.stored_at,
                    has_paradigm = excluded.has_paradigm,
                    has_record = excluded.has_record
                """,
                (
                    judge_person_id,
                    judge_name,
                    fetched_at,
                    int(bool(paradigm_html)),
                    int(bool(record_rows)),
                ),
            )

    def log_failure(self, judge_person_id: int, error: str) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO scrape_failures (judge_person_id, error, failed_at, is_resolved, resolved_at)
                VALUES (?, ?, ?, 0, NULL)
                """,
                (judge_person_id, error, utc_now_iso()),
            )

    def mark_failure_resolved(self, judge_person_id: int) -> int:
        now_iso = utc_now_iso()
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE scrape_failures
                SET is_resolved = 1, resolved_at = ?
                WHERE judge_person_id = ? AND COALESCE(is_resolved, 0) = 0
                """,
                (now_iso, judge_person_id),
            )
            return int(cursor.rowcount or 0)

    def fetch_failures(self) -> list[dict]:
        with closing(self.conn.cursor()) as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    judge_person_id,
                    error,
                    failed_at,
                    COALESCE(is_resolved, 0) AS is_resolved,
                    COALESCE(resolved_at, '') AS resolved_at
                FROM scrape_failures
                ORDER BY id ASC
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def log_stop_event(self, phase: str, unit_key: str, reason: str, requested_at: str, stopped_at: str) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO judge_scrape_stops (phase, unit_key, reason, requested_at, stopped_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (phase, unit_key, reason, requested_at, stopped_at),
            )

    def fetch_stop_events(self) -> list[dict]:
        with closing(self.conn.cursor()) as cursor:
            cursor.execute(
                """
                SELECT id, phase, unit_key, reason, requested_at, stopped_at
                FROM judge_scrape_stops
                ORDER BY id ASC
                """
            )
            return [dict(row) for row in cursor.fetchall()]


class SQLiteDebaterStorage:
    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS debater_entries (
                    debater_id TEXT NOT NULL,
                    team_code TEXT NOT NULL,
                    tourn_id INTEGER NOT NULL,
                    tournament_name TEXT,
                    judge_event_label TEXT NOT NULL,
                    matched_sidebar_label TEXT,
                    event_id INTEGER,
                    source_url TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (debater_id, team_code, tourn_id, judge_event_label, event_id, source_url)
                );

                CREATE TABLE IF NOT EXISTS debater_profiles (
                    debater_id TEXT PRIMARY KEY,
                    tournaments_json TEXT NOT NULL,
                    tournament_count INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS debater_identity (
                    debater_id TEXT PRIMARY KEY,
                    debater_name TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS debater_name_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    debater_id TEXT NOT NULL,
                    debater_name TEXT NOT NULL,
                    tourn_id INTEGER,
                    event_id INTEGER,
                    source_url TEXT,
                    observed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS debater_tournament_events (
                    tourn_id INTEGER NOT NULL,
                    tournament_name TEXT,
                    judge_event_label TEXT NOT NULL,
                    matched_sidebar_label TEXT,
                    dropdown_label TEXT,
                    event_id INTEGER,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tourn_id, judge_event_label)
                );

                CREATE TABLE IF NOT EXISTS debater_progress (
                    tourn_id INTEGER NOT NULL,
                    judge_event_label TEXT NOT NULL,
                    event_id INTEGER,
                    status TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    stored_at TEXT NOT NULL,
                    PRIMARY KEY (tourn_id, judge_event_label)
                );

                CREATE TABLE IF NOT EXISTS debater_failures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tourn_id INTEGER NOT NULL,
                    tournament_name TEXT,
                    judge_event_label TEXT NOT NULL,
                    event_id INTEGER,
                    error TEXT NOT NULL,
                    failed_at TEXT NOT NULL,
                    is_resolved INTEGER NOT NULL DEFAULT 0,
                    resolved_at TEXT
                );

                CREATE TABLE IF NOT EXISTS debater_scrape_stops (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phase TEXT NOT NULL,
                    unit_key TEXT,
                    reason TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    stopped_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_debater_entries_tourn
                    ON debater_entries (tourn_id, judge_event_label);
                CREATE INDEX IF NOT EXISTS idx_debater_entries_debater
                    ON debater_entries (debater_id);
                CREATE INDEX IF NOT EXISTS idx_debater_name_observations_debater
                    ON debater_name_observations (debater_id);
                """
            )
        self._ensure_column("debater_failures", "is_resolved", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("debater_failures", "resolved_at", "TEXT")

    def _ensure_column(self, table_name: str, column_name: str, column_sql: str) -> None:
        columns = self.conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        existing = {row["name"] for row in columns}
        if column_name not in existing:
            with self.conn:
                self.conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")

    def close(self) -> None:
        self.conn.close()

    def load_processed_event_keys(self) -> set[tuple[int, str]]:
        with closing(self.conn.cursor()) as cursor:
            cursor.execute(
                """
                SELECT tourn_id, judge_event_label
                FROM debater_progress
                WHERE status IN ('completed', 'completed_empty', 'unmatched')
                   OR status LIKE 'skipped_tournament_%'
                """
            )
            return {(int(row["tourn_id"]), row["judge_event_label"]) for row in cursor.fetchall()}

    def save_tournament_event(self, row: dict) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO debater_tournament_events (
                    tourn_id, tournament_name, judge_event_label, matched_sidebar_label,
                    dropdown_label, event_id, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tourn_id, judge_event_label) DO UPDATE SET
                    tournament_name = excluded.tournament_name,
                    matched_sidebar_label = excluded.matched_sidebar_label,
                    dropdown_label = excluded.dropdown_label,
                    event_id = excluded.event_id,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    row["tourn_id"],
                    row.get("tournament_name", ""),
                    row["judge_event_label"],
                    row.get("matched_sidebar_label", ""),
                    row.get("dropdown_label", ""),
                    row.get("event_id") if row.get("event_id") not in {"", None} else None,
                    row["status"],
                    utc_now_iso(),
                ),
            )

    def save_debater_rows(self, rows: list[dict]) -> None:
        if not rows:
            return
        now_iso = utc_now_iso()
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO debater_entries (
                    debater_id, team_code, tourn_id, tournament_name, judge_event_label,
                    matched_sidebar_label, event_id, source_url, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(debater_id, team_code, tourn_id, judge_event_label, event_id, source_url) DO UPDATE SET
                    tournament_name = excluded.tournament_name,
                    matched_sidebar_label = excluded.matched_sidebar_label,
                    fetched_at = excluded.fetched_at
                """,
                [
                    (
                        row["debater_id"],
                        row.get("team_code", ""),
                        int(row["tourn_id"]),
                        row.get("tournament_name", ""),
                        row["judge_event_label"],
                        row.get("matched_sidebar_label", ""),
                        int(row["event_id"]) if row.get("event_id") not in {"", None} else None,
                        row["source_url"],
                        now_iso,
                    )
                    for row in rows
                ],
            )

    def save_name_observations(self, rows: list[dict]) -> int:
        payload = []
        now_iso = utc_now_iso()
        for row in rows:
            name = (row.get("debater_name") or "").strip()
            debater_id = (row.get("debater_id") or "").strip()
            if not name or not debater_id:
                continue
            payload.append(
                (
                    debater_id,
                    name,
                    int(row["tourn_id"]) if row.get("tourn_id") not in {"", None} else None,
                    int(row["event_id"]) if row.get("event_id") not in {"", None} else None,
                    row.get("source_url", ""),
                    now_iso,
                )
            )

        if not payload:
            return 0

        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO debater_name_observations (
                    debater_id, debater_name, tourn_id, event_id, source_url, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
        return len(payload)

    def upsert_identity_from_rows(self, rows: list[dict]) -> int:
        latest_by_id: dict[str, str] = {}
        for row in rows:
            debater_id = (row.get("debater_id") or "").strip()
            name = (row.get("debater_name") or "").strip()
            if not debater_id or not name:
                continue
            latest_by_id[debater_id] = name

        if not latest_by_id:
            return 0

        now_iso = utc_now_iso()
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO debater_identity (debater_id, debater_name, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(debater_id) DO UPDATE SET
                    debater_name = excluded.debater_name,
                    updated_at = excluded.updated_at
                """,
                [(debater_id, debater_name, now_iso) for debater_id, debater_name in latest_by_id.items()],
            )
        return len(latest_by_id)

    def rebuild_identity_ids_only(self) -> int:
        with closing(self.conn.cursor()) as cursor:
            rows = cursor.execute(
                """
                SELECT DISTINCT debater_id
                FROM debater_entries
                WHERE COALESCE(debater_id, '') <> ''
                ORDER BY debater_id
                """
            ).fetchall()

        now_iso = utc_now_iso()
        with self.conn:
            self.conn.execute("DELETE FROM debater_identity")
            if rows:
                self.conn.executemany(
                    """
                    INSERT INTO debater_identity (debater_id, debater_name, updated_at)
                    VALUES (?, '', ?)
                    """,
                    [(str(row["debater_id"]), now_iso) for row in rows],
                )
        return len(rows)

    def mark_event_processed(self, tourn_id: int, judge_event_label: str, event_id: int | None, status: str, row_count: int) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO debater_progress (
                    tourn_id, judge_event_label, event_id, status, row_count, stored_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(tourn_id, judge_event_label) DO UPDATE SET
                    event_id = excluded.event_id,
                    status = excluded.status,
                    row_count = excluded.row_count,
                    stored_at = excluded.stored_at
                """,
                (tourn_id, judge_event_label, event_id, status, int(row_count), utc_now_iso()),
            )

    def log_failure(self, tourn_id: int, tournament_name: str, judge_event_label: str, event_id: int | None, error: str) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO debater_failures (
                    tourn_id, tournament_name, judge_event_label, event_id, error, failed_at, is_resolved, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, NULL)
                """,
                (tourn_id, tournament_name, judge_event_label, event_id, error, utc_now_iso()),
            )

    def mark_failure_resolved(self, tourn_id: int, judge_event_label: str, event_id: int | None) -> int:
        now_iso = utc_now_iso()
        with self.conn:
            if event_id is None:
                cursor = self.conn.execute(
                    """
                    UPDATE debater_failures
                    SET is_resolved = 1, resolved_at = ?
                    WHERE tourn_id = ?
                      AND judge_event_label = ?
                      AND COALESCE(is_resolved, 0) = 0
                    """,
                    (now_iso, tourn_id, judge_event_label),
                )
            else:
                cursor = self.conn.execute(
                    """
                    UPDATE debater_failures
                    SET is_resolved = 1, resolved_at = ?
                    WHERE tourn_id = ?
                      AND judge_event_label = ?
                      AND (event_id = ? OR event_id IS NULL)
                      AND COALESCE(is_resolved, 0) = 0
                    """,
                    (now_iso, tourn_id, judge_event_label, event_id),
                )
            return int(cursor.rowcount or 0)

    def fetch_tournament_rows(self) -> list[dict]:
        with closing(self.conn.cursor()) as cursor:
            cursor.execute(
                """
                SELECT tourn_id, tournament_name, judge_event_label, matched_sidebar_label,
                       dropdown_label, event_id, status
                FROM debater_tournament_events
                ORDER BY tourn_id, judge_event_label
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def fetch_debater_rows(self) -> list[dict]:
        with closing(self.conn.cursor()) as cursor:
            cursor.execute(
                """
                SELECT debater_id, team_code, tourn_id, tournament_name, judge_event_label,
                       matched_sidebar_label, event_id, source_url
                FROM debater_entries
                ORDER BY tourn_id, judge_event_label, team_code, debater_id
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def fetch_debater_profiles(self) -> list[dict]:
        with closing(self.conn.cursor()) as cursor:
            cursor.execute(
                """
                SELECT debater_id, tournaments_json, tournament_count, updated_at
                FROM debater_profiles
                ORDER BY debater_id
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def rebuild_debater_profiles(self) -> int:
        with closing(self.conn.cursor()) as cursor:
            rows = cursor.execute(
                """
                SELECT debater_id, tourn_id, tournament_name, judge_event_label, team_code
                FROM debater_entries
                ORDER BY debater_id, tourn_id, judge_event_label, team_code
                """
            ).fetchall()

        profile_map: dict[str, dict] = {}
        for row in rows:
            debater_id = str(row["debater_id"])
            profile = profile_map.setdefault(
                debater_id,
                {
                    "debater_id": debater_id,
                    "entries": [],
                    "seen": set(),
                },
            )
            key = (
                int(row["tourn_id"]),
                row["judge_event_label"] or "",
                row["team_code"] or "",
            )
            if key in profile["seen"]:
                continue
            profile["seen"].add(key)
            profile["entries"].append(
                {
                    "tourn_id": int(row["tourn_id"]),
                    "tournament_name": row["tournament_name"] or "",
                    "event": row["judge_event_label"] or "",
                    "team_code": row["team_code"] or "",
                }
            )

        now_iso = utc_now_iso()
        with self.conn:
            self.conn.execute("DELETE FROM debater_profiles")
            if profile_map:
                self.conn.executemany(
                    """
                    INSERT INTO debater_profiles (
                        debater_id, tournaments_json, tournament_count, updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    [
                        (
                            debater_id,
                            json.dumps(
                                sorted(
                                    info["entries"],
                                    key=lambda item: (item["tourn_id"], item["event"], item["team_code"]),
                                ),
                                ensure_ascii=False,
                            ),
                            len({entry["tourn_id"] for entry in info["entries"]}),
                            now_iso,
                        )
                        for debater_id, info in profile_map.items()
                    ],
                )
        return len(profile_map)

    def fetch_failures(self) -> list[dict]:
        with closing(self.conn.cursor()) as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    tourn_id,
                    tournament_name,
                    judge_event_label,
                    event_id,
                    error,
                    failed_at,
                    COALESCE(is_resolved, 0) AS is_resolved,
                    COALESCE(resolved_at, '') AS resolved_at
                FROM debater_failures
                ORDER BY id ASC
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def log_stop_event(self, phase: str, unit_key: str, reason: str, requested_at: str, stopped_at: str) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO debater_scrape_stops (phase, unit_key, reason, requested_at, stopped_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (phase, unit_key, reason, requested_at, stopped_at),
            )

    def fetch_stop_events(self) -> list[dict]:
        with closing(self.conn.cursor()) as cursor:
            cursor.execute(
                """
                SELECT id, phase, unit_key, reason, requested_at, stopped_at
                FROM debater_scrape_stops
                ORDER BY id ASC
                """
            )
            return [dict(row) for row in cursor.fetchall()]
