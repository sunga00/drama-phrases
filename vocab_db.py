"""
Postgres 단어장 DB (Supabase).
  analyzed   — 파일 해시 기반 재분석 방지 캐시
  expressions — 선별된 표현 전체 (lang+text UNIQUE, 재등장 시 seen_count++)
  my_vocab   — 사용자가 ⭐ 저장한 표현
"""

import hashlib
import os
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras


def _get_secret(key: str) -> str:
    """st.secrets(Streamlit Cloud) 우선, 없으면 os.environ(로컬 .env) 폴백."""
    try:
        import streamlit as st
        return st.secrets[key]
    except Exception:
        return os.environ.get(key, "")


def _dsn() -> str:
    url = _get_secret("SUPABASE_DB_URL")
    if not url:
        raise RuntimeError("SUPABASE_DB_URL이 설정되지 않았습니다.")
    return url


@contextmanager
def _conn():
    """트랜잭션 단위 Postgres 커서를 yield. 성공 시 commit, 예외 시 rollback."""
    con = psycopg2.connect(_dsn(), connect_timeout=10)
    cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield cur
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        cur.close()
        con.close()


def init_db() -> None:
    with _conn() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS analyzed (
                id             BIGSERIAL PRIMARY KEY,
                file_hash      TEXT      UNIQUE NOT NULL,
                source_title   TEXT,
                source_episode TEXT,
                lang           TEXT,
                analyzed_at    TEXT      NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS expressions (
                id             BIGSERIAL PRIMARY KEY,
                lang           TEXT    NOT NULL,
                text           TEXT    NOT NULL,
                reading        TEXT,
                ko             TEXT,
                level          TEXT,
                usage_note     TEXT,
                example_orig   TEXT,
                example_ko     TEXT,
                source_title   TEXT,
                source_episode TEXT,
                source_time_s  INTEGER,
                seen_count     INTEGER NOT NULL DEFAULT 1,
                created_at     TEXT    NOT NULL,
                file_hash      TEXT,
                type           TEXT,
                UNIQUE (lang, text)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS my_vocab (
                id            BIGSERIAL PRIMARY KEY,
                expression_id BIGINT    NOT NULL UNIQUE REFERENCES expressions(id),
                starred_at    TEXT      NOT NULL,
                memo          TEXT
            )
        """)
        # type 백필 (idempotent)
        cur.execute("""
            UPDATE expressions
            SET type = CASE
                WHEN text LIKE '%…%' OR text LIKE '%...%' THEN '문형'
                ELSE '표현'
            END
            WHERE type IS NULL
        """)


def file_md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def is_already_analyzed(file_hash: str) -> bool:
    with _conn() as cur:
        cur.execute("SELECT 1 FROM analyzed WHERE file_hash = %s", (file_hash,))
        return cur.fetchone() is not None


def save_analysis(
    file_hash: str,
    source_title: str,
    source_episode: str,
    lang: str,
    expressions: list[dict],
) -> dict[str, tuple[int, int]]:
    """분析 결과를 DB에 저장. 반환: {text: (expression_id, seen_count)}"""
    now = datetime.now(timezone.utc).isoformat()
    result: dict[str, tuple[int, int]] = {}

    with _conn() as cur:
        cur.execute(
            """INSERT INTO analyzed (file_hash, source_title, source_episode, lang, analyzed_at)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (file_hash) DO NOTHING""",
            (file_hash, source_title, source_episode, lang, now),
        )

        for expr in expressions:
            text = expr.get("text", "").strip()
            if not text:
                continue

            cur.execute(
                """
                INSERT INTO expressions
                    (lang, text, reading, ko, level, usage_note,
                     example_orig, example_ko, source_title, source_episode,
                     file_hash, source_time_s, type, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (lang, text) DO UPDATE SET seen_count = expressions.seen_count + 1
                """,
                (
                    lang, text,
                    expr.get("reading", ""), expr.get("ko", ""), expr.get("level", ""),
                    expr.get("usage_note", ""), expr.get("example_orig", ""),
                    expr.get("example_ko", ""), source_title, source_episode,
                    file_hash, expr.get("source_time_s"), expr.get("type", "표현"), now,
                ),
            )
            cur.execute(
                "SELECT id, seen_count FROM expressions WHERE lang = %s AND text = %s",
                (lang, text),
            )
            row = cur.fetchone()
            if row:
                result[text] = (row["id"], row["seen_count"])

    return result


def star_expression(expression_id: int, memo: str = "") -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as cur:
        cur.execute(
            """INSERT INTO my_vocab (expression_id, starred_at, memo)
               VALUES (%s, %s, %s)
               ON CONFLICT (expression_id) DO NOTHING""",
            (expression_id, now, memo),
        )


def unstar_expression(expression_id: int) -> None:
    with _conn() as cur:
        cur.execute("DELETE FROM my_vocab WHERE expression_id = %s", (expression_id,))


def get_starred_ids() -> set[int]:
    with _conn() as cur:
        cur.execute("SELECT expression_id FROM my_vocab")
        rows = cur.fetchall()
    return {r["expression_id"] for r in rows}


def get_all_vocab(order: str = "recent") -> list[dict]:
    order_clause = {
        "recent": "v.starred_at DESC",
        "seen":   "e.seen_count DESC",
        "level":  "CASE e.level WHEN '고급' THEN 1 WHEN '중급' THEN 2 ELSE 3 END",
    }.get(order, "v.starred_at DESC")

    sql = f"""
        SELECT e.id, e.lang, e.text, e.reading, e.ko, e.level, e.type,
               e.usage_note, e.example_orig, e.example_ko,
               e.source_title, e.seen_count, v.starred_at, v.memo
        FROM my_vocab v
        JOIN expressions e ON v.expression_id = e.id
        ORDER BY {order_clause}
    """
    with _conn() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def search_vocab(query: str) -> list[dict]:
    q = f"%{query}%"
    sql = """
        SELECT e.id, e.lang, e.text, e.reading, e.ko, e.level, e.type,
               e.usage_note, e.example_orig, e.example_ko,
               e.source_title, e.seen_count, v.starred_at
        FROM my_vocab v
        JOIN expressions e ON v.expression_id = e.id
        WHERE e.text ILIKE %s OR e.ko ILIKE %s OR e.reading ILIKE %s
        ORDER BY v.starred_at DESC
    """
    with _conn() as cur:
        cur.execute(sql, (q, q, q))
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def vocab_count() -> int:
    with _conn() as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM my_vocab")
        return cur.fetchone()["cnt"]


# ── 라이브러리 쿼리 ────────────────────────────────────────────

def get_analyzed_list() -> list[dict]:
    """분석된 파일 목록 + 각 파일의 표현 수 반환."""
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
    with _conn() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def get_expressions_by_source(
    source_title: str, source_episode: str, file_hash: str | None = None
) -> list[dict]:
    """특정 파일의 표현 전체 반환. file_hash 우선 사용."""
    sql = """
        SELECT e.id, e.lang, e.text, e.reading, e.ko, e.level, e.type,
               e.usage_note, e.example_orig, e.example_ko,
               e.source_title, e.source_episode, e.source_time_s, e.seen_count,
               CASE WHEN v.id IS NOT NULL THEN 1 ELSE 0 END AS is_starred
        FROM expressions e
        LEFT JOIN my_vocab v ON v.expression_id = e.id
        WHERE (e.file_hash IS NOT NULL AND e.file_hash = %s)
           OR (e.file_hash IS NULL
               AND e.source_title = %s
               AND COALESCE(e.source_episode,'') = %s)
        ORDER BY e.source_time_s
    """
    with _conn() as cur:
        cur.execute(sql, (file_hash, source_title, source_episode))
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def update_source_info(file_hash: str, new_title: str, new_episode: str) -> None:
    """analyzed + 연결된 expressions의 source_title/source_episode 일괄 업데이트."""
    with _conn() as cur:
        cur.execute(
            "SELECT source_title, source_episode FROM analyzed WHERE file_hash = %s",
            (file_hash,),
        )
        row = cur.fetchone()
        if not row:
            return
        old_title = row["source_title"] or ""
        old_episode = row["source_episode"] or ""

        cur.execute(
            "UPDATE analyzed SET source_title=%s, source_episode=%s WHERE file_hash=%s",
            (new_title, new_episode, file_hash),
        )
        cur.execute(
            "UPDATE expressions SET source_title=%s, source_episode=%s WHERE file_hash=%s",
            (new_title, new_episode, file_hash),
        )
        cur.execute(
            """UPDATE expressions SET source_title=%s, source_episode=%s
               WHERE file_hash IS NULL
               AND source_title=%s
               AND COALESCE(source_episode,'')=%s""",
            (new_title, new_episode, old_title, old_episode),
        )


def delete_analyzed_entry(file_hash: str) -> tuple[int, int]:
    """analyzed 항목과 연결된 expressions 삭제. 반환: (삭제된 표현 수, ⭐ 수)"""
    with _conn() as cur:
        cur.execute(
            "SELECT source_title, source_episode FROM analyzed WHERE file_hash=%s",
            (file_hash,),
        )
        row = cur.fetchone()
        if not row:
            return 0, 0
        old_title = row["source_title"] or ""
        old_episode = row["source_episode"] or ""

        cur.execute("SELECT id FROM expressions WHERE file_hash=%s", (file_hash,))
        ids: set[int] = {r["id"] for r in cur.fetchall()}

        cur.execute(
            """SELECT id FROM expressions
               WHERE file_hash IS NULL
               AND source_title=%s
               AND COALESCE(source_episode,'')=%s""",
            (old_title, old_episode),
        )
        ids |= {r["id"] for r in cur.fetchall()}

        starred_count = 0
        if ids:
            id_list = list(ids)
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM my_vocab WHERE expression_id = ANY(%s)",
                (id_list,),
            )
            starred_count = cur.fetchone()["cnt"]
            cur.execute("DELETE FROM my_vocab WHERE expression_id = ANY(%s)", (id_list,))
            cur.execute("DELETE FROM expressions WHERE id = ANY(%s)", (id_list,))

        cur.execute("DELETE FROM analyzed WHERE file_hash=%s", (file_hash,))
    return len(ids), starred_count


def get_all_expressions(query: str = "", levels: list[str] | None = None) -> list[dict]:
    """전체 표현 검색+필터 (최대 200개). is_starred 포함."""
    params: list = []
    where: list[str] = []

    if query:
        where.append(
            "(e.text ILIKE %s OR e.ko ILIKE %s OR e.reading ILIKE %s OR e.source_title ILIKE %s)"
        )
        q = f"%{query}%"
        params += [q, q, q, q]

    if levels:
        where.append("e.level = ANY(%s)")
        params.append(levels)

    where_clause = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"""
        SELECT e.id, e.lang, e.text, e.reading, e.ko, e.level, e.type,
               e.usage_note, e.example_orig, e.example_ko,
               e.source_title, e.source_episode, e.source_time_s, e.seen_count,
               CASE WHEN v.id IS NOT NULL THEN 1 ELSE 0 END AS is_starred
        FROM expressions e
        LEFT JOIN my_vocab v ON v.expression_id = e.id
        {where_clause}
        ORDER BY e.source_title, COALESCE(e.source_episode, ''), e.source_time_s
        LIMIT 200
    """
    with _conn() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [dict(r) for r in rows]
