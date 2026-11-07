# vLLM 0.33.0 (CPU) - rootless образ с приватным логгированием.
# База (vllm/vllm-openai-cpu:v0.33.0) закреплена по digest. Python-зависимости
# жёстко зафиксированы в requirements.txt (полный freeze, включая транзитивные),
# и сборка падает при любом расхождении (check_requirements.py).
FROM vllm/vllm-openai-cpu@sha256:87167cf0ec6e1e9607bb1cf9bf618a67d04963236a34bd955ac75b7d31a6edf5

ENV PYTHONUNBUFFERED=1 \
    HOME=/home/vllm \
    VLLM_CACHE_ROOT=/home/vllm/.cache/vllm \
    VLLM_NO_USAGE_STATS=1 \
    DO_NOT_TRACK=1 \
    HF_HUB_DISABLE_TELEMETRY=1

RUN groupadd --gid 10001 vllm \
 && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/vllm \
        --shell /usr/sbin/nologin vllm \
 && mkdir -p /models /home/vllm/.cache/vllm \
 && chown -R 10001:10001 /models /home/vllm

WORKDIR /app

COPY requirements.txt check_requirements.py /app/
RUN python /app/check_requirements.py /app/requirements.txt

COPY --chown=10001:10001 custom_logger.py /app/custom_logger.py

USER 10001:10001

EXPOSE 8000

# Параметры запуска берутся из ConfigMap
ENTRYPOINT ["python", "/app/custom_logger.py"]
CMD ["serve", "--config", "/etc/vllm/vllm.yaml"]
