import os
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

import tts
import vocab_db
from analyzer import analyze
from languages import PROFILES, get_profile
from parser import parse_xlsx

st.set_page_config(
    page_title="드라마 표현 추출기",
    page_icon="🎬",
    layout="wide",
)


def _check_auth() -> bool:
    """인증 여부 확인. 미인증 시 로그인 폼을 표시하고 False 반환."""
    if st.session_state.get("authenticated"):
        return True

    _, col, _ = st.columns([1, 1, 1])
    with col:
        st.write("")
        st.write("")
        st.markdown("## 🎬 드라마 표현 추출기")
        with st.form("login_form"):
            pw = st.text_input("비밀번호", type="password", placeholder="비밀번호를 입력하세요")
            ok = st.form_submit_button("입장", use_container_width=True, type="primary")
        if ok:
            expected = os.environ.get("APP_PASSWORD", "")
            if pw == expected:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("비밀번호가 틀렸습니다.")
    return False


if not _check_auth():
    st.stop()

vocab_db.init_db()

LEVEL_META = {
    "기초": "🟢",
    "중급": "🟡",
    "고급": "🔴",
}

TYPE_BADGE = {
    "표현": '<span style="background:#e8f4f8;color:#1a7ba4;padding:1px 7px;border-radius:4px;font-size:0.78em;font-weight:600">💬 표현</span>',
    "문형": '<span style="background:#fff3cd;color:#856404;padding:1px 7px;border-radius:4px;font-size:0.78em;font-weight:600">📝 문형</span>',
}

def _type_filter_widget(key: str) -> str:
    """전체 / 표현만 / 문형만 라디오. 선택값("전체"|"표현"|"문형") 반환."""
    choice = st.radio(
        "유형",
        ["전체", "💬 표현만", "📝 문형만"],
        horizontal=True,
        label_visibility="collapsed",
        key=key,
    )
    return {"전체": "전체", "💬 표현만": "표현", "📝 문형만": "문형"}[choice]


def _apply_type_filter(items: list[dict], type_sel: str) -> list[dict]:
    if type_sel == "전체":
        return items
    return [r for r in items if r.get("type", "표현") == type_sel]


def _level_filter_widget(key: str) -> str:
    """전체 / 기초 / 중급 / 고급 라디오. 선택값 반환."""
    return st.radio(
        "난이도",
        ["전체", "🟢 기초", "🟡 중급", "🔴 고급"],
        horizontal=True,
        label_visibility="collapsed",
        key=key,
    ).replace("🟢 ", "").replace("🟡 ", "").replace("🔴 ", "")


def _apply_level_filter(items: list[dict], level_sel: str) -> list[dict]:
    if level_sel == "전체":
        return items
    return [r for r in items if r.get("level", "") == level_sel]


def _filter_row(type_key: str, level_key: str, items: list[dict]) -> list[dict]:
    """유형+난이도 필터 위젯을 한 행으로 표시하고 필터링된 결과 반환."""
    col_type, col_level, col_cnt = st.columns([3, 3, 1])
    with col_type:
        type_sel = _type_filter_widget(type_key)
    with col_level:
        level_sel = _level_filter_widget(level_key)
    filtered = _apply_level_filter(_apply_type_filter(items, type_sel), level_sel)
    with col_cnt:
        if type_sel != "전체" or level_sel != "전체":
            st.caption(f"{len(filtered)} / {len(items)}")
    return filtered

# ── Session state 초기화 ────────────────────────────────────────
_defaults: dict = {
    "results": [],
    "lang": "zh",
    "source_title": "",
    "source_episode": "",
    "analyzed": False,
    "expr_ids": {},       # text -> expression_id
    "seen_counts": {},    # expression_id -> seen_count
    "starred": set(),     # starred expression_ids
    "audio_cache": {},    # expr_id -> mp3 bytes
    "lib_selected": None,      # int | None  — 열린 analyzed entry id
    "lib_edit_open": None,     # int | None  — 편집 중인 analyzed entry id
    "lib_delete_confirm": None, # int | None — 삭제 확인 대기 중인 entry id
}
for k, v in _defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

if not st.session_state.starred and vocab_db.vocab_count() > 0:
    st.session_state.starred = vocab_db.get_starred_ids()


# ── 콜백 ───────────────────────────────────────────────────────
def _toggle_star(expr_id: int) -> None:
    if expr_id in st.session_state.starred:
        vocab_db.unstar_expression(expr_id)
        st.session_state.starred.discard(expr_id)
    else:
        vocab_db.star_expression(expr_id)
        st.session_state.starred.add(expr_id)


def _toggle_lib_source(entry_id: int) -> None:
    if st.session_state.lib_selected == entry_id:
        st.session_state.lib_selected = None
    else:
        st.session_state.lib_selected = entry_id
        st.session_state.lib_edit_open = None
        st.session_state.lib_delete_confirm = None


def _open_lib_edit(entry_id: int) -> None:
    st.session_state.lib_edit_open = entry_id
    st.session_state.lib_delete_confirm = None


def _request_lib_delete(entry_id: int) -> None:
    st.session_state.lib_delete_confirm = entry_id
    st.session_state.lib_edit_open = None


def _load_audio(expr_id: int, text: str, lang: str, voice: str) -> None:
    try:
        st.session_state.audio_cache[expr_id] = tts.get_audio_bytes(text, lang, voice)
    except Exception:
        st.session_state.audio_cache[expr_id] = None


def _load_example_audio(cache_key: str, text: str, lang: str, voice: str) -> None:
    try:
        st.session_state.audio_cache[cache_key] = tts.get_audio_bytes(text, lang, voice)
    except Exception:
        st.session_state.audio_cache[cache_key] = None


# ── Export helpers ──────────────────────────────────────────────
def _results_to_df(results: list[dict]) -> pd.DataFrame:
    col_order = ["text", "reading", "ko", "level", "usage_note",
                 "example_orig", "example_ko", "source_time_s"]
    df = pd.DataFrame(results)
    return df[[c for c in col_order if c in df.columns]]


def _to_markdown(results: list[dict], title: str, episode: str, profile) -> str:
    rl = profile.reading_label
    lines = [f"# {title} {episode} — 실용 표현 목록\n"]
    rh = f"| {rl} " if rl else ""
    rs = "|----" if rl else ""
    lines += [f"| # | 표현 {rh}| 뜻 | 난이도 | 사용법 | 예문 |",
              f"|---|----{rs}|----|----|----|-----|"]
    for i, r in enumerate(results, 1):
        rc = f"| {r.get('reading', '')} " if rl else ""
        ex = f"{r.get('example_orig', '')} / {r.get('example_ko', '')}"
        lines.append(
            f"| {i} | {r.get('text', '')} {rc}"
            f"| {r.get('ko', '')} | {r.get('level', '')} "
            f"| {r.get('usage_note', '')} | {ex} |"
        )
    return "\n".join(lines)


def _to_anki_tsv(results: list[dict], profile, source_tag: str = "") -> str:
    """
    Anki 가져오기용 TSV 생성.
    앞면: 표현 (+ reading)  /  뒷면: 뜻 + 사용법 + 예문  /  태그: level source
    """
    tag = source_tag.replace(" ", "_") if source_tag else "drama"
    lines = ["#separator:Tab", "#html:true"]
    for r in results:
        text = r.get("text", "")
        reading = r.get("reading", "")
        ko = r.get("ko", "")
        level = r.get("level", "")
        usage = r.get("usage_note", "")
        ex_orig = r.get("example_orig", "")
        ex_ko = r.get("example_ko", "")

        front = text
        if reading and profile.reading_label:
            front += f"<br><small>{reading}</small>"

        back_parts = [f"<b>{ko}</b>"]
        if reading and profile.reading_label:
            back_parts.append(f"<small>{profile.reading_label}: {reading}</small>")
        if usage:
            back_parts.append(f"<br>📌 {usage}")
        if ex_orig:
            back_parts.append(f"<br><i>{ex_orig}</i>")
        if ex_ko:
            back_parts.append(f"→ {ex_ko}")
        back = "<br>".join(back_parts)

        tags = f"{level} {tag}"
        lines.append(f"{front.replace(chr(9), ' ')}\t{back.replace(chr(9), ' ')}\t{tags}")
    return "\n".join(lines)


# ── Card renderer ───────────────────────────────────────────────
def _render_card(
    idx: int, r: dict, profile, *,
    expr_id: int | None = None,
    seen_count: int | None = None,
    key_prefix: str = "",
) -> None:
    """
    표현 카드 렌더링.
    key_prefix: 탭 간 위젯 키 충돌 방지 ("an_" / "ls_" / "la_" 등)
    expr_id/seen_count: 라이브러리처럼 DB에서 직접 가져올 때 전달, 없으면 session_state 조회
    """
    text = r.get("text", "")
    level = r.get("level", "")
    emoji = LEVEL_META.get(level, "⚪")

    if expr_id is None:
        expr_id = st.session_state.expr_ids.get(text)
    seen = seen_count if seen_count is not None else (
        st.session_state.seen_counts.get(expr_id, 1) if expr_id else 1
    )
    is_starred = expr_id in st.session_state.starred if expr_id else False

    type_badge = TYPE_BADGE.get(r.get("type", "표현"), TYPE_BADGE["표현"])

    with st.container(border=True):
        col_text, col_badge = st.columns([6, 1])
        with col_text:
            header = f"**{idx}. &nbsp; {text}**"
            if seen > 1:
                header += f' &nbsp; <span style="font-size:0.8em;color:#888">{seen}번째 등장 🔄</span>'
            st.markdown(header, unsafe_allow_html=True)
            if profile.reading_label and r.get("reading"):
                st.caption(f"{profile.reading_label}: {r['reading']}")
            st.write(r.get("ko", ""))
        with col_badge:
            st.markdown(f"<br>{type_badge}<br>{emoji} {level}", unsafe_allow_html=True)

        with st.expander("사용법 & 예문 보기"):
            st.info(r.get("usage_note", ""), icon="📌")
            c1, c2 = st.columns(2)
            example_orig = r.get("example_orig", "")
            with c1:
                st.markdown(f"**{profile.display_name}**  \n{example_orig}")
                if expr_id and example_orig:
                    ex_key = f"ex_{expr_id}"
                    ex_audio = st.session_state.audio_cache.get(ex_key)
                    if ex_audio:
                        st.audio(ex_audio, format="audio/mp3")
                    elif ex_audio is None and ex_key in st.session_state.audio_cache:
                        st.caption("⚠️ 발음 로드 실패")
                    else:
                        st.button(
                            "▶️ 예문 발음",
                            key=f"{key_prefix}ex_tts_{expr_id}",
                            on_click=_load_example_audio,
                            args=(ex_key, example_orig, profile.code, profile.tts_voice),
                        )
            c2.markdown(f"**한국어**  \n{r.get('example_ko', '')}")

        if expr_id:
            col_star, col_tts = st.columns([1, 1])
            with col_star:
                star_label = "⭐ 저장됨" if is_starred else "☆ 단어장에 저장"
                st.button(star_label, key=f"{key_prefix}star_{expr_id}",
                          on_click=_toggle_star, args=(expr_id,))
            with col_tts:
                audio_data = st.session_state.audio_cache.get(expr_id)
                if audio_data:
                    st.audio(audio_data, format="audio/mp3")
                elif audio_data is None and expr_id in st.session_state.audio_cache:
                    st.caption("⚠️ 발음 로드 실패")
                else:
                    st.button("▶️ 발음 듣기", key=f"{key_prefix}tts_{expr_id}",
                              on_click=_load_audio,
                              args=(expr_id, text, profile.code, profile.tts_voice))


# ── Sidebar ────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ 설정")
    lang_code = st.selectbox(
        "언어", list(PROFILES.keys()),
        format_func=lambda x: PROFILES[x].display_name,
    )
    source_title = st.text_input("작품명", placeholder="예: 투투장부주")
    source_episode = st.text_input(
        "화수", placeholder="예: 1화",
        help="복수 파일 업로드 시 파일명이 화수로 자동 사용됩니다.",
    )
    min_level = st.radio(
        "최소 난이도",
        ["기초부터 전부", "중급 이상만", "고급만"],
        help="선택 난이도 미만 표현은 다음 분석부터 제외됩니다.",
    )
    st.divider()
    st.metric("내 단어장", f"{vocab_db.vocab_count()}개")


# ── 탭 레이아웃 ────────────────────────────────────────────────
st.title("🎬 드라마 표현 추출기")
tab_analyze, tab_vocab, tab_lib = st.tabs(["🔍 분석", "⭐ 내 단어장", "📚 라이브러리"])


# ════════════════════════════════════════════════════════════════
# 탭 1: 분석 (복수 파일 일괄 처리)
# ════════════════════════════════════════════════════════════════
with tab_analyze:
    st.caption("Language Reactor 자막 파일에서 실용 표현을 AI로 선별합니다. 복수 파일을 한번에 업로드할 수 있습니다.")

    uploaded_files = st.file_uploader(
        "자막 파일 업로드 (.xlsx, 복수 선택 가능)",
        type=["xlsx"],
        accept_multiple_files=True,
    )

    if uploaded_files:
        # 각 파일의 해시 및 분석 여부 확인
        file_data = []
        for f in uploaded_files:
            fb = f.getvalue()
            fh = vocab_db.file_md5(fb)
            file_data.append({
                "name": f.name,
                "bytes": fb,
                "hash": fh,
                "already": vocab_db.is_already_analyzed(fh),
                "episode": source_episode if len(uploaded_files) == 1 else Path(f.name).stem,
            })

        new_files = [fd for fd in file_data if not fd["already"]]
        done_files = [fd for fd in file_data if fd["already"]]

        if done_files:
            st.info(
                f"🗂️ 이미 분석된 파일 {len(done_files)}개 건너뜀: "
                + ", ".join(f"**{fd['name']}**" for fd in done_files),
            )

        if not new_files:
            st.warning("업로드된 모든 파일이 이미 분석됐습니다. **내 단어장** 탭을 확인하세요.")
        else:
            btn_label = (f"🔍 {len(new_files)}개 파일 일괄 분석 시작"
                         if len(new_files) > 1 else "🔍 분석 시작")
            if st.button(btn_label, type="primary", use_container_width=True):
                all_results: list[dict] = []
                all_expr_ids: dict[str, int] = {}
                all_seen_counts: dict[int, int] = {}

                for fi, fd in enumerate(new_files, 1):
                    if len(new_files) > 1:
                        st.markdown(f"**[{fi}/{len(new_files)}] {fd['name']}**")

                    # 파싱
                    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
                        tmp.write(fd["bytes"])
                        tmp_path = tmp.name
                    try:
                        with st.spinner("파싱 중..."):
                            rows = parse_xlsx(tmp_path)
                    finally:
                        os.unlink(tmp_path)

                    chunk_total = (len(rows) + 99) // 100
                    st.caption(f"{len(rows)}행 → {chunk_total}청크")

                    pb = st.progress(0.0)
                    sp = st.empty()

                    def _make_cb(pb=pb, sp=sp):
                        def cb(cur, total):
                            pb.progress(cur / total)
                            sp.text(f"청크 {cur}/{total} 완료...")
                        return cb

                    results = analyze(rows, lang=lang_code, progress_callback=_make_cb(), min_level=min_level)
                    pb.progress(1.0)
                    sp.success(f"✅ {len(results)}개 선별됨")

                    mapping = vocab_db.save_analysis(
                        fd["hash"], source_title, fd["episode"], lang_code, results
                    )
                    all_results.extend(results)
                    all_expr_ids.update({t: ids[0] for t, ids in mapping.items()})
                    all_seen_counts.update({ids[0]: ids[1] for ids in mapping.values()})

                st.session_state.results = all_results
                st.session_state.lang = lang_code
                st.session_state.source_title = source_title
                st.session_state.source_episode = source_episode
                st.session_state.analyzed = True
                st.session_state.expr_ids = all_expr_ids
                st.session_state.seen_counts = all_seen_counts
                st.session_state.starred = vocab_db.get_starred_ids()

    # ── 결과 카드 ───────────────────────────────────────────────
    if st.session_state.analyzed and st.session_state.results:
        results = st.session_state.results
        profile = get_profile(st.session_state.lang)
        title_str = st.session_state.source_title or "드라마"
        ep_str = st.session_state.source_episode or ""
        label = f"{title_str} {ep_str}".strip()

        st.divider()
        st.subheader(f"📋 {label} — 선별된 표현 {len(results)}개")

        col_csv, col_md, col_anki, _ = st.columns([1, 1, 1, 3])
        df = _results_to_df(results)
        with col_csv:
            st.download_button(
                "⬇️ CSV",
                df.to_csv(index=False).encode("utf-8-sig"),
                f"{label}.csv", "text/csv",
            )
        with col_md:
            st.download_button(
                "⬇️ Markdown",
                _to_markdown(results, title_str, ep_str, profile).encode("utf-8"),
                f"{label}.md", "text/markdown",
            )
        with col_anki:
            st.download_button(
                "⬇️ Anki",
                _to_anki_tsv(results, profile, title_str).encode("utf-8"),
                f"{label}.txt", "text/plain",
                help="Anki → 파일 가져오기 → 필드 구분자: 탭",
            )

        filtered_results = _filter_row("an_type_filter", "an_level_filter", results)

        for i, r in enumerate(filtered_results, 1):
            _render_card(i, r, profile, key_prefix="an_")


# ════════════════════════════════════════════════════════════════
# 탭 2: 내 단어장
# ════════════════════════════════════════════════════════════════
with tab_vocab:
    _all_vocab = vocab_db.get_all_vocab("recent")

    # 언어 셀렉터 — 저장된 표현이 있는 언어만 표시
    _LANG_FLAG = {"zh": "🇨🇳", "en": "🇺🇸", "ja": "🇯🇵"}
    _vocab_langs = sorted(set(r["lang"] for r in _all_vocab if r.get("lang")),
                          key=lambda l: ["zh", "ja", "en"].index(l) if l in ["zh", "ja", "en"] else 99)
    if len(_vocab_langs) > 1:
        _vlang_labels = [f"{_LANG_FLAG.get(l,'🌐')} {PROFILES.get(l, PROFILES['zh']).display_name}"
                         for l in _vocab_langs]
        _vsel_label = st.radio("언어", _vlang_labels, horizontal=True,
                               label_visibility="collapsed", key="vocab_lang_sel")
        _vsel_lang = _vocab_langs[_vlang_labels.index(_vsel_label)]
    else:
        _vsel_lang = _vocab_langs[0] if _vocab_langs else "zh"

    col_search, col_sort = st.columns([3, 1])
    with col_search:
        query = st.text_input("🔍 검색", placeholder="표현, 뜻, 병음으로 검색...")
    with col_sort:
        sort_opt = st.selectbox(
            "정렬", ["recent", "seen", "level"],
            format_func=lambda x: {
                "recent": "최근 저장순", "seen": "등장 많은 순", "level": "난이도순"
            }[x],
        )

    _base = vocab_db.search_vocab(query) if query else vocab_db.get_all_vocab(sort_opt)
    vocab = [r for r in _base if r.get("lang") == _vsel_lang]

    if not vocab:
        if not _all_vocab:
            st.info("저장된 표현이 없습니다. 분석 결과에서 ☆ 버튼을 클릭해 저장하세요.", icon="⭐")
        else:
            st.info("이 언어로 저장된 표현이 없습니다.", icon="⭐")
    else:
        head_col, anki_col = st.columns([3, 1])
        with head_col:
            st.caption(f"총 {len(vocab)}개 저장됨")
        with anki_col:
            # 단어장 전체 Anki 내보내기
            v_profile = get_profile(st.session_state.lang)
            st.download_button(
                "⬇️ Anki (전체)",
                _to_anki_tsv(vocab, v_profile, "my_vocab").encode("utf-8"),
                "my_vocab.txt", "text/plain",
                help="단어장 전체를 Anki로 내보냅니다.",
            )

        display_df = pd.DataFrame(vocab)[
            ["text", "reading", "ko", "level", "seen_count", "source_title"]
        ].rename(columns={
            "text": "표현", "reading": "병음", "ko": "뜻",
            "level": "난이도", "seen_count": "등장 횟수", "source_title": "출처",
        })
        st.dataframe(display_df, use_container_width=True, hide_index=True)

        st.write("")
        for row in vocab:
            with st.expander(f"⭐ {row['text']} — {row['ko']}"):
                c1, c2, c3 = st.columns(3)
                c1.metric("난이도", row.get("level", ""))
                c2.metric("등장 횟수", row.get("seen_count", 1))
                c3.metric("출처", row.get("source_title", ""))
                st.markdown(f"📌 {row.get('usage_note', '')}")
                st.markdown(
                    f"**예문** {row.get('example_orig', '')}  \n"
                    f"**번역** {row.get('example_ko', '')}"
                )
                col_del, col_tts_v = st.columns([1, 1])
                with col_del:
                    if st.button("🗑️ 단어장에서 삭제", key=f"del_{row['id']}"):
                        vocab_db.unstar_expression(row["id"])
                        st.session_state.starred.discard(row["id"])
                        st.rerun()
                with col_tts_v:
                    row_lang = row.get("lang", "zh")
                    row_voice = PROFILES.get(row_lang, PROFILES["zh"]).tts_voice
                    audio_data = st.session_state.audio_cache.get(row["id"])
                    if audio_data:
                        st.audio(audio_data, format="audio/mp3")
                    elif audio_data is None and row["id"] in st.session_state.audio_cache:
                        st.caption("⚠️ 발음 로드 실패")
                    else:
                        st.button("▶️ 발음 듣기", key=f"tts_v_{row['id']}",
                                  on_click=_load_audio,
                                  args=(row["id"], row.get("text", ""), row_lang, row_voice))


# ════════════════════════════════════════════════════════════════
# 탭 3: 라이브러리
# ════════════════════════════════════════════════════════════════

def _source_display(title: str, episode: str) -> str:
    """source_title/source_episode가 없을 때 대체 표시명 반환."""
    title = (title or "").strip()
    episode = (episode or "").strip()
    if title and episode:
        return f"{title} > {episode}"
    return title or (f"({episode})" if episode else "(제목 없음)")


with tab_lib:
    view_mode = st.radio(
        "보기 방식",
        ["🎬 작품별", "🔍 전체 검색"],
        horizontal=True,
        label_visibility="collapsed",
    )
    st.write("")

    # ── 작품별 보기 ─────────────────────────────────────────────
    if view_mode == "🎬 작품별":
        analyzed_list = vocab_db.get_analyzed_list()

        if not analyzed_list:
            st.info("아직 분석된 파일이 없습니다. 분석 탭에서 자막 파일을 업로드해 주세요.", icon="📂")
        else:
            # 언어 셀렉터 — DB에 있는 언어만 동적 표시
            _LANG_FLAG = {"zh": "🇨🇳", "en": "🇺🇸", "ja": "🇯🇵"}
            langs_available = sorted(set(e["lang"] or "zh" for e in analyzed_list),
                                     key=lambda l: ["zh", "ja", "en"].index(l) if l in ["zh","ja","en"] else 99)
            lang_labels = [f"{_LANG_FLAG.get(l,'🌐')} {PROFILES.get(l, PROFILES['zh']).display_name}" for l in langs_available]
            if len(langs_available) > 1:
                sel_label = st.radio("언어", lang_labels, horizontal=True, label_visibility="collapsed", key="lib_lang_sel")
                sel_lang = langs_available[lang_labels.index(sel_label)]
            else:
                sel_lang = langs_available[0]
            st.write("")

            for entry in [e for e in analyzed_list if (e["lang"] or "zh") == sel_lang]:
                eid = entry["id"]
                e_hash = entry["file_hash"]
                e_title = entry["source_title"] or ""
                e_episode = entry["source_episode"] or ""
                e_lang = entry["lang"] or "zh"
                e_count = entry["expr_count"]
                e_date = (entry["analyzed_at"] or "")[:10]
                e_profile = PROFILES.get(e_lang, PROFILES["zh"])
                e_display = _source_display(e_title, e_episode)

                is_open = st.session_state.lib_selected == eid
                is_editing = st.session_state.lib_edit_open == eid
                is_deleting = st.session_state.lib_delete_confirm == eid

                with st.container(border=True):
                    # ── 헤더 행 ──────────────────────────────
                    col_info, col_edit_btn, col_del_btn, col_view_btn = st.columns([5, 1, 1, 1])
                    with col_info:
                        st.markdown(
                            f"**{e_display}** &nbsp;"
                            f"<span style='color:#888;font-size:0.85em'>"
                            f"{e_profile.display_name} · {e_count}개 · {e_date}"
                            f"</span>",
                            unsafe_allow_html=True,
                        )
                    with col_edit_btn:
                        st.button(
                            "✏️",
                            key=f"edit_btn_{eid}",
                            help="작품명·화수 수정",
                            on_click=_open_lib_edit,
                            args=(eid,),
                        )
                    with col_del_btn:
                        st.button(
                            "🗑️",
                            key=f"del_btn_{eid}",
                            help=f"이 항목과 표현 {e_count}개 삭제",
                            on_click=_request_lib_delete,
                            args=(eid,),
                        )
                    with col_view_btn:
                        st.button(
                            "▲ 접기" if is_open else "▼ 열기",
                            key=f"view_btn_{eid}",
                            on_click=_toggle_lib_source,
                            args=(eid,),
                        )

                    # ── 편집 폼 ──────────────────────────────
                    if is_editing:
                        st.divider()
                        with st.form(key=f"edit_form_{eid}"):
                            new_title = st.text_input("작품명", value=e_title)
                            new_episode = st.text_input("화수", value=e_episode)
                            col_s, col_c = st.columns(2)
                            save_clicked = col_s.form_submit_button(
                                "💾 저장", type="primary", use_container_width=True
                            )
                            cancel_clicked = col_c.form_submit_button(
                                "취소", use_container_width=True
                            )
                        if save_clicked:
                            vocab_db.update_source_info(
                                e_hash, new_title.strip(), new_episode.strip()
                            )
                            st.session_state.lib_edit_open = None
                            st.rerun()
                        if cancel_clicked:
                            st.session_state.lib_edit_open = None
                            st.rerun()

                    # ── 삭제 확인 ────────────────────────────
                    elif is_deleting:
                        st.divider()
                        starred_preview = sum(
                            1 for _ in vocab_db.get_expressions_by_source(
                                e_title, e_episode, e_hash
                            ) if _["is_starred"]
                        )
                        warn_msg = (
                            f"⚠️ **{e_display}** 항목과 표현 **{e_count}개**를 모두 삭제합니다."
                        )
                        if starred_preview:
                            warn_msg += f" (⭐ {starred_preview}개 포함)"
                        st.warning(warn_msg)
                        col_cf, col_ca = st.columns(2)
                        if col_cf.button(
                            "🗑️ 삭제 확인", key=f"del_cf_{eid}",
                            type="primary", use_container_width=True,
                        ):
                            vocab_db.delete_analyzed_entry(e_hash)
                            if st.session_state.lib_selected == eid:
                                st.session_state.lib_selected = None
                            st.session_state.lib_delete_confirm = None
                            st.session_state.starred = vocab_db.get_starred_ids()
                            st.rerun()
                        if col_ca.button(
                            "취소", key=f"del_ca_{eid}", use_container_width=True
                        ):
                            st.session_state.lib_delete_confirm = None
                            st.rerun()

                    # ── 표현 카드 목록 ────────────────────────
                    elif is_open:
                        st.divider()
                        exprs = vocab_db.get_expressions_by_source(e_title, e_episode, e_hash)
                        if not exprs:
                            st.caption("저장된 표현이 없습니다.")
                        else:
                            col_dl_csv, col_dl_anki = st.columns([1, 1])
                            with col_dl_csv:
                                st.download_button(
                                    "⬇️ CSV",
                                    _results_to_df(exprs).to_csv(index=False).encode("utf-8-sig"),
                                    f"{e_display.replace(' > ', '_')}.csv",
                                    "text/csv",
                                    key=f"dl_csv_{eid}",
                                )
                            with col_dl_anki:
                                st.download_button(
                                    "⬇️ Anki",
                                    _to_anki_tsv(exprs, e_profile, e_title).encode("utf-8"),
                                    f"{e_display.replace(' > ', '_')}.txt",
                                    "text/plain",
                                    key=f"dl_anki_{eid}",
                                )
                            filtered_exprs = _filter_row(f"ls_type_{eid}", f"ls_lv_{eid}", exprs)
                            st.write("")
                            for i, expr in enumerate(filtered_exprs, 1):
                                _render_card(
                                    i, expr, e_profile,
                                    expr_id=expr["id"],
                                    seen_count=expr["seen_count"],
                                    key_prefix=f"ls{eid}_",
                                )

    # ── 전체 검색 보기 ───────────────────────────────────────────
    else:
        col_q, col_lv, col_tf3 = st.columns([3, 2, 2])
        with col_q:
            lib_query = st.text_input(
                "검색어", placeholder="표현, 뜻, 병음, 작품명으로 검색...",
                label_visibility="collapsed",
            )
        with col_lv:
            lib_levels = st.multiselect(
                "난이도",
                ["기초", "중급", "고급"],
                placeholder="난이도 필터 (복수 선택 가능)",
                label_visibility="collapsed",
            )
        with col_tf3:
            la_type_sel = _type_filter_widget("la_type_filter")

        if not lib_query and not lib_levels:
            st.caption("검색어를 입력하거나 난이도를 선택하면 결과가 표시됩니다.")
        else:
            all_exprs = vocab_db.get_all_expressions(lib_query, lib_levels or None)
            all_exprs = _apply_type_filter(all_exprs, la_type_sel)

            if not all_exprs:
                st.info("검색 결과가 없습니다.")
            else:
                result_note = f"**{len(all_exprs)}개**"
                if len(all_exprs) == 200:
                    result_note += " (최대 200개 표시 — 검색어로 범위를 좁혀주세요)"
                st.caption(result_note)
                st.write("")

                for i, expr in enumerate(all_exprs, 1):
                    e_lang = expr.get("lang", "zh")
                    e_profile = PROFILES.get(e_lang, PROFILES["zh"])
                    _render_card(
                        i, expr, e_profile,
                        expr_id=expr["id"],
                        seen_count=expr["seen_count"],
                        key_prefix=f"la{expr['id']}_",
                    )
