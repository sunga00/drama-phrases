"""
언어 프로필 정의. 새 언어 추가 = PROFILES에 항목 1개 추가.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class LanguageProfile:
    code: str                  # "zh", "ja", "en"
    display_name: str
    reading_label: str         # "병음", "후리가나", ""
    tts_voice: str             # edge-tts voice ID
    selection_rules: str       # analyzer 시스템 프롬프트에 삽입될 언어별 규칙


PROFILES: dict[str, LanguageProfile] = {
    "zh": LanguageProfile(
        code="zh",
        display_name="중국어 (简体)",
        reading_label="병음",
        tts_voice="zh-CN-XiaoxiaoNeural",
        selection_rules="""\
선별 기준:
- 포함: 인사/리액션/요청/거절/감탄/동의 등 범용 회화 표현 (예: 我来吧, 不急, 说不定啊)
- 제외: 사극체·시대 어휘(丫头片子, 老天爷 등), 고유명사, 줄거리 종속 문장,
  욕설·비하 표현, 사용 빈도 낮은 관용구
- 같은 표현의 변형(好/好嘞)은 대표형 1개만
- level 기준: "기초"=HSK 1-2, "중급"=HSK 3-4, "고급"=HSK 5-6\
""",
    ),
    "ja": LanguageProfile(
        code="ja",
        display_name="일본어",
        reading_label="후리가나",
        tts_voice="ja-JP-NanamiNeural",
        selection_rules="""\
선별 기준:
- 포함: 일상 대화에서 그대로 쓸 수 있는 표현 (인사/리액션/요청/거절/감탄/동의 등)
- 제외: 고어·문어체, 고유명사, 줄거리 종속 문장, 욕설, 사용 빈도 낮은 관용구
- 같은 표현의 경어 변형(ありがとう/ありがとうございます)은 대표형 1개만
- level 기준: "기초"=JLPT N5-N4, "중급"=N3, "고급"=N2-N1
- usage_note에 반드시 경어 레벨(반말체/보통체/정중체 중 하나) 명시
- reading 필드: 히라가나 독음 (한자 포함 표현만; 순수 히라가나는 빈 문자열)\
""",
    ),
    "en": LanguageProfile(
        code="en",
        display_name="영어",
        reading_label="",
        tts_voice="en-US-JennyNeural",
        selection_rules="""\
선별 기준:
- 포함: 일상 대화에서 그대로 쓸 수 있는 구어체 표현 (인사/리액션/요청/관용구/감탄 등)
- 제외: 격식체·문어체, 고유명사, 줄거리 종속 문장, 비속어
- 같은 표현의 변형(Sure/Sure thing)은 대표형 1개만
- level 기준: "기초"=CEFR A1-A2 (중학 수준), "중급"=B1-B2 (고교 수준), "고급"=C1-C2 (대학 수준)
- reading 필드: 빈 문자열 (영어는 음차 표기 불필요)\
""",
    ),
}


def get_profile(code: str) -> LanguageProfile:
    if code not in PROFILES:
        raise ValueError(f"지원하지 않는 언어: {code}. 사용 가능: {list(PROFILES)}")
    return PROFILES[code]
