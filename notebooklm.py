import os
import shutil
import uuid
import json
import http.client
from pathlib import Path
from tempfile import NamedTemporaryFile

import streamlit as st
from pdf2image import convert_from_path
from PIL import Image
import boto3
from dotenv import load_dotenv
import requests
from pydub import AudioSegment  # 병합을 위한 라이브러리

# .env 파일에서 환경 변수 로드
load_dotenv()

# ---------- 설정 ----------
POPPLER_PATH = os.getenv(r"C:\Program Files (x86)\Release-24.08.0-0\poppler-24.08.0\Library\bin")  # 예: r"C:\\poppler\\bin"
CLOVA_API_KEY = os.getenv("CLOVA_API_KEY")
CLOVA_HOST = "clovastudio.stream.ntruss.com"
OBJECT_STORAGE_ACCESS_KEY = os.getenv("OBJECT_STORAGE_ACCESS_KEY")
OBJECT_STORAGE_SECRET_KEY = os.getenv("OBJECT_STORAGE_SECRET_KEY")
OBJECT_STORAGE_BUCKET = os.getenv("OBJECT_STORAGE_BUCKET")
OBJECT_STORAGE_ENDPOINT = os.getenv("OBJECT_STORAGE_ENDPOINT")
OBJECT_STORAGE_FOLDER = os.getenv("OBJECT_STORAGE_FOLDER", "converted_images")
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET")


SPEAKERS = [
    "ndaeseong", "ndain", "ndonghyun", "nes_c_hyeri", "nes_c_kihyo",
    "nes_c_mikyung", "nes_c_sohyun", "neunseo", "neunwoo"
]

# ---------- Object Storage 업로드 ----------
def upload_to_object_storage(file_path, object_name) -> str:
    session = boto3.session.Session()
    s3 = session.client(
        service_name="s3",
        aws_access_key_id=OBJECT_STORAGE_ACCESS_KEY,
        aws_secret_access_key=OBJECT_STORAGE_SECRET_KEY,
        endpoint_url=OBJECT_STORAGE_ENDPOINT,
    )
    s3.upload_file(
        Filename=file_path,
        Bucket=OBJECT_STORAGE_BUCKET,
        Key=object_name,
        ExtraArgs={"ACL": "public-read", "ContentType": "image/png"}
    )
    return f"{OBJECT_STORAGE_ENDPOINT}/{OBJECT_STORAGE_BUCKET}/{object_name}"

# ---------- 발표 스크립트 생성 함수 (URL 기반) ----------
def generate_presentation_script_from_url(image_url: str, tone: str = "친절하고 명확하게"):
    headers = {
        "Content-Type": "application/json",
        "Authorization": CLOVA_API_KEY,
        "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4())
    }

    body = {
        "messages": [
            {"role": "system", "content": f"- {tone} 발표 스크립트를 작성하는 AI 발표자입니다."},
            {"role": "user", "content": [
                {"type": "text", "text": "이 이미지를 보고 1분 내외 발표자가 설명하는 발표 스크립트를 작성해줘. 청중이 이해하기 쉽게 핵심 내용을 중심으로 설명해. 앞에 인사부분은 제외해도되."},
                {"type": "image_url", "imageUrl": {"url": image_url}}
            ]}
        ],
        "maxTokens": 512
    }

    conn = http.client.HTTPSConnection(CLOVA_HOST)
    conn.request("POST", "/testapp/v3/chat-completions/HCX-005", json.dumps(body), headers)
    response = conn.getresponse()
    response_body = response.read().decode("utf-8")
    conn.close()

    if response.status != 200:
        return f"❌ Clova API 오류: {response.status} {response.reason}\n{response_body}"

    result = json.loads(response_body)
    return result.get("result", {}).get("message", {}).get("content", "응답이 없습니다.")

#음성 변환
def generate_tts(text, speaker, speed=0):
    url = "https://naveropenapi.apigw.ntruss.com/tts-premium/v1/tts"
    headers = {
        "X-NCP-APIGW-API-KEY-ID": NAVER_CLIENT_ID,
        "X-NCP-APIGW-API-KEY": NAVER_CLIENT_SECRET,
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {"speaker": speaker, "speed": str(speed), "format": "mp3", "text": text}
    r = requests.post(url, headers=headers, data=data)
    r.raise_for_status()
    filename = f"tts_{speaker}_{uuid.uuid4().hex[:6]}.mp3"
    with open(filename, "wb") as f:
        f.write(r.content)
    return filename
#음성 merge
def merge_audio_files(mp3_paths, output_path="final_podcast.mp3"):
    combined = AudioSegment.empty()
    for path in mp3_paths:
        audio = AudioSegment.from_file(path, format="mp3")
        combined += audio
    combined.export(output_path, format="mp3")
    return output_path

# ---------- PDF 변환 및 업로드 ----------
def pdf_to_images_and_upload(pdf_path):
    os.makedirs("temp", exist_ok=True)
    images = convert_from_path(pdf_path, poppler_path=POPPLER_PATH)
    uploaded_urls = []

    for i, page in enumerate(images):
        filename = f"page_{i+1}.png"
        local_path = os.path.join("temp", filename)

        # 비율 제한 (1:5 또는 5:1 이하로 제한)
        w, h = page.size
        aspect_ratio = max(w / h, h / w)
        if aspect_ratio > 5:
            if w > h:
                w = min(w, 2240)
                h = max(int(w / 5), 4)
            else:
                h = min(h, 2240)
                w = max(int(h / 5), 4)
            page = page.resize((w, h), Image.LANCZOS)

        # 긴 쪽이 2240px 이하, 짧은 쪽이 4px 이상 되도록 리사이징
        w, h = page.size
        if max(w, h) > 2240:
            scale = 2240 / max(w, h)
            w, h = int(w * scale), int(h * scale)
            page = page.resize((w, h), Image.LANCZOS)
        if min(w, h) < 4:
            scale = 4 / min(w, h)
            w, h = int(w * scale), int(h * scale)
            page = page.resize((w, h), Image.LANCZOS)

        page.save(local_path, "PNG")

        object_name = f"{OBJECT_STORAGE_FOLDER}/{uuid.uuid4().hex[:8]}_{filename}"
        image_url = upload_to_object_storage(local_path, object_name)
        uploaded_urls.append(image_url)

        os.remove(local_path)

    return uploaded_urls

# ---------- Streamlit UI ----------
st.set_page_config(page_title="📄 PDF → 발표 스크립트 생성기", layout="wide")
st.title("📄 PDF → 발표 스크립트 생성기 (NCP Object Storage 연동)")

uploaded_file = st.file_uploader("PDF 파일을 업로드하세요", type=["pdf"])

selected_speakers = st.multiselect(
    "TTS 목소리를 선택하세요:",
    options=SPEAKERS,
    default=SPEAKERS[:1],
    help="리스트에서 목소리를 선택하세요"
)


if uploaded_file:
    with st.spinner("파일 변환 및 업로드 중..."):
        try:
            file_ext = Path(uploaded_file.name).suffix.lower()
            with NamedTemporaryFile(delete=False, suffix=file_ext) as tmp:
                tmp.write(uploaded_file.read())
                tmp_path = tmp.name

            image_urls = pdf_to_images_and_upload(tmp_path)

        except Exception as e:
            st.error(f"오류 발생: {e}")
            st.stop()

    st.success(f"총 {len(image_urls)}개의 이미지 업로드 및 변환 완료!")

    for i, image_url in enumerate(image_urls, 1):
        st.markdown(f"### 페이지 {i}")
        col1, col2 = st.columns([1, 2])
        with col1:
            st.image(image_url)
        with col2:
            with st.spinner("스크립트 생성 중..."):
                script = generate_presentation_script_from_url(image_url)
            st.text_area("🗣️ 발표 스크립트", script, height=180, key=f"script_{i}")

        lines = [line.strip() for line in script.splitlines() if line.strip()]
        audio_files = []
        for idx, line in enumerate(lines):
            _, utter = line.split(":", 1) if ":" in line else (None, line)
            print(idx)
            speaker = selected_speakers[idx % len(selected_speakers)]
            try:
                path = generate_tts(utter, speaker=speaker)
                audio_files.append((speaker, path))
            except Exception as e:
                st.error(f"TTS 변환 오류 (라인 {idx+1}): {e}")

        # 병합된 오디오 출력
    try:
        st.subheader("🎧 병합된 전체 팟캐스트 오디오")
        merged_path = merge_audio_files([f for _, f in audio_files])

        # 파일 이름만 추출하여 download_button에 전달
        final_file_name = os.path.basename(merged_path)

        with open(merged_path, "rb") as f:
            merged_audio_bytes = f.read()
            st.audio(merged_audio_bytes, format="audio/mp3")
            st.download_button(
                "📥 병합된 오디오 다운로드",
                merged_audio_bytes,
                file_name=final_file_name,  # ⭐ 파일 이름만 전달 ⭐
                mime="audio/mp3"
            )
    except Exception as e:
        st.error(f"병합 오류: {e}")
