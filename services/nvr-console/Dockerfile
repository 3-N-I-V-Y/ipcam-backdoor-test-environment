FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY services/nvr-console/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/nvr-console/app.py /app/app.py
COPY services/nvr-console/templates /app/templates
COPY services/nvr-console/static /app/static
COPY services/common /app/common

EXPOSE 8091

CMD ["python", "app.py"]
