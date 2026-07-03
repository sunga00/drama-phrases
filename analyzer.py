"""
Claude API로 자막 대사에서 실용 표현을 선별하는 분석기.
"""

import json
import os
import re

import anthropic
from dotenv import load_dotenv

from languages import LanguageProfile, get_profile

load_dotenv()

CHUNK_SIZE = 100
MODEL = "claude-sonnet-4-6"

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def _build_system_prompt(profile: LanguageProfile) -> str:
    return f"""\
너는 {profile.display_name} 학습 콘텐츠 큐레이터다. 드라마 자막 대사 목록에서
'일상 대화에서 그대로 쓸 수 있는 실용 표현'만 선별한다.

{profile.selection_rules}

각 표현에 부가할 정보:
- level: "기초" | "중급" | "고급"
- usage_note: 어떤 상황에서 쓰는지 한 줄 (한국어)
- example_orig: 드라마 밖 일상 응용 예문 ({profile.display_name})
- example_ko: 예문 한국어 번역

응답은 반드시 아래 JSON 배열만 출력. 마크다운 코드펜스 금지.
[{{"text":"...","reading":"...","ko":"...","level":"...",
  "usage_note":"...","example_orig":"...","example_ko":"...",
  "source_time_s":0}}]"""


def _build_user_message(chunk: list[dict]) -> str:
    lines = [f"{r['time_s']}|{r['text']}|{r['ko']}|{r['reading']}" for r in chunk]
    return "\n".join(lines)


def _parse_response(text: str) -> list[dict]:
    """코드펜스 제거 후 JSON 파싱. 실패 시 빈 리스트 반환."""
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", text).strip()
    try:
        result = json.loads(cleaned)
        return result if isinstance(result, list) else []
    except json.JSONDecodeError:
        return []


def _deduplicate(items: list[dict]) -> list[dict]:
    """text 기준 중복 제거 (먼저 등장한 것 유지)."""
    seen: set[str] = set()
    out = []
    for item in items:
        key = item.get("text", "").strip()
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def analyze(
    rows: list[dict],
    lang: str = "zh",
    progress_callback=None,
) -> list[dict]:
    """
    파싱된 대사 rows를 Claude API로 분석해 실용 표현 목록을 반환.

    progress_callback(current, total): 청크 진행률 콜백 (선택)
    """
    profile = get_profile(lang)
    client = _get_client()
    system_prompt = _build_system_prompt(profile)

    chunks = [rows[i : i + CHUNK_SIZE] for i in range(0, len(rows), CHUNK_SIZE)]
    total = len(chunks)
    all_results: list[dict] = []

    for idx, chunk in enumerate(chunks, 1):
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": _build_user_message(chunk)}],
        )
        raw = response.content[0].text
        results = _parse_response(raw)
        all_results.extend(results)

        if progress_callback:
            progress_callback(idx, total)

    return _deduplicate(all_results)


if __name__ == "__main__":
    import sys
    from parser import parse_xlsx

    path = sys.argv[1] if len(sys.argv) > 1 else "투투장부주 1화.xlsx"
    max_rows = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    lang = sys.argv[3] if len(sys.argv) > 3 else "zh"

    from languages import get_profile
    profile = get_profile(lang)

    print(f"파싱 중: {path} (최대 {max_rows}행, 언어: {profile.display_name})")
    rows = parse_xlsx(path)[:max_rows]
    print(f"  → {len(rows)}행 파싱 완료")

    chunk_count = (len(rows) + CHUNK_SIZE - 1) // CHUNK_SIZE
    print(f"  → {chunk_count}개 청크로 분석 시작\n")

    def progress(cur, total):
        print(f"  청크 {cur}/{total} 완료")

    results = analyze(rows, lang=lang, progress_callback=progress)

    reading_col = profile.reading_label or "reading"
    print(f"\n선별된 표현: {len(results)}개\n")
    print(f"{'번호':<4} {'표현':<22} {reading_col:<28} {'뜻':<20} {'난이도'}")
    print("-" * 90)
    for i, r in enumerate(results, 1):
        print(
            f"{i:<4} {r.get('text',''):<22} {r.get('reading',''):<28} "
            f"{r.get('ko',''):<20} {r.get('level','')}"
        )
    print()

    if results:
        print("=== 첫 번째 표현 상세 ===")
        r = results[0]
        print(f"  표현    : {r.get('text')}")
        if profile.reading_label:
            print(f"  {profile.reading_label:<6}: {r.get('reading')}")
        print(f"  뜻      : {r.get('ko')}")
        print(f"  난이도  : {r.get('level')}")
        print(f"  사용법  : {r.get('usage_note')}")
        print(f"  예문    : {r.get('example_orig')}")
        print(f"  예문(한): {r.get('example_ko')}")
        print(f"  출처    : {r.get('source_time_s')}초")
