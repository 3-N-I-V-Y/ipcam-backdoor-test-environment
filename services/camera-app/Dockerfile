FROM python:3.11-alpine

ENV PYTHONUNBUFFERED=1

RUN apk add --no-cache ffmpeg

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

EXPOSE 8090

CMD ["python", "main.py"]
