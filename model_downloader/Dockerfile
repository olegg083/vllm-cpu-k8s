# Лёгкий rootless-образ только для скачивания весов (initContainer)
# База закреплена по digest (python:3.12-slim), зависимости - в requirements.txt
FROM python@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/downloader \
    HF_HOME=/tmp/hf \
    HF_HUB_DISABLE_TELEMETRY=1

RUN groupadd --gid 10001 downloader \
 && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/downloader \
        --shell /usr/sbin/nologin downloader \
 && mkdir -p /models \
 && chown 10001:10001 /models

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt && pip check

COPY --chown=10001:10001 download_model.py /app/download_model.py

USER 10001:10001

ENTRYPOINT ["python", "/app/download_model.py"]
