FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY config.py .
COPY database.py .
COPY parser_manager.py .
COPY parsers/ ./parsers/

CMD ["python", "bot.py"]
