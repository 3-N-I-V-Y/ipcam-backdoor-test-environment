FROM alpine:3.20

RUN apk add --no-cache ffmpeg wget bash

WORKDIR /app
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

CMD ["/app/start.sh"]