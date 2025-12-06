import streamlit as st
import google.generativeai as genai
from datetime import datetime
import sqlite3
import pandas as pd
import os
import time
import re
import io
import wave
from streamlit_mic_recorder import mic_recorder

# ==========================================
# 1. 설정 및 데이터베이스 초기화
# ==========================================

st.set_page_config(page_title="AI 회의록 비서 (Final)", layout="wide")

# DB 연결 (오디오 저장을 위해 새로운 DB 파일명 사용)
conn = sqlite3.connect('meeting_history_v2.db', check_same_thread=False)
c = conn.cursor()

# 테이블 생성 (audio_blob 컬럼 추가: 녹음 파일 저장용)
c.execute('''
    CREATE TABLE IF NOT EXISTS meetings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        title TEXT,
        script TEXT,
        summary TEXT,
        audio_blob BLOB
    )
''')
conn.commit()

# ==========================================
# 2. 헬퍼 함수 (오디오 병합 & AI)
# ==========================================

def merge_audio_bytes(audio_chunks):
    """
    여러 개의 WAV 바이트 청크를 하나의 WAV 파일로 병합합니다.
    (각 청크의 헤더를 처리하고 데이터만 이어 붙임)
    """
    if not audio_chunks:
        return None
    
    output = io.BytesIO()
    
    try:
        # 첫 번째 청크에서 오디오 파라미터(Sample rate 등) 추출
        first_chunk = io.BytesIO(audio_chunks[0])
        with wave.open(first_chunk, 'rb') as wav_in:
            params = wav_in.getparams()
            
        # 병합 시작
        with wave.open(output, 'wb') as wav_out:
            wav_out.setparams(params)
            
            for chunk_bytes in audio_chunks:
                with wave.open(io.BytesIO(chunk_bytes), 'rb') as wav_in:
                    # 헤더를 제외한 순수 오디오 프레임만 쓰기
                    wav_out.writeframes(wav_in.readframes(wav_in.getnframes()))
                    
        return output.getvalue()
    except Exception as e:
        st.error(f"오디오 병합 중 오류 발생: {e}")
        return None

def transcribe_audio_segment(audio_bytes, api_key):
    """Gemini 1.5 Flash를 사용하여 빠른 STT 변환"""
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-2.5-flash')
    
    temp_filename = f"temp_{int(time.time())}.wav"
    with open(temp_filename, "wb") as f:
        f.write(audio_bytes)
        
    try:
        audio_file = genai.upload_file(path=temp_filename)
        while audio_file.state.name == "PROCESSING":
            time.sleep(0.2)
            audio_file = genai.get_file(audio_file.name)
            
        # 타임스탬프와 화자 구분 없이 텍스트만 빠르게 추출
        response = model.generate_content([audio_file, "이 오디오의 내용을 한국어(혹은 영어/아랍어)로 정확하게 받아적어줘. 부가 설명 없이 텍스트만 출력해."])
        return response.text
    except Exception as e:
        return f"(인식 오류: {e})"
    finally:
        if os.path.exists(temp_filename):
            os.remove(temp_filename)

def generate_final_report(full_script, api_key):
    """Gemini 1.5 Pro를 사용하여 최종 회의록 생성"""
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
# 3. UI 구성
# ==========================================

st.sidebar.title("🗂️ 구글 AI 회의 비서")
api_key = st.sidebar.text_input("Google API Key", type="password", help="AIza로 시작하는 키 입력")

menu = st.sidebar.radio("메뉴", ["🔴 실시간 회의 (Live)", "📂 파일 업로드", "🗄️ 회의 기록"])

# ----------------------------------------------------
# [메뉴 1] 🔴 실시간 회의 (Live) - 녹음/다운로드 기능 강화
# ----------------------------------------------------
if menu == "🔴 실시간 회의 (Live)":
    st.title("🔴 실시간 회의 녹음")
    st.markdown("회의 내용을 녹음하면 **실시간으로 텍스트가 변환**되고, 종료 시 **음성 파일과 회의록이 저장**됩니다.")

    if not api_key:
        st.warning("👈 사이드바에 Google API Key를 먼저 입력해주세요.")
    else:
        # 세션 초기화
        if 'live_script' not in st.session_state:
            st.session_state.live_script = []  # 텍스트 저장
        if 'audio_chunks' not in st.session_state:
            st.session_state.audio_chunks = [] # 오디오 바이너리 조각 저장
        if 'interim_summary' not in st.session_state:
            st.session_state.interim_summary = "회의가 시작되면 요약이 표시됩니다."

        # --- 녹음기 위젯 ---
        col_rec, col_info = st.columns([1, 4])
        with col_rec:
            # 녹음기 위젯 (사용자가 Stop을 누르면 audio_data 반환)
            audio_data = mic_recorder(
                start_prompt="⏺️ 녹음 시작",
                stop_prompt="⏹️ 녹음 중지 (변환)",
                key='recorder',
                format='wav',
                use_container_width=True
            )

        # --- 데이터 처리 로직 ---
        if audio_data is not None:
            # 중복 처리 방지 (Streamlit 특성상 리런될 때 중복 실행 방지)
            if 'last_id' not in st.session_state or st.session_state.last_id != audio_data['id']:
                st.session_state.last_id = audio_data['id']
                
                # 1. 오디오 조각 저장 (나중에 합치기 위해 리스트에 추가)
                st.session_state.audio_chunks.append(audio_data['bytes'])
                
                # 2. 실시간 STT 변환
                with st.spinner("✍️ 받아적는 중..."):
                    text_seg = transcribe_audio_segment(audio_data['bytes'], api_key)
                    
                    # 타임스탬프 추가
                    ts = datetime.now().strftime("%H:%M")
                    formatted_line = f"[{ts}] {text_seg}"
                    st.session_state.live_script.append(formatted_line)
                    
                    # 3. 간단 중간 요약 (텍스트가 쌓일 때마다)
                    full_text = "\n".join(st.session_state.live_script)
                    # 간단하게 Flash 모델로 요약 업데이트 (비용 절약 및 속도)
                    if len(st.session_state.live_script) % 2 == 0: # 2번 녹음마다 요약 갱신
                        try:
                            genai.configure(api_key=api_key)
                            model_flash = genai.GenerativeModel('gemini-2.5-flash')
                            res = model_flash.generate_content(f"이 회의 내용을 3줄로 핵심만 요약해:\n{full_text}")
                            st.session_state.interim_summary = res.text
                        except: pass
                
                st.rerun()

        st.divider()

        # --- 화면 표시 ---
        c1, c2 = st.columns([2, 1])
        with c1:
            st.subheader("📜 실시간 스크립트")
            # 채팅창처럼 보여주기
            script_view = "\n\n".join(st.session_state.live_script)
            st.text_area("Script", value=script_view, height=400, disabled=True)
            
        with c2:
            st.subheader("💡 실시간 요약")
            st.info(st.session_state.interim_summary)
            st.caption(f"현재 녹음된 파일 조각: {len(st.session_state.audio_chunks)}개")

        # --- 최종 저장 버튼 ---
        if st.button("💾 회의 종료 및 저장 (오디오+회의록)", type="primary", use_container_width=True):
            if not st.session_state.live_script:
                st.error("저장할 대화 내용이 없습니다.")
            else:
                with st.spinner("💽 오디오 병합 및 최종 회의록 작성 중..."):
                    # 1. 오디오 병합 (조각난 WAV들을 하나로 합침)
                    merged_audio = merge_audio_bytes(st.session_state.audio_chunks)
                    
                    # 2. 스크립트 합치기
                    final_script = "\n\n".join(st.session_state.live_script)
                    
                    # 3. 최종 회의록 생성 (Pro 모델)
                    final_summary = generate_final_report(final_script, api_key)
                    
                    # 4. DB 저장 (BLOB 포함)
                    title = f"회의_{datetime.now().strftime('%Y%m%d_%H%M')}"
                    save_to_db(title, final_script, final_summary, merged_audio)
                    
                    # 5. 초기화
                    st.session_state.live_script = []
                    st.session_state.audio_chunks = []
                    st.session_state.interim_summary = "새로운 회의를 시작하세요."
                    st.success("저장 완료! '회의 기록' 탭에서 확인하세요.")
                    time.sleep(2)
                    st.rerun()

# ----------------------------------------------------
# [메뉴 2] 📂 파일 업로드 (기존 로직 유지)
# ----------------------------------------------------
elif menu == "📂 파일 업로드":
    st.title("📂 파일 업로드 회의록 생성")
    st.markdown("녹음 파일(m4a, mp3 등)을 업로드하여 분석합니다.")
    
    meeting_title = st.text_input("회의 제목", value=f"회의_{datetime.now().strftime('%Y%m%d_%H%M')}")
    uploaded_file = st.file_uploader("파일 선택", type=["m4a", "mp3", "wav", "webm", "aac"])

    if uploaded_file and st.button("분석 시작"):
        if not api_key: st.error("API Key 필요")
        else:
            try:
                genai.configure(api_key=api_key)
                temp_filename = "temp_" + uploaded_file.name
                with open(temp_filename, "wb") as f:
                    f.write(uploaded_file.getbuffer())
                
                with st.spinner("분석 중... (시간이 소요됩니다)"):
                    audio_file = genai.upload_file(path=temp_filename)
                    while audio_file.state.name == "PROCESSING":
                        time.sleep(1)
                        audio_file = genai.get_file(audio_file.name)
                    
                    model = genai.GenerativeModel('gemini-2.5-flash')
                    
                    # STT
                    res_script = model.generate_content([audio_file, "이 오디오 전체를 스크립트로 작성해줘. [MM:SS] 화자: 내용 형식으로."])
                    script_text = res_script.text
                    
                    # 요약
                    res_summary = generate_final_report(script_text, api_key)
                    
                    # 저장 (업로드한 파일 바이너리도 DB에 저장)
                    save_to_db(meeting_title, script_text, res_summary, uploaded_file.getvalue())
                    
                    st.success("완료!")
                    if os.path.exists(temp_filename): os.remove(temp_filename)
            except Exception as e:
                st.error(f"오류: {e}")

# ----------------------------------------------------
# [메뉴 3] 🗄️ 회의 기록 (다운로드 기능 추가)
# ----------------------------------------------------
elif menu == "🗄️ 회의 기록":
    st.title("🗄️ 지난 회의 기록")
    
    # DB 조회
    df = pd.read_sql_query("SELECT id, date, title, script, summary FROM meetings ORDER BY id DESC", conn)
    
    if not df.empty:
        for index, row in df.iterrows():
            with st.expander(f"[{row['date']}] {row['title']}"):
                
                # 1. 오디오 다운로드/재생 섹션
                c.execute("SELECT audio_blob FROM meetings WHERE id=?", (row['id'],))
                result = c.fetchone()
                audio_data = result[0] if result else None
                
                if audio_data:
                    st.markdown("### 🎧 녹음 파일")
                    st.audio(audio_data, format='audio/wav')
                    st.download_button(
                        label="💾 WAV 파일 다운로드",
                        data=audio_data,
                        file_name=f"{row['title']}.wav",
                        mime="audio/wav"
                    )
                else:
                    st.info("저장된 오디오 파일이 없습니다.")

                st.divider()

                # 2. 수정 및 보기 섹션
                edit_key = f"edit_{row['id']}"
                if edit_key not in st.session_state: st.session_state[edit_key] = False
                
                if st.session_state[edit_key]:
                    # 수정 모드
                    new_title = st.text_input("제목 수정", value=row['title'], key=f"t_{row['id']}")
                    t1, t2 = st.tabs(["📝 회의록 수정", "🗣️ 스크립트 수정"])
                    with t1: n_sum = st.text_area("sum", value=row['summary'], height=300, key=f"s_{row['id']}")
                    with t2: n_scr = st.text_area("scr", value=row['script'], height=300, key=f"sc_{row['id']}")
                    
                    c1, c2 = st.columns([1,8])
                    with c1: 
                        if st.button("저장", key=f"sv_{row['id']}"):
                            update_db(row['id'], new_title, n_scr, n_sum)
                            st.session_state[edit_key] = False
                            st.rerun()
                    with c2:
                         if st.button("취소", key=f"cc_{row['id']}"):
                            st.session_state[edit_key] = False
                            st.rerun()
                else:
                    # 보기 모드
                    col_h, col_b = st.columns([8, 1])
                    with col_h: st.markdown(f"### {row['title']}")
                    with col_b: 
                        if st.button("✏️", key=f"ed_{row['id']}"):
                            st.session_state[edit_key] = True
                            st.rerun()
                    
                    t1, t2 = st.tabs(["📝 회의록", "🗣️ 스크립트"])
                    with t1: st.markdown(row['summary'])
                    with t2: 
                        st.markdown(
                            f"<div style='background-color:#f9f9f9;padding:15px;max-height:400px;overflow-y:auto;'>{row['script'].replace(chr(10), '<br>')}</div>", 
                            unsafe_allow_html=True
                        )

    else:
        st.info("저장된 회의 기록이 없습니다.")
