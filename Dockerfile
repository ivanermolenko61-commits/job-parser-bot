FROM python:3.11-slim

WORKDIR /app

# Системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright: скачиваем Chromium + системные зависимости для него
RUN playwright install --with-deps chromium

COPY bot.py .
COPY config.py .
COPY database.py .
COPY parser_manager.py .
COPY parsers/ ./parsers/

CMD ["python", "bot.py"]