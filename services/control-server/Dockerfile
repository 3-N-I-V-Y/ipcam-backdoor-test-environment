FROM python:3.12-slim

WORKDIR /app

COPY services/control-server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/control-server/app.py .
COPY services/common /app/common

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
