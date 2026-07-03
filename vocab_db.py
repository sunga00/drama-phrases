"""
SQLite 단어장 DB.
  analyzed   — 파일 해시 기반 재분析 방지 캐시
  expressions — 선별된 표현 전체 (lang+text UNIQUE, 재등장 시 seen_count++)
  my_vocab   — 사용자가 ⭐ 저장한 표현
"""

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("vocab.db")


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db() -> None:
    with _conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS analyzed (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                file_hash   TEXT    UNIQUE NOT NULL,
                source_title    TEXT,
                source_episode  TEXT,
                lang        TEXT,
                analyzed_at TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS expressions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                lang         TEXT    NOT NULL,
                text         TEXT    NOT NULL,
                reading      TEXT,
                ko           TEXT,
                level        TEXT,
                usage_note   TEXT,
                example_orig TEXT,
                example_ko   TEXT,
                source_title TEXT,
                source_episode TEXT,
                source_time_s INTEGER,
                seen_count   INTEGER NOT NULL DEFAULT 1,
                created_at   TEXT    NOT NULL,
                UNIQUE(lang, text)
            );

            CREATE TABLE IF NOT EXISTS my_vocab (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                expression_id INTEGER NOT NULL UNIQUE REFERENCES expressions(id),
                starred_at    TEXT    NOT NULL,
                memo          TEXT
            );
        """)
        # 기존 DB 마이그레이션
        for ddl in (
            "ALTER TABLE expressions ADD COLUMN source_episode TEXT",
            "ALTER TABLE expressions ADD COLUMN file_hash TEXT",
        ):
            try:
                con.execute(ddl)
            except Exception:
                pass  # 이미 존재하는 컬럼


def file_md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def is_already_analyzed(file_hash: str) -> bool:
    with _conn() as con:
        row = con.execute(
            "SELECT 1 FROM analyzed WHERE file_hash = ?", (file_hash,)
        ).fetchone()
    return row is not None


def save_analysis(
    file_hash: str,
    source_title: str,
    source_episode: str,
    lang: str,
    expressions: list[dict],
) -> dict[str, tuple[int, int]]:
    """
    분석 결과를 DB에 저장.
    반환: {text: (expression_id, seen_count)}
    """
    now = datetime.now(timezone.utc).isoformat()
    result: dict[str, tuple[int, int]] = {}

    with _conn() as con:
        # analyzed 이력 기록
        con.execute(
            "INSERT OR IGNORE INTO analyzed (file_hash, source_title, source_episode, lang, analyzed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (file_hash, source_title, source_episode, lang, now),
        )

        for expr in expressions:
            text = expr.get("text", "").strip()
            if not text:
                continue

            # 이미 있으면 seen_count 증가, 없으면 신규 삽입
            con.execute(
                """
                INSERT INTO expressions
                    (lang, text, reading, ko, level, usage_note,
                     example_orig, example_ko, source_title, source_episode,
                     file_hash, source_time_s, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(lang, text) DO UPDATE SET seen_count = seen_count + 1
                """,
                (
                    lang,
                    text,
                    expr.get("reading", ""),
                    expr.get("ko", ""),
                    expr.get("level", ""),
                    expr.get("usage_note", ""),
                    expr.get("example_orig", ""),
                    expr.get("example_ko", ""),
                    source_title,
                    source_episode,
                    file_hash,
                    expr.get("source_time_s"),
                    now,
                ),
            )

            row = con.execute(
                "SELECT id, seen_count FROM expressions WHERE lang = ? AND text = ?",
                (lang, text),
            ).fetchone()
            if row:
                result[text] = (row["id"], row["seen_count"])

    return result


def star_expression(expression_id: int, memo: str = "") -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO my_vocab (expression_id, starred_at, memo) VALUES (?, ?, ?)",
            (expression_id, now, memo),
        )


def unstar_expression(expression_id: int) -> None:
    with _conn() as con:
        con.execute("DELETE FROM my_vocab WHERE expression_id = ?", (expression_id,))


def get_starred_ids() -> set[int]:
    with _conn() as con:
        rows = con.execute("SELECT expression_id FROM my_vocab").fetchall()
    return {r["expression_id"] for r in rows}


def get_all_vocab(order: str = "recent") -> list[dict]:
    order_clause = {
        "recent": "v.starred_at DESC",
        "seen":   "e.seen_count DESC",
        "level":  "CASE e.level WHEN '고급' THEN 1 WHEN '중급' THEN 2 ELSE 3 END",
    }.get(order, "v.starred_at DESC")

    sql = f"""
        SELECT e.id, e.lang, e.text, e.reading, e.ko, e.level,
               e.usage_note, e.example_orig, e.example_ko,
               e.source_title, e.seen_count, v.starred_at, v.memo
        FROM my_vocab v
        JOIN expressions e ON v.expression_id = e.id
        ORDER BY {order_clause}
    """
    with _conn() as con:
        rows = con.execute(sql).fetchall()
    return [dict(r) for r in rows]


def search_vocab(query: str) -> list[dict]:
    q = f"%{query}%"
    sql = """
        SELECT e.id, e.lang, e.text, e.reading, e.ko, e.level,
               e.usage_note, e.example_orig, e.example_ko,
               e.source_title, e.seen_count, v.starred_at
        FROM my_vocab v
        JOIN expressions e ON v.expression_id = e.id
        WHERE e.text LIKE ? OR e.ko LIKE ? OR e.reading LIKE ?
        ORDER BY v.starred_at DESC
    """
    with _conn() as con:
        rows = con.execute(sql, (q, q, q)).fetchall()
    return [dict(r) for r in rows]


def vocab_count() -> int:
    with _conn() as con:
        return con.execute("SELECT COUNT(*) FROM my_vocab").fetchone()[0]


# ── 라이브러리 쿼리 ────────────────────────────────────────────

def get_analyzed_list() -> list[dict]:
    """분析된 파일 목록 + 각 파일의 표현 수 반환. file_hash 포함."""
    sql = """
        SELECT a.id, a.file_hash, a.source_title, a.source_episode, a.lang, a.analyzed_at,
               COUNT(DISTINCT e.id) AS expr_count
        FROM analyzed a
        LEFT JOIN expressions e
            ON (e.file_hash IS NOT NULL AND e.file_hash = a.file_hash)
            OR (e.file_hash IS NULL
                AND COALESCE(e.source_title,'') = COALESCE(a.source_title,'')
                AND COALESCE(e.source_episode,'') = COALESCE(a.source_episode,''))
        GROUP BY a.id
        ORDER BY a.analyzed_at DESC
    """
    with _conn() as con:
        rows = con.execute(sql).fetchall()
    return [dict(r) for r in rows]


def get_expressions_by_source(
    source_title: str, source_episode: str, file_hash: str | None = None
) -> list[dict]:
    """특정 파일의 표현 전체 반환. file_hash가 있으면 그걸 우선 사용."""
    sql = """
        SELECT e.id, e.lang, e.text, e.reading, e.ko, e.level,
               e.usage_note, e.example_orig, e.example_ko,
               e.source_title, e.source_episode, e.source_time_s, e.seen_count,
               CASE WHEN v.id IS NOT NULL THEN 1 ELSE 0 END AS is_starred
        FROM expressions e
        LEFT JOIN my_vocab v ON v.expression_id = e.id
        WHERE (e.file_hash IS NOT NULL AND e.file_hash = ?)
           OR (e.file_hash IS NULL
               AND e.source_title = ?
               AND COALESCE(e.source_episode,'') = ?)
        ORDER BY e.source_time_s
    """
    with _conn() as con:
        rows = con.execute(sql, (file_hash, source_title, source_episode)).fetchall()
    return [dict(r) for r in rows]


def update_source_info(file_hash: str, new_title: str, new_episode: str) -> None:
    """analyzed 항목과 연결된 expressions의 source_title/source_episode를 일괄 업데이트."""
    with _conn() as con:
        row = con.execute(
            "SELECT source_title, source_episode FROM analyzed WHERE file_hash=?", (file_hash,)
        ).fetchone()
        if not row:
            return
        old_title = row["source_title"] or ""
        old_episode = row["source_episode"] or ""

        con.execute(
            "UPDATE analyzed SET source_title=?, source_episode=? WHERE file_hash=?",
            (new_title, new_episode, file_hash),
        )
        # file_hash 기준 업데이트 (신규 데이터)
        con.execute(
            "UPDATE expressions SET source_title=?, source_episode=? WHERE file_hash=?",
            (new_title, new_episode, file_hash),
        )
        # source_title+episode 기준 업데이트 (file_hash 없는 기존 데이터)
        con.execute(
            """UPDATE expressions SET source_title=?, source_episode=?
               WHERE file_hash IS NULL
               AND source_title=?
               AND COALESCE(source_episode,'')=?""",
            (new_title, new_episode, old_title, old_episode),
        )


def delete_analyzed_entry(file_hash: str) -> tuple[int, int]:
    """analyzed 항목과 연결된 expressions를 삭제.
    반환: (삭제된 표현 수, 그 중 ⭐ 저장됐던 수)
    """
    with _conn() as con:
        row = con.execute(
            "SELECT source_title, source_episode FROM analyzed WHERE file_hash=?", (file_hash,)
        ).fetchone()
        if not row:
            return 0, 0
        old_title = row["source_title"] or ""
        old_episode = row["source_episode"] or ""

        # 삭제할 expression IDs 수집
        ids: set[int] = set()
        for r in con.execute(
            "SELECT id FROM expressions WHERE file_hash=?", (file_hash,)
        ).fetchall():
            ids.add(r[0])
        for r in con.execute(
            """SELECT id FROM expressions
               WHERE file_hash IS NULL
               AND source_title=?
               AND COALESCE(source_episode,'')=?""",
            (old_title, old_episode),
        ).fetchall():
            ids.add(r[0])

        starred_count = 0
        if ids:
            placeholders = ",".join("?" * len(ids))
            id_list = list(ids)
            starred_count = con.execute(
                f"SELECT COUNT(*) FROM my_vocab WHERE expression_id IN ({placeholders})", id_list
            ).fetchone()[0]
            con.execute(
                f"DELETE FROM my_vocab WHERE expression_id IN ({placeholders})", id_list
            )
            con.execute(
                f"DELETE FROM expressions WHERE id IN ({placeholders})", id_list
            )

        con.execute("DELETE FROM analyzed WHERE file_hash=?", (file_hash,))
    return len(ids), starred_count


def get_all_expressions(query: str = "", levels: list[str] | None = None) -> list[dict]:
    """전체 표현 검색+필터 (최대 200개). 단어장 저장 여부(is_starred) 포함."""
    params: list = []
    where: list[str] = []

    if query:
        where.append(
            "(e.text LIKE ? OR e.ko LIKE ? OR e.reading LIKE ? OR e.source_title LIKE ?)"
        )
        q = f"%{query}%"
        params += [q, q, q, q]

    if levels:
        placeholders = ",".join("?" * len(levels))
        where.append(f"e.level IN ({placeholders})")
        params += levels

    where_clause = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"""
        SELECT e.id, e.lang, e.text, e.reading, e.ko, e.level,
               e.usage_note, e.example_orig, e.example_ko,
               e.source_title, e.source_episode, e.source_time_s, e.seen_count,
               CASE WHEN v.id IS NOT NULL THEN 1 ELSE 0 END AS is_starred
        FROM expressions e
        LEFT JOIN my_vocab v ON v.expression_id = e.id
        {where_clause}
        ORDER BY e.source_title, COALESCE(e.source_episode, ''), e.source_time_s
        LIMIT 200
    """
    with _conn() as con:
        rows = con.execute(sql, params).fetchall()
    return [dict(r) for r in rows]
