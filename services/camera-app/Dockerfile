FROM python:3.11-alpine

ENV PYTHONUNBUFFERED=1

RUN apk add --no-cache ffmpeg ttf-dejavu

WORKDIR /app

COPY services/camera-app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/camera-app /app
COPY services/common /app/common

EXPOSE 8090

CMD ["python", "main.py"]
