import streamlit as st
import google.generativeai as genai
from datetime import datetime
import sqlite3
import pandas as pd
import os
import time

# ==========================================
# 1. 설정 및 데이터베이스 초기화
# ==========================================

st.set_page_config(page_title="AI 회의록 비서 (Google Gemini)", layout="wide")

# DB 연결 및 테이블 생성
conn = sqlite3.connect('meeting_history_google.db', check_same_thread=False)
c = conn.cursor()
c.execute('''
    CREATE TABLE IF NOT EXISTS meetings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        title TEXT,
        script TEXT,
        summary TEXT,
        filename TEXT
    )
''')
conn.commit()

# ==========================================
# 2. AI 시스템 프롬프트
# ==========================================

SYSTEM_PROMPT = """
너는 회의록 정리 전문 GPT이다.
녹취록만 기반으로 회의록을 작성하고, 없는 내용은 절대 생성하지 않는다.

## 회의록 템플릿
1. 회의 개요
2. 회의 내용 (중요 문장 인용)
3. 결정 사항
4. 향후 계획
"""

# ==========================================
# 3. Gemini 기반 오디오 처리 함수
# ==========================================

def process_audio_with_gemini(uploaded_file, api_key):

    progress_text = st.empty()
    progress_bar = st.progress(0)

    genai.configure(api_key=api_key)

    temp_filename = "temp_upload_audio" + os.path.splitext(uploaded_file.name)[1]
    with open(temp_filename, "wb") as f:
        f.write(uploaded_file.getbuffer())

    try:
        # STEP 1: Google 서버 업로드
        progress_text.write("① Google 서버에 오디오 파일 업로드 중...")
        progress_bar.progress(10)

        audio_file = genai.upload_file(path=temp_filename)

        # STEP 2: 파일 처리 대기
        progress_text.write("② Google이 오디오 파일을 처리 중입니다...")
        progress_bar.progress(30)

        start_time = time.time()
        TIMEOUT = 1200  # 20분

        while audio_file.state.name == "PROCESSING":
            if time.time() - start_time > TIMEOUT:
                raise TimeoutError("Google 파일 처리 Timeout 초과")
            time.sleep(0.5)
            audio_file = genai.get_file(audio_file.name)

        # STEP 3: STT 변환
        progress_text.write("③ 음성을 텍스트로 변환(STT) 중...")
        progress_bar.progress(60)

        model = genai.GenerativeModel("gemini-2.5-flash")

        response_script = model.generate_content(
            [audio_file, "오디오 전체를 한국어로 정확하게 받아적어줘."]
        )
        script_text = response_script.text

        # STEP 4: 회의록 생성
        progress_text.write("④ 회의록 생성 중...")
        progress_bar.progress(85)

        response_summary = model.generate_content(
            [script_text, SYSTEM_PROMPT]
        )
        summary_text = response_summary.text

        progress_text.write("✅ 완료되었습니다!")
        progress_bar.progress(100)

        return script_text, summary_text

    except Exception as e:
        progress_text.write("❌ 오류 발생")
        st.error(str(e))
        raise e

    finally:
        if os.path.exists(temp_filename):
            os.remove(temp_filename)


# ==========================================
# 4. DB 저장 함수
# ==========================================

def save_meeting(title, script, summary, filename):
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    c.execute("INSERT INTO meetings (date, title, script, summary, filename) VALUES (?, ?, ?, ?, ?)",
              (date_str, title, script, summary, filename))
    conn.commit()


# ==========================================
# 5. Streamlit UI 구성
# ==========================================

st.sidebar.title("🗂️ 구글 AI 회의 비서")
api_key = st.sidebar.text_input("Google API Key", type="password")

menu = st.sidebar.radio("메뉴 이동", ["새 회의 시작", "회의 기록 (History)"])

# ------------------------------------------
# 새 회의 시작
# ------------------------------------------
if menu == "새 회의 시작":
    st.title("🎙️ AI 회의록 생성기 (Google Gemini)")

    meeting_title = st.text_input("회의 제목 입력", value=f"회의_{datetime.now().strftime('%Y%m%d_%H%M')}")
    uploaded_file = st.file_uploader("녹음 파일 업로드", type=["m4a", "mp3", "wav", "webm", "aac"])

    script_result = ""
    summary_result = ""

    if uploaded_file is not None:
        st.info(f"파일이 준비되었습니다: {uploaded_file.name}")

        if st.button("분석 및 회의록 생성 시작"):
            if not api_key:
                st.error("왼쪽 사이드바에 Google API Key를 입력하세요.")
            else:
                try:
                    script_result, summary_result = process_audio_with_gemini(uploaded_file, api_key)
                    save_meeting(meeting_title, script_result, summary_result, uploaded_file.name)
                    st.success("완료! 아래에서 결과를 확인하세요.")
                except Exception as e:
                    st.error(f"오류 발생: {e}")

    if script_result:
        st.divider()

        col1, col2 = st.columns(2)

        with col1:
            st.subheader("📝 전체 스크립트")
            st.text_area("Script", script_result, height=600)

        with col2:
            st.subheader("📑 회의록 요약")
            st.markdown(summary_result)

# ------------------------------------------
# 회의 기록 보기
# ------------------------------------------
elif menu == "회의 기록 (History)":
    st.title("🗄️ 회의록 히스토리")

    df = pd.read_sql_query("SELECT id, date, title FROM meetings ORDER BY id DESC", conn)

    if not df.empty:
        for index, row in df.iterrows():
            with st.expander(f"{row['date']} - {row['title']}"):
                c.execute("SELECT script, summary, filename FROM meetings WHERE id=?", (row['id'],))
                detail = c.fetchone()

                if detail:
                    script_db, summary_db, filename_db = detail
                    st.caption(f"원본 파일명: {filename_db}")

                    col_h1, col_h2 = st.columns(2)

                    with col_h1:
                        st.markdown("**[전체 스크립트]**")
                        st.text_area(f"script_{row['id']}", script_db, height=300)

                    with col_h2:
                        st.markdown("**[AI 요약 회의록]**")
                        st.markdown(summary_db)

    else:
        st.info("저장된 회의 기록이 없습니다.")