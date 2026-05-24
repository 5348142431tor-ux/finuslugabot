FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY bot_app ./bot_app
COPY creds /app/creds
CMD ["python", "-m", "bot_app.main"]
