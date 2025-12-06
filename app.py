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

# 페이지 설정은 반드시 코드 최상단에 있어야 합니다.
st.set_page_config(page_title="AI 회의록 비서 (Video Support)", layout="wide")

# DB 연결 (충돌 방지를 위해 v4로 이름 변경)
db_filename = 'meeting_history_v4.db'
conn = sqlite3.connect(db_filename, check_same_thread=False)
c = conn.cursor()

# 테이블 생성 (구조가 변경되었으므로 새로운 DB에 생성됨)
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
        # 파일 업로드인 경우
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
    c.execute("UPDATE meetings SET title=?, scri
