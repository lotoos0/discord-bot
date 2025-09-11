FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# Jeśli Twój bot startuje inaczej — zamień komendę poniżej
CMD ["python", "main.py"]

