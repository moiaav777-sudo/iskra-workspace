# Рабочее пространство целиком в одном контейнере: портал, сервисы и шлюз (run.sh).
# Сборка:  docker compose build      Запуск:  docker compose up -d      Адрес: http://localhost:8080
# Образ тяжёлый из-за LibreOffice (перевод .doc/.rtf/.odt и PDF-выгрузки) и ffmpeg («Транскрибатор»);
# без них всё работает, кроме этих двух возможностей — уберите строку apt-get, если они не нужны.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libreoffice-writer libreoffice-calc libreoffice-impress ffmpeg fonts-dejavu fonts-liberation ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app
RUN ./run.sh --install

# данные и настройки живут в томе: /app/data (базы, папки документов, журналы) и /app/data/.env
ENV ENV_FILE=/app/data/.env HOST=0.0.0.0 PORT=8080
VOLUME ["/app/data"]
EXPOSE 8080
CMD ["./run.sh", "--no-install"]
