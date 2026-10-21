FROM python:3.11-slim

# ffmpeg is required for audio conversion (voiceover jobs produce mp3)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Piper offline TTS (voiceover jobs run on-server; no network calls, no IP wall)
RUN mkdir -p /app/voices \
    && cd /app/voices \
    && for f in \
        ur/ur_PK/aegis_female/medium/ur_PK-aegis_female-medium \
        ur/ur_PK/fasih/medium/ur_PK-fasih-medium \
        hi/hi_IN/priyamvada/medium/hi_IN-priyamvada-medium \
        hi/hi_IN/rohan/medium/hi_IN-rohan-medium \
        en/en_US/amy/medium/en_US-amy-medium \
        en/en_US/ryan/medium/en_US-ryan-medium \
        en/en_GB/alan/medium/en_GB-alan-medium \
        en/en_GB/alba/medium/en_GB-alba-medium \
      ; do \
        curl -fL --retry 3 "https://huggingface.co/rhasspy/piper-voices/resolve/main/$f.onnx" -o "$(basename $f).onnx" \
        && curl -fL --retry 3 "https://huggingface.co/rhasspy/piper-voices/resolve/main/$f.onnx.json" -o "$(basename $f).onnx.json" \
      ; done \
    && ls -la /app/voices
# Build-time smoke test: fails the build if piper/espeak data is broken
RUN echo "This is a smoke test." | python -m piper --model /app/voices/en_US-amy-medium.onnx --config /app/voices/en_US-amy-medium.onnx.json --output_file /tmp/smoke.wav && test -s /tmp/smoke.wav && rm /tmp/smoke.wav

COPY . .

# Generated files live here (audio cache); the SQLite file too when DATABASE_URL is unset
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
