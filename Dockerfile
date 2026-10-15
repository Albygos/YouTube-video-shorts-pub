FROM python:3.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 WORK_DIR=/tmp/malayalam-reels WHISPER_MODEL=base MAX_CONCURRENT_JOBS=1 MAX_VIDEO_MINUTES=240 MAX_UPLOAD_MB=90
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-noto-core fonts-noto-extra ca-certificates curl unzip nodejs npm && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt
COPY . .
RUN mkdir -p /tmp/malayalam-reels && useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app /tmp/malayalam-reels
USER appuser
EXPOSE 10000
CMD ["sh","-c","uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}"]
