"""
SQLite(vocab.db) → Supabase Postgres 전체 데이터 이전 스크립트.
실행: .venv/bin/python migrate_to_supabase.py
"""

import os
import sqlite3
import sys

import psycopg2
import psycopg2.extras

SQLITE_PATH = "vocab.db"
TABLES = ["analyzed", "expressions", "my_vocab"]


def sqlite_conn():
    con = sqlite3.connect(SQLITE_PATH)
    con.row_factory = sqlite3.Row
    return con


def pg_conn():
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        sys.exit("❌ SUPABASE_DB_URL 환경변수가 설정되지 않았습니다.")
    return psycopg2.connect(dsn, connect_timeout=10)


def migrate():
    sq = sqlite_conn()
    pg = pg_conn()
    pgc = pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # ── SQLite 현황 ────────────────────────────────────────────
    print("=== SQLite 원본 행 수 ===")
    sq_counts = {}
    for tbl in TABLES:
        n = sq.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        sq_counts[tbl] = n
        print(f"  {tbl:<15} {n:>4}행")

    # ── Supabase 기존 데이터 확인 ──────────────────────────────
    print("\n=== Supabase 현재 행 수 (이전 전) ===")
    for tbl in TABLES:
        pgc.execute(f"SELECT COUNT(*) AS cnt FROM {tbl}")
        n = pgc.fetchone()["cnt"]
        print(f"  {tbl:<15} {n:>4}행")

    print("\n데이터 이전 시작...\n")

    # ── 1. analyzed ────────────────────────────────────────────
    rows = sq.execute("SELECT * FROM analyzed ORDER BY id").fetchall()
    for r in rows:
        pgc.execute(
            """INSERT INTO analyzed (id, file_hash, source_title, source_episode, lang, analyzed_at)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (file_hash) DO NOTHING""",
            (r["id"], r["file_hash"], r["source_title"],
             r["source_episode"], r["lang"], r["analyzed_at"]),
        )
    pg.commit()
    print(f"  ✅ analyzed   {len(rows)}행 이전 완료")

    # ── 2. expressions ─────────────────────────────────────────
    rows = sq.execute("SELECT * FROM expressions ORDER BY id").fetchall()
    for r in rows:
        pgc.execute(
            """INSERT INTO expressions
                (id, lang, text, reading, ko, level, usage_note,
                 example_orig, example_ko, source_title, source_episode,
                 source_time_s, seen_count, created_at, file_hash, type)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (lang, text) DO NOTHING""",
            (
                r["id"], r["lang"], r["text"], r["reading"], r["ko"], r["level"],
                r["usage_note"], r["example_orig"], r["example_ko"],
                r["source_title"], r["source_episode"], r["source_time_s"],
                r["seen_count"], r["created_at"], r["file_hash"], r["type"],
            ),
        )
    pg.commit()
    print(f"  ✅ expressions {len(rows)}행 이전 완료")

    # ── 3. my_vocab ────────────────────────────────────────────
    rows = sq.execute("SELECT * FROM my_vocab ORDER BY id").fetchall()
    for r in rows:
        pgc.execute(
            """INSERT INTO my_vocab (id, expression_id, starred_at, memo)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (expression_id) DO NOTHING""",
            (r["id"], r["expression_id"], r["starred_at"], r["memo"]),
        )
    pg.commit()
    print(f"  ✅ my_vocab   {len(rows)}행 이전 완료")

    # ── 시퀀스 리셋 (새 행 삽입 시 ID 충돌 방지) ──────────────
    for tbl in TABLES:
        pgc.execute(
            f"SELECT setval(pg_get_serial_sequence('{tbl}', 'id'), "
            f"COALESCE((SELECT MAX(id) FROM {tbl}), 1))"
        )
    pg.commit()
    print("\n  ✅ 시퀀스 리셋 완료")

    # ── 대조 검증 ──────────────────────────────────────────────
    print("\n=== 행 수 대조 결과 ===")
    print(f"  {'테이블':<15} {'SQLite':>8} {'Supabase':>10} {'일치':>6}")
    print("  " + "─" * 42)
    all_ok = True
    for tbl in TABLES:
        sq_n = sq_counts[tbl]
        pgc.execute(f"SELECT COUNT(*) AS cnt FROM {tbl}")
        pg_n = pgc.fetchone()["cnt"]
        ok = "✅" if sq_n == pg_n else "❌"
        if sq_n != pg_n:
            all_ok = False
        print(f"  {tbl:<15} {sq_n:>8} {pg_n:>10} {ok:>6}")

    print()
    if all_ok:
        print("✅ 모든 테이블 행 수 일치 — 이전 성공")
    else:
        print("❌ 불일치 테이블이 있습니다. 위 결과를 확인하세요.")

    sq.close()
    pgc.close()
    pg.close()


if __name__ == "__main__":
    migrate()
