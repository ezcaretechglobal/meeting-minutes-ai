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

st.set_page_config(page_title="AI 회의록 비서", layout="wide")

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
# 2. 프롬프트 정의 (STT & 요약)
# ==========================================

# 1) STT 전용 프롬프트 (화자 분리 및 타임스탬프 강제)
STT_PROMPT = """
너는 전문 속기사야. 제공된 오디오 파일을 듣고 정확한 회의 스크립트를 작성해.
다음 규칙을 엄격하게 지켜야 해:

1. **화자 분리**: 목소리를 구분하여 '화자 1', '화자 2', '화자 3' 등으로 표기할 것. (참석자 이름을 안다면 이름으로 표기해도 됨)
2. **타임스탬프**: 대화가 시작되는 시간을 [MM:SS] 형식으로 앞에 붙일 것.
3. **언어**: 한국어, 영어, 아랍어가 섞여 있을 수 있음. 들리는 그대로 정확하게 받아적을 것.
4. **출력 형식**: 아래 형식을 반드시 따를 것.

[형식 예시]
[00:00] 화자 1: 이번 회의를 시작하겠습니다. 모두 오셨나요?
[00:05] 화자 2: 네, 참석했습니다.
[00:10] 화자 1: Okay, let's discuss the agenda.

오디오의 처음부터 끝까지 빠짐없이 작성해.
"""

# 2) 회의록 요약 프롬프트 (기존 내용을 유지하되 포맷에 최적화)
SUMMARY_PROMPT = """
# 역할 (Role)
너는 ‘회의록 정리 전문 GPT’이다.
내가 제공하는 [시간] 화자: 대화내용 형식의 스크립트를 기반으로 회의록을 작성한다.

# 목적 (Goals)
- 스크립트를 정독하고, 핵심 내용을 분석하여 회의록 형태로 구조화한다.
- 화자(Speaker)가 구분되어 있으므로, 누가 어떤 발언을 했는지 맥락을 정확히 파악하여 결정 사항과 향후 계획을 도출한다.
- 추측하지 말고 오직 텍스트에 기반하여 작성한다.

# 회의록 템플릿 (Template)

## 1. 회의 개요
1. 날짜: (오늘 날짜 혹은 스크립트상 날짜)
2. 주요 의제: (내용 기반 추론)
3. 추정 참석자: (화자 1, 화자 2 등으로 표기되더라도 대화 내용에서 직책이나 이름이 유추되면 기재)

## 2. 회의 내용 요약
1) 주요 이슈 및 논의사항
   - 주제별로 그룹화하여 정리
   - **중요 발언 인용**: | [00:00] 화자 1 : "원문 텍스트" (반드시 타임스탬프 포함)

## 3. 주요 결정 사항
- (명확히 합의된 내용 위주로 작성)

## 4. 향후 실행 계획 (Action Items)
- 과제 (기한) - 담당자(화자)

# 출력 형식
- 위 템플릿 구조를 유지할 것.
"""

# ==========================================
# 3. AI 처리 함수 (Google Gemini Pro)
# ==========================================

def process_audio_with_gemini(uploaded_file, api_key):
    """Google Gemini Pro를 사용하여 STT(화자분리) -> 회의록 생성"""
    
    # 1. API 설정
    genai.configure(api_key=api_key)
    
    # 2. 임시 파일 저장
    temp_filename = "temp_" + uploaded_file.name
    with open(temp_filename, "wb") as f:
        f.write(uploaded_file.getbuffer())

    try:
        # 3. 파일 업로드
        with st.spinner("☁️ 구글 서버에 오디오 업로드 중..."):
            audio_file = genai.upload_file(path=temp_filename)
        
        # 처리 대기
        while audio_file.state.name == "PROCESSING":
            time.sleep(2)
            audio_file = genai.get_file(audio_file.name)

        # 4. 모델 설정 (Gemini 1.5 Pro 사용 - 화자 분리 성능이 더 좋음)
        model = genai.GenerativeModel('gemini-2.5-flash')

        # 5. STT (스크립트 추출) 실행
        with st.spinner("🗣️ 목소리 구분 및 스크립트 작성 중 (시간이 조금 걸립니다)..."):
            response_script = model.generate_content(
                [audio_file, STT_PROMPT]
            )
            script_text = response_script.text

        # 6. 회의록 요약 실행
        with st.spinner("📝 스크립트 기반으로 회의록 정리 중..."):
            response_summary = model.generate_content(
                [script_text, SUMMARY_PROMPT]
            )
            summary_text = response_summary.text
            
        return script_text, summary_text

    except Exception as e:
        raise e
    finally:
        if os.path.exists(temp_filename):
            os.remove(temp_filename)

def save_meeting(title, script, summary, filename):
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    c.execute("INSERT INTO meetings (date, title, script, summary, filename) VALUES (?, ?, ?, ?, ?)",
              (date_str, title, script, summary, filename))
    conn.commit()

# ==========================================
# 4. UI 구성
# ==========================================

st.sidebar.title("🗂️ 구글 AI 회의 비서")
api_key = st.sidebar.text_input("Google API Key", type="password", help="AIza로 시작하는 키 입력")

menu = st.sidebar.radio("메뉴", ["새 회의 시작", "회의 기록 (History)"])

if menu == "새 회의 시작":
    st.title("🎙️ 클로바노트 스타일 회의록 생성기")
    st.markdown("Google **Gemini**를 사용하여 **화자 분리(Diarization)** 및 **타임스탬프**가 포함된 기록을 만듭니다.")

    meeting_title = st.text_input("회의 제목", value=f"회의_{datetime.now().strftime('%Y%m%d_%H%M')}")
    uploaded_file = st.file_uploader("녹음 파일 (m4a, mp3, wav, aac)", type=["m4a", "mp3", "wav", "webm", "aac"])

    if uploaded_file and st.button("분석 시작"):
        if not api_key:
            st.error("API Key를 입력해주세요.")
        else:
            try:
                script_result, summary_result = process_audio_with_gemini(uploaded_file, api_key)
                save_meeting(meeting_title, script_result, summary_result, uploaded_file.name)
                st.success("완료되었습니다!")
                
                # 결과 탭 분리
                tab1, tab2 = st.tabs(["📝 회의록 요약", "🗣️ 상세 스크립트"])
                
                with tab1:
                    st.markdown(summary_result)
                
                with tab2:
                    st.text_area("전체 대화 내용", script_result, height=600)
                    
            except Exception as e:
                st.error(f"오류 발생: {e}")

elif menu == "회의 기록 (History)":
    st.title("🗄️ 지난 회의 기록")
    df = pd.read_sql_query("SELECT * FROM meetings ORDER BY id DESC", conn)
    
    if not df.empty:
        for index, row in df.iterrows():
            with st.expander(f"[{row['date']}] {row['title']}"):
                col1, col2 = st.columns(2)
                with col1:
                    st.markdown("### 📑 요약본")
                    st.markdown(row['summary'])
                with col2:
                    st.markdown("### 🗣️ 스크립트")
                    st.text_area("상세 내용", row['script'], height=400, key=f"hist_{row['id']}")
    else:
        st.info("기록이 없습니다.")



