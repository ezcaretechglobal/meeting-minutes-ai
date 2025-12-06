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

# DB 연결
conn = sqlite3.connect('meeting_history_v3.db', check_same_thread=False)
c = conn.cursor()

# 테이블 생성
c.execute('''
    CREATE TABLE IF NOT EXISTS meetings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        title TEXT,
        script TEXT,
        summary TEXT,
        filename TEXT,
        audio_blob BLOB
    )
''')
conn.commit()

# ==========================================
# 2. 헬퍼 함수
# ==========================================

def merge_audio_bytes(audio_chunks):
    """여러 WAV 조각 병합"""
    if not audio_chunks: return None
    output = io.BytesIO()
    try:
        first_chunk = io.BytesIO(audio_chunks[0])
        with wave.open(first_chunk, 'rb') as wav_in:
            params = wav_in.getparams()
        with wave.open(output, 'wb') as wav_out:
            wav_out.setparams(params)
            for chunk_bytes in audio_chunks:
                with wave.open(io.BytesIO(chunk_bytes), 'rb') as wav_in:
                    wav_out.writeframes(wav_in.readframes(wav_in.getnframes()))
        return output.getvalue()
    except Exception as e:
        return None

def transcribe_audio_segment(audio_bytes, api_key):
    """Gemini 1.5 Flash (빠른 STT)"""
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
        response = model.generate_content([audio_file, "이 오디오의 내용을 한국어(혹은 사용된 언어)로 정확하게 받아적어줘. 부가 설명 없이 텍스트만 출력해."])
        return response.text
    except: return "(인식 대기 중...)"
    finally:
        if os.path.exists(temp_filename): os.remove(temp_filename)

def generate_final_report(input_content, api_key, is_file=False):
    """Gemini 1.5 Pro (최종 회의록)"""
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-2.5-flash')
    
    SUMMARY_PROMPT = """
    # 역할
    너는 '회의록 정리 전문 GPT'야. 제공된 내용을 바탕으로 회의록을 작성해.
    
    # 회의록 템플릿
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

    ## 4. 향후 실행 계획
    - 과제 (기한) - 담당자
    """

    if is_file:
        # 파일 업로드인 경우 (오디오/비디오 파일 자체를 넘김)
        prompt = [input_content, f"이 미디어 파일 전체를 분석해서 회의록을 작성해줘.\n{SUMMARY_PROMPT}"]
    else:
        # 텍스트 스크립트인 경우
        prompt = f"아래 스크립트를 바탕으로 회의록을 작성해.\n[스크립트]\n{input_content}\n{SUMMARY_PROMPT}"
    
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e: return f"생성 실패: {e}"

def save_to_db(title, script, summary, filename, audio_blob):
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    c.execute("INSERT INTO meetings (date, title, script, summary, filename, audio_blob) VALUES (?, ?, ?, ?, ?, ?)",
              (date_str, title, script, summary, filename, audio_blob))
    conn.commit()

def update_db(id, title, script, summary):
    c.execute("UPDATE meetings SET title=?, script=?, summary=? WHERE id=?", (title, script, summary, id))
    conn.commit()

# ==========================================
# 3. UI 구성
# ==========================================

st.sidebar.title("🗂️ 구글 AI 회의 비서")
api_key = st.sidebar.text_input("Google API Key", type="password", help="AIza로 시작하는 키 입력")

menu = st.sidebar.radio("메뉴", ["🔴 실시간 회의 (Live)", "📂 파일 업로드 (MP3/MP4)", "🗄️ 회의 기록"])

# ----------------------------------------------------
# [메뉴 1] 🔴 실시간 회의 (Live)
# ----------------------------------------------------
if menu == "🔴 실시간 회의 (Live)":
    st.title("🔴 실시간 회의 녹음")
    
    if not api_key: st.warning("👈 API Key를 입력해주세요.")
    else:
        if 'live_script' not in st.session_state: st.session_state.live_script = []
        if 'audio_chunks' not in st.session_state: st.session_state.audio_chunks = []
        if 'interim_summary' not in st.session_state: st.session_state.interim_summary = "회의가 시작되면 요약이 표시됩니다."

        col_rec, col_info = st.columns([1, 4])
        with col_rec:
            audio_data = mic_recorder(
                start_prompt="⏺️ 녹음 시작", stop_prompt="⏹️ 녹음 중지", key='recorder', format='wav', use_container_width=True
            )

        if audio_data is not None:
            if 'last_id' not in st.session_state or st.session_state.last_id != audio_data['id']:
                st.session_state.last_id = audio_data['id']
                st.session_state.audio_chunks.append(audio_data['bytes'])
                
                with st.spinner("✍️ 받아적는 중..."):
                    text_seg = transcribe_audio_segment(audio_data['bytes'], api_key)
                    st.session_state.live_script.append(f"[{datetime.now().strftime('%H:%M')}] {text_seg}")
                    
                    if len(st.session_state.live_script) % 2 == 0:
                        try:
                            genai.configure(api_key=api_key)
                            res = genai.GenerativeModel('gemini-2.5-flash').generate_content(f"3줄 요약해:\n" + "\n".join(st.session_state.live_script))
                            st.session_state.interim_summary = res.text
                        except: pass
                st.rerun()

        st.divider()
        c1, c2 = st.columns([2, 1])
        with c1: st.text_area("Script", value="\n\n".join(st.session_state.live_script), height=400, disabled=True)
        with c2: st.info(st.session_state.interim_summary)

        if st.button("💾 저장하기", type="primary", use_container_width=True):
            if not st.session_state.live_script: st.error("내용이 없습니다.")
            else:
                with st.spinner("정리 중..."):
                    merged = merge_audio_bytes(st.session_state.audio_chunks)
                    f_script = "\n\n".join(st.session_state.live_script)
                    f_sum = generate_final_report(f_script, api_key, is_file=False)
                    save_to_db(f"회의_{datetime.now().strftime('%Y%m%d_%H%M')}", f_script, f_sum, "live_record.wav", merged)
                    
                    st.session_state.live_script = []
                    st.session_state.audio_chunks = []
                    st.session_state.interim_summary = ""
                    st.success("저장 완료!")
                    time.sleep(2)
                    st.rerun()

# ----------------------------------------------------
# [메뉴 2] 📂 파일 업로드 (MP4 지원 추가)
# ----------------------------------------------------
elif menu == "📂 파일 업로드 (MP3/MP4)":
    st.title("📂 파일 업로드 회의록")
    st.markdown("음성(mp3, wav) 또는 **동영상(mp4)** 파일을 업로드하세요.")
    
    title = st.text_input("회의 제목", value=f"회의_{datetime.now().strftime('%Y%m%d_%H%M')}")
    # mp4 추가됨
    uploaded_file = st.file_uploader("파일 선택", type=["m4a", "mp3", "wav", "webm", "aac", "mp4"])

    if uploaded_file and st.button("분석 시작"):
        if not api_key: st.error("API Key 필요")
        else:
            try:
                genai.configure(api_key=api_key)
                temp_filename = "temp_" + uploaded_file.name
                with open(temp_filename, "wb") as f: f.write(uploaded_file.getbuffer())
                
                with st.spinner("파일 업로드 및 AI 분석 중... (영상은 시간이 좀 더 걸릴 수 있습니다)"):
                    # 1. 파일 업로드
                    media_file = genai.upload_file(path=temp_filename)
                    while media_file.state.name == "PROCESSING":
                        time.sleep(2)
                        media_file = genai.get_file(media_file.name)
                    
                    # 2. STT 추출 (스크립트용)
                    stt_model = genai.GenerativeModel('gemini-2.5-flash')
                    res_script = stt_model.generate_content([media_file, "이 미디어의 모든 대화 내용을 [MM:SS] 화자: 내용 형식으로 받아적어줘."])
                    script_text = res_script.text
                    
                    # 3. 회의록 생성
                    res_sum = generate_final_report(media_file, api_key, is_file=True)
                    
                    # 4. 저장
                    save_to_db(title, script_text, res_sum, uploaded_file.name, uploaded_file.getvalue())
                    st.success("완료!")
                    if os.path.exists(temp_filename): os.remove(temp_filename)
            except Exception as e: st.error(f"오류: {e}")

# ----------------------------------------------------
# [메뉴 3] 🗄️ 회의 기록 (MP4 플레이어 지원)
# ----------------------------------------------------
elif menu == "🗄️ 회의 기록":
    st.title("🗄️ 지난 회의 기록")
    
    # filename 컬럼 추가 조회
    df = pd.read_sql_query("SELECT id, date, title, script, summary, filename FROM meetings ORDER BY id DESC", conn)
    
    if not df.empty:
        for index, row in df.iterrows():
            with st.expander(f"[{row['date']}] {row['title']}"):
                
                # 파일 데이터 조회
                c.execute("SELECT audio_blob FROM meetings WHERE id=?", (row['id'],))
                blob_data = c.fetchone()[0]
                
                if blob_data:
                    # 확장자 확인
                    file_ext = row['filename'].split('.')[-1].lower() if row['filename'] else 'wav'
                    
                    st.markdown(f"### 🎬 원본 파일 ({file_ext.upper()})")
                    
                    # MP4면 비디오 플레이어, 아니면 오디오 플레이어
                    if file_ext == 'mp4':
                        st.video(blob_data, format="video/mp4")
                        mime_type = "video/mp4"
                    else:
                        st.audio(blob_data, format=f'audio/{file_ext}')
                        mime_type = f"audio/{file_ext}"

                    st.download_button("💾 파일 다운로드", data=blob_data, file_name=row['filename'], mime=mime_type)
                else:
                    st.info("파일 없음")

                st.divider()

                # 수정/보기 로직
                edit_key = f"edit_{row['id']}"
                if edit_key not in st.session_state: st.session_state[edit_key] = False
                
                if st.session_state[edit_key]:
                    new_t = st.text_input("제목", value=row['title'], key=f"t_{row['id']}")
                    t1, t2 = st.tabs(["요약 수정", "스크립트 수정"])
                    with t1: n_s = st.text_area("sum", value=row['summary'], height=300, key=f"s_{row['id']}")
                    with t2: n_sc = st.text_area("scr", value=row['script'], height=300, key=f"sc_{row['id']}")
                    if st.button("저장", key=f"sv_{row['id']}"):
                        update_db(row['id'], new_t, n_sc, n_s)
                        st.session_state[edit_key] = False
                        st.rerun()
                else:
                    c1, c2 = st.columns([9,1])
                    with c1: st.markdown(f"### {row['title']}")
                    with c2: 
                        if st.button("✏️", key=f"ed_{row['id']}"):
                            st.session_state[edit_key] = True
                            st.rerun()
                    
                    t1, t2 = st.tabs(["📝 회의록", "🗣️ 스크립트"])
                    with t1: st.markdown(row['summary'])
                    with t2: st.markdown(f"<div style='background-color:#f9f9f9;padding:15px;max-height:400px;overflow-y:auto;'>{row['script'].replace(chr(10), '<br>')}</div>", unsafe_allow_html=True)
    else:
        st.info("기록 없음")
