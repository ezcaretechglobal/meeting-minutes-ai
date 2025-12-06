import streamlit as st
import google.generativeai as genai
from datetime import datetime
import sqlite3
import pandas as pd
import os
import time
import io
import wave
from streamlit_mic_recorder import mic_recorder

# ==========================================
# 1. 설정 및 데이터베이스 초기화
# ==========================================

st.set_page_config(page_title="AI 회의록 비서 (Final)", layout="wide")

conn = sqlite3.connect("meeting_history_v2.db", check_same_thread=False)
c = conn.cursor()

c.execute("""
CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT,
    title TEXT,
    script TEXT,
    summary TEXT,
    audio_blob BLOB
)
""")
conn.commit()

# ==========================================
# 2. 헬퍼 함수
# ==========================================

def merge_audio_bytes(audio_chunks):
    if not audio_chunks:
        return None

    output = io.BytesIO()
    first_chunk = io.BytesIO(audio_chunks[0])

    with wave.open(first_chunk, "rb") as w:
        params = w.getparams()

    with wave.open(output, "wb") as out:
        out.setparams(params)
        for chunk in audio_chunks:
            with wave.open(io.BytesIO(chunk), "rb") as w:
                out.writeframes(w.readframes(w.getnframes()))

    return output.getvalue()


def transcribe_audio_segment(audio_bytes, api_key):
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.5-flash")

    tmp = f"temp_{int(time.time())}.wav"
    with open(tmp, "wb") as f:
        f.write(audio_bytes)

    try:
        audio = genai.upload_file(path=tmp)
        while audio.state.name == "PROCESSING":
            time.sleep(0.2)
            audio = genai.get_file(audio.name)

        res = model.generate_content(
            [audio, "이 오디오를 한국어(영어/아랍어 포함 가능)로 정확히 받아적어. 설명 없이 텍스트만 출력해."]
        )
        return res.text
    except Exception as e:
        return f"(STT 오류: {e})"
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def generate_final_report(script, api_key):
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-2.5-flash')
    
    # 사용자 요청 프롬프트 적용
    SUMMARY_PROMPT = """
    # 역할 (Role)
    너는 ‘회의록 정리 전문 GPT’이다.
    내가 제공하는 스크립트를 기반으로 회의록을 작성한다.

    # 회의록 템플릿 (Template)
    ## 1. 회의 개요
    1. 날짜: (오늘 날짜)
    2. 주요 의제: (내용 기반 추론)
    3. 추정 참석자: (내용 기반 추론)

    ## 2. 회의 내용 요약
    1) 주요 이슈 및 논의사항
       - 주제별로 그룹화하여 정리
       - **중요 발언 인용**: | [00:00] 화자 : "원문 텍스트" (타임스탬프는 추정)

    ## 3. 주요 결정 사항
    - (명확히 합의된 내용)

    ## 4. 향후 실행 계획 (Action Items)
    - 과제 (기한) - 담당자
    """
    
    prompt = f"""
    아래 스크립트를 바탕으로 완벽한 회의록을 작성해.
    
    [전체 스크립트]
    {full_script}
    
    {SUMMARY_PROMPT}
    """
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"회의록 생성 실패: {e}"

def save_to_db(title, script, summary, audio_blob):
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    # Binary 데이터를 DB에 저장
    c.execute("INSERT INTO meetings (date, title, script, summary, audio_blob) VALUES (?, ?, ?, ?, ?)",
              (date_str, title, script, summary, audio_blob))
    conn.commit()

def update_db(id, title, script, summary):
    c.execute("UPDATE meetings SET title=?, script=?, summary=? WHERE id=?", (title, script, summary, id))
    conn.commit()

# ==========================================
# 3. UI
# ==========================================

st.sidebar.title("🗂️ 구글 AI 회의 비서")
api_key = st.sidebar.text_input("Google API Key", type="password")
menu = st.sidebar.radio("메뉴", ["🔴 실시간 회의 (Live)", "📂 파일 업로드", "🗄️ 회의 기록"])

# ==========================================
# 🔴 Live (3초 자동 준실시간)
# ==========================================

if menu == "🔴 실시간 회의 (Live)":
    st.title("🔴 실시간 회의 (3초 자동 받아쓰기)")

    if not api_key:
        st.warning("API Key를 입력해주세요.")
        st.stop()

    if "is_recording" not in st.session_state:
        st.session_state.is_recording = False
    if "live_script" not in st.session_state:
        st.session_state.live_script = []
    if "audio_chunks" not in st.session_state:
        st.session_state.audio_chunks = []
    if "mic_key" not in st.session_state:
        st.session_state.mic_key = 0

    c1, c2 = st.columns(2)
    with c1:
        if st.button("▶️ 회의 시작"):
            st.session_state.is_recording = True
    with c2:
        if st.button("⏹️ 녹음 중지"):
            st.session_state.is_recording = False

    st.divider()

    if st.session_state.is_recording:
        st.info("🎧 듣는 중… (3초마다 자동 인식)")

        audio = mic_recorder(
            record_seconds=3,
            format="wav",
            key=f"mic_{st.session_state.mic_key}",
        )

        if audio and audio.get("bytes"):
            st.session_state.mic_key += 1
            st.session_state.audio_chunks.append(audio["bytes"])

            with st.spinner("✍️ 받아적는 중..."):
                text = transcribe_audio_segment(audio["bytes"], api_key)

            ts = datetime.now().strftime("%H:%M:%S")
            st.session_state.live_script.append(f"[{ts}] {text}")

            st.rerun()

    st.subheader("📜 실시간 스크립트")
    st.text_area(
        "Transcript",
        "\n\n".join(st.session_state.live_script),
        height=400,
        disabled=True,
    )

    st.caption(f"녹음된 오디오 조각: {len(st.session_state.audio_chunks)}개")

    st.divider()

    if st.button("💾 회의 종료 및 저장", type="primary"):
        if not st.session_state.live_script:
            st.error("저장할 내용이 없습니다.")
        else:
            merged_audio = merge_audio_bytes(st.session_state.audio_chunks)
            final_script = "\n\n".join(st.session_state.live_script)
            summary = generate_final_report(final_script, api_key)

            save_to_db(
                f"회의_{datetime.now().strftime('%Y%m%d_%H%M')}",
                final_script,
                summary,
                merged_audio,
            )

            st.session_state.is_recording = False
            st.session_state.live_script = []
            st.session_state.audio_chunks = []
            st.session_state.mic_key = 0

            st.success("✅ 저장 완료")
            time.sleep(1)
            st.rerun()

# ==========================================
# 📂 파일 업로드
# ==========================================

elif menu == "📂 파일 업로드":
    st.title("📂 파일 업로드 회의록 생성")

    meeting_title = st.text_input(
        "회의 제목", f"회의_{datetime.now().strftime('%Y%m%d_%H%M')}"
    )
    uploaded_file = st.file_uploader(
        "파일 선택", type=["m4a", "mp3", "wav", "webm", "aac"]
    )

    if uploaded_file and st.button("분석 시작"):
        model = genai.GenerativeModel("gemini-2.5-flash")
        tmp = "temp_" + uploaded_file.name

        with open(tmp, "wb") as f:
            f.write(uploaded_file.getbuffer())

        audio = genai.upload_file(path=tmp)
        while audio.state.name == "PROCESSING":
            time.sleep(1)
            audio = genai.get_file(audio.name)

        script = model.generate_content(
            [audio, "이 오디오를 회의 스크립트로 작성해."]
        ).text

        summary = generate_final_report(script, api_key)
        save_to_db(meeting_title, script, summary, uploaded_file.getvalue())
        os.remove(tmp)

        st.success("✅ 완료")

# ==========================================
# 🗄️ 회의 기록
# ==========================================

elif menu == "🗄️ 회의 기록":
    st.title("🗄️ 회의 기록")

    df = pd.read_sql_query(
        "SELECT id, date, title, script, summary FROM meetings ORDER BY id DESC",
        conn,
    )

    for _, row in df.iterrows():
        with st.expander(f"[{row['date']}] {row['title']}"):
            c.execute("SELECT audio_blob FROM meetings WHERE id=?", (row["id"],))
            audio = c.fetchone()[0]

            if audio:
                st.audio(audio, format="audio/wav")
                st.download_button(
                    "WAV 다운로드",
                    audio,
                    f"{row['title']}.wav",
                    "audio/wav",
                )

            st.markdown("### 📝 회의록")
            st.markdown(row["summary"])

            st.markdown("### 🗣️ 스크립트")
            st.markdown(
                row["script"].replace("\n", "<br>"), unsafe_allow_html=True
            )
