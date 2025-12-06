import streamlit as st
import google.generativeai as genai
from datetime import datetime
import sqlite3
import pandas as pd
import os
import time
import re
from streamlit_mic_recorder import mic_recorder # 클라우드용 녹음 라이브러리
import io

# ==========================================
# 1. 설정 및 데이터베이스 초기화
# ==========================================

st.set_page_config(page_title="AI 회의록 비서 (Cloud)", layout="wide")

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
# 2. 프롬프트 정의
# ==========================================

STT_PROMPT = """
너는 전문 속기사야. 제공된 오디오 파일을 듣고 정확한 회의 스크립트를 작성해.
다음 규칙을 엄격하게 지켜야 해:

1. **화자 분리**: 목소리를 구분하여 '화자 1', '화자 2', '화자 3' 등으로 표기할 것. (참석자 이름을 안다면 이름으로 표기해도 됨)
2. **타임스탬프**: 대화가 시작되는 시간을 [MM:SS] 형식으로 앞에 붙일 것. (이전 대화와 이어지는 시간 흐름을 고려해)
3. **언어**: 한국어, 영어, 아랍어가 섞여 있을 수 있음. 들리는 그대로 정확하게 받아적을 것.
4. **출력 형식**: 
   [MM:SS] 화자 1: 내용...

오디오의 처음부터 끝까지 빠짐없이 작성해.
"""

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
# 3. AI 처리 및 헬퍼 함수
# ==========================================

def format_script_with_spacing(text):
    """스크립트 가독성을 위해 [MM:SS] 화자 패턴 앞에 줄바꿈 추가"""
    formatted_text = re.sub(r'(?<!^)(\[\d{2}:\d{2}\])', r'\n\n\1', text)
    return formatted_text

def format_script_for_markdown(text):
    """보기 모드에서 화자 부분 볼드 처리"""
    formatted_text = re.sub(r'(\[\d{2}:\d{2}\].*?:)', r'**\1**', text)
    return formatted_text

def transcribe_segment(audio_bytes, api_key):
    """짧은 오디오 세그먼트를 STT 변환 (Gemini Flash 사용 - 속도 중요)"""
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-2.5-flash')
    
    # Bytes를 임시 파일로 저장 (Gemini API 요구사항)
    temp_filename = f"temp_seg_{int(time.time())}.wav"
    with open(temp_filename, "wb") as f:
        f.write(audio_bytes)
        
    try:
        audio_file = genai.upload_file(path=temp_filename)
        while audio_file.state.name == "PROCESSING":
            time.sleep(1)
            audio_file = genai.get_file(audio_file.name)
            
        response = model.generate_content([audio_file, "이 오디오의 내용을 한국어(혹은 영어/아랍어)로 정확하게 받아적어줘. 화자 구분은 필요없고 텍스트만 줘."])
        text = response.text
    except Exception as e:
        text = f"(오류 발생: {e})"
    finally:
        if os.path.exists(temp_filename):
            os.remove(temp_filename)
            
    return text

def generate_interim_summary(full_text, api_key):
    """중간 요약 생성"""
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-2.5-flash')
    prompt = f"다음은 진행 중인 회의의 누적 스크립트야. 현재까지의 내용을 3~5줄로 핵심만 요약해서 브리핑해줘:\n\n{full_text}"
    try:
        response = model.generate_content(prompt)
        return response.text
    except:
        return "요약 생성 중..."

def generate_final_minutes(full_text, api_key):
    """최종 회의록 생성 (텍스트 기반)"""
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-2.5-flash') # 성능 좋은 모델 사용
    
    # 텍스트를 기반으로 다시 정형화 요청
    prompt = f"""
    아래 텍스트는 구간별로 녹음된 회의 스크립트를 합친 거야.
    이 내용을 바탕으로 정식 회의록을 작성해줘.
    
    [전체 스크립트]
    {full_text}
    
    {SUMMARY_PROMPT}
    """
    response = model.generate_content(prompt)
    return response.text

def save_meeting(title, script, summary, filename):
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    c.execute("INSERT INTO meetings (date, title, script, summary, filename) VALUES (?, ?, ?, ?, ?)",
              (date_str, title, script, summary, filename))
    conn.commit()

def update_meeting(id, title, script, summary):
    c.execute("UPDATE meetings SET title=?, script=?, summary=? WHERE id=?", (title, script, summary, id))
    conn.commit()

# ==========================================
# 4. UI 구성
# ==========================================

st.sidebar.title("🗂️ 구글 AI 회의 비서")
api_key = st.sidebar.text_input("Google API Key", type="password", help="AIza로 시작하는 키 입력")

menu = st.sidebar.radio("메뉴", ["🔴 실시간 회의 (Live)", "📂 파일 업로드", "🗄️ 회의 기록"])

# ----------------------------------------------------
# [메뉴 1] 🔴 실시간 회의 (Cloud Compatible)
# ----------------------------------------------------
if menu == "🔴 실시간 회의 (Live)":
    st.title("🔴 실시간 회의 녹음 (구간 분석)")
    st.markdown("""
    **사용법:**
    1. **'Record'** 버튼을 눌러 발언을 녹음하세요.
    2. **'Stop'** 버튼을 누르면 자동으로 텍스트로 변환되고 요약이 갱신됩니다.
    3. 회의가 끝날 때까지 1~2 과정을 반복하세요.
    4. 마지막에 **'최종 회의록 생성 및 저장'**을 누르세요.
    """)
    
    # 세션 상태 초기화
    if 'live_transcript' not in st.session_state:
        st.session_state.live_transcript = [] # 누적 스크립트 리스트
    if 'interim_summary' not in st.session_state:
        st.session_state.interim_summary = "회의 내용이 입력되면 요약이 시작됩니다."

    if not api_key:
        st.warning("먼저 왼쪽 사이드바에 Google API Key를 입력해주세요.")
    else:
        # --- 녹음 위젯 ---
        col_rec, col_info = st.columns([1, 3])
        with col_rec:
            # streamlit-mic-recorder 사용
            # 녹음이 끝나면(Stop 누르면) audio_data에 바이트 데이터가 들어옴
            audio_data = mic_recorder(
                start_prompt="⏺️ 녹음 시작",
                stop_prompt="⏹️ 녹음 중지 & 분석",
                key='recorder',
                format='wav'
            )

        # --- 분석 로직 ---
        if audio_data is not None:
            # 이전 데이터와 다른 새로운 데이터인지 확인 (중복 실행 방지)
            if 'last_audio_id' not in st.session_state or st.session_state.last_audio_id != audio_data['id']:
                st.session_state.last_audio_id = audio_data['id']
                
                with st.spinner("방금 녹음된 내용을 분석 중입니다..."):
                    # 1. STT 변환
                    text_segment = transcribe_segment(audio_data['bytes'], api_key)
                    timestamp = datetime.now().strftime("%H:%M")
                    formatted_line = f"[{timestamp}] {text_segment}"
                    
                    # 2. 스크립트 누적
                    st.session_state.live_transcript.append(formatted_line)
                    
                    # 3. 중간 요약 갱신
                    full_text = "\n".join(st.session_state.live_transcript)
                    st.session_state.interim_summary = generate_interim_summary(full_text, api_key)
                    
                st.rerun() # 화면 갱신해서 텍스트 보여주기

        st.divider()

        # --- 결과 화면 ---
        col_script, col_summary = st.columns([2, 1])

        with col_script:
            st.subheader("🗣️ 실시간 스크립트")
            # 채팅창처럼 보여주기
            chat_content = "\n\n".join(st.session_state.live_transcript)
            st.text_area("Transcript", value=chat_content, height=400, disabled=True)

        with col_summary:
            st.subheader("💡 실시간 요약")
            st.info(st.session_state.interim_summary)

        st.divider()
        
        # --- 최종 저장 ---
        if st.button("💾 최종 회의록 생성 및 저장", type="primary"):
            if not st.session_state.live_transcript:
                st.error("저장할 내용이 없습니다.")
            else:
                with st.spinner("전체 내용을 정리하여 회의록을 생성하고 있습니다 (Pro 모델)..."):
                    final_full_script = "\n\n".join(st.session_state.live_transcript)
                    
                    # 최종 회의록 생성
                    final_summary = generate_final_minutes(final_full_script, api_key)
                    
                    # 저장
                    title = f"실시간회의_{datetime.now().strftime('%Y%m%d_%H%M')}"
                    save_meeting(title, final_full_script, final_summary, "live_recording.txt")
                    
                    st.success("회의록이 저장되었습니다! '회의 기록' 탭에서 확인하세요.")
                    # 초기화
                    st.session_state.live_transcript = []
                    st.session_state.interim_summary = "새로운 회의를 시작하세요."
                    time.sleep(2)
                    st.rerun()

# ----------------------------------------------------
# [메뉴 2] 📂 파일 업로드 (기존 유지)
# ----------------------------------------------------
elif menu == "📂 파일 업로드":
    st.title("📂 파일 업로드 회의록 생성")
    st.markdown("녹음 파일(m4a, mp3 등)을 업로드하여 정밀 분석합니다.")
    
    # ... (기존 파일 업로드 로직)
    def process_audio_file(uploaded_file, api_key):
        genai.configure(api_key=api_key)
        temp_filename = "temp_" + uploaded_file.name
        with open(temp_filename, "wb") as f:
            f.write(uploaded_file.getbuffer())
        try:
            with st.spinner("☁️ 업로드 및 분석 중..."):
                audio_file = genai.upload_file(path=temp_filename)
                while audio_file.state.name == "PROCESSING":
                    time.sleep(2)
                    audio_file = genai.get_file(audio_file.name)
                
                model = genai.GenerativeModel('gemini-2.5-flash')
                response_script = model.generate_content([audio_file, STT_PROMPT])
                raw_script = response_script.text
                script_text = format_script_with_spacing(raw_script)
                
                response_summary = model.generate_content([script_text, SUMMARY_PROMPT])
                summary_text = response_summary.text
                return script_text, summary_text
        finally:
            if os.path.exists(temp_filename):
                os.remove(temp_filename)

    meeting_title = st.text_input("회의 제목", value=f"회의_{datetime.now().strftime('%Y%m%d_%H%M')}")
    uploaded_file = st.file_uploader("파일 선택", type=["m4a", "mp3", "wav", "webm", "aac"])

    if uploaded_file and st.button("분석 시작"):
        if not api_key: st.error("API Key 필요")
        else:
            try:
                script_res, summary_res = process_audio_file(uploaded_file, api_key)
                save_meeting(meeting_title, script_res, summary_res, uploaded_file.name)
                st.success("완료!")
                tab1, tab2 = st.tabs(["요약", "스크립트"])
                with tab1: st.markdown(summary_res)
                with tab2: st.markdown(format_script_for_markdown(script_res))
            except Exception as e: st.error(f"Error: {e}")

# ----------------------------------------------------
# [메뉴 3] 🗄️ 회의 기록 (기존 유지 - 수정 기능 포함)
# ----------------------------------------------------
elif menu == "🗄️ 회의 기록":
    st.title("🗄️ 지난 회의 기록")
    df = pd.read_sql_query("SELECT * FROM meetings ORDER BY id DESC", conn)
    
    if not df.empty:
        for index, row in df.iterrows():
            with st.expander(f"[{row['date']}] {row['title']}"):
                edit_key = f"edit_mode_{row['id']}"
                if edit_key not in st.session_state: st.session_state[edit_key] = False

                if st.session_state[edit_key]: # 수정 모드
                    new_title = st.text_input("제목", value=row['title'], key=f"t_{row['id']}")
                    t1, t2 = st.tabs(["요약 수정", "스크립트 수정"])
                    with t1: n_sum = st.text_area("sum", value=row['summary'], height=400, key=f"s_{row['id']}")
                    with t2: n_scr = st.text_area("scr", value=row['script'], height=400, key=f"sc_{row['id']}")
                    
                    c1, c2 = st.columns([1,8])
                    with c1: 
                        if st.button("저장", key=f"sv_{row['id']}"):
                            update_meeting(row['id'], new_title, n_scr, n_sum)
                            st.session_state[edit_key] = False
                            st.rerun()
                    with c2:
                        if st.button("취소", key=f"cn_{row['id']}"):
                            st.session_state[edit_key] = False
                            st.rerun()
                else: # 보기 모드
                    c1, c2 = st.columns([9,1])
                    with c1: st.markdown(f"### {row['title']}")
                    with c2: 
                        if st.button("✏️", key=f"ed_{row['id']}"):
                            st.session_state[edit_key] = True
                            st.rerun()
                    
                    t1, t2 = st.tabs(["요약", "스크립트"])
                    with t1: st.markdown(row['summary'])
                    with t2: st.markdown(f"<div style='background-color:#f9f9f9;padding:15px;'>{format_script_for_markdown(row['script']).replace(chr(10), '<br>')}</div>", unsafe_allow_html=True)
    else:
        st.info("기록 없음")
