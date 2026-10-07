FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2

WORKDIR /app

# Системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    tini \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright: скачиваем только chromium-headless-shell (headless=True) + системные зависимости для него
RUN playwright install --with-deps --only-shell chromium

COPY bot.py .
COPY config.py .
COPY database.py .
COPY health.py .
COPY memwatch.py .
COPY parser_manager.py .
COPY parsers/ ./parsers/
COPY freelance/ ./freelance/

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "bot.py"]