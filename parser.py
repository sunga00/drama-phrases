"""
Language Reactor Excel 자막 파일 파서.
시트: Subtitles, 열: Time / Subtitle / Human Translation / Transliteration
"""

import re
import pandas as pd


def _parse_time(raw: str) -> int:
    """Time 셀 값을 초 단위 정수로 변환. e.g. '20s'→20, '1:06'→66, '44:03'→2643"""
    s = str(raw).strip()
    if s.endswith("s"):
        return int(s[:-1])
    parts = s.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return 0


_STAGE_DIR = re.compile(r"^[（(【\[]")  # 전각/반각 괄호로 시작하는 무대 지문


def _is_stage_direction(text: str) -> bool:
    return bool(_STAGE_DIR.match(text.strip()))


def _normalize_reading(raw: str) -> str:
    """음차(병음 등) 셀의 다중 공백을 단일 공백으로 정규화."""
    return " ".join(raw.split())


def _normalize_ko(raw: str) -> str:
    """한국어 번역 셀의 줄바꿈을 공백으로 치환 후 연속 공백 정리."""
    return " ".join(raw.replace("\n", " ").split())


def parse_xlsx(path: str) -> list[dict]:
    """
    xlsx 파일을 파싱해 정제된 대사 리스트를 반환.

    반환 형식: [{"time_s": int, "text": str, "ko": str, "reading": str}, ...]
    """
    df = pd.read_excel(path, sheet_name="Subtitles", dtype=str)

    # 열 이름 공백 트림
    df.columns = [c.strip() for c in df.columns]

    required = {"Time", "Subtitle", "Human Translation", "Transliteration"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"필수 열 없음: {missing}")

    rows = []
    for _, row in df.iterrows():
        text = str(row.get("Subtitle", "")).strip()
        ko_raw = str(row.get("Human Translation", "")).strip()
        reading_raw = str(row.get("Transliteration", "")).strip()
        time_raw = str(row.get("Time", "")).strip()

        # 빈 행, NaN 행 제거
        if not text or text in ("nan", "NaN"):
            continue

        # 무대 지문 행 제거
        if _is_stage_direction(text):
            continue

        rows.append(
            {
                "time_s": _parse_time(time_raw),
                "text": text,
                "ko": _normalize_ko(ko_raw) if ko_raw not in ("nan", "NaN") else "",
                "reading": _normalize_reading(reading_raw) if reading_raw not in ("nan", "NaN") else "",
            }
        )

    return rows


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "투투장부주 1화.xlsx"
    result = parse_xlsx(path)
    print(f"총 {len(result)}행 파싱 완료\n")
    for entry in result[:10]:
        print(f"[{entry['time_s']:>5}s] {entry['text']}")
        print(f"       ko: {entry['ko']}")
        print(f"  reading: {entry['reading']}")
        print()
