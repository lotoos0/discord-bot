FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .

# ffmpeg + libopus + libsodium do audio
RUN apt-get update \
      && apt-get install -y --no-install-recommends ffmpeg libopus0 libsodium23 \
      && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -r requirements.txt
COPY . .

CMD ["python", "main.py"]

