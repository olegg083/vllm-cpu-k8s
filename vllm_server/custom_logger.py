#!/usr/bin/env python3
"""Точка входа vLLM с приватным логгированием запросов.
"""

import os
import sys

os.environ["VLLM_LOGGING_LEVEL"] = "INFO"

from vllm.entrypoints.serve.utils.request_logger import (  # noqa: E402
    RequestLogger,
    logger as request_log,
)


def _safe_log_inputs(
    self,
    request_id,
    prompt=None,
    prompt_token_ids=None,
    prompt_embeds=None,
    params=None,
    lora_request=None,
):
    """Только метаданные: сам промпт (текст/токены/эмбеддинги) не логируется."""
    if prompt_token_ids is not None:
        prompt_tokens = len(prompt_token_ids)
    elif prompt_embeds is not None:
        prompt_tokens = int(prompt_embeds.shape[0])
    else:
        prompt_tokens = -1
    request_log.info(
        "Model request received: id=%s prompt_tokens=%s max_tokens=%s",
        request_id,
        prompt_tokens,
        getattr(params, "max_tokens", None),
    )


def _noop(self, *args, **kwargs):
    """Тело запроса и ответы модели не логируются вообще."""
    return None


RequestLogger.log_inputs = _safe_log_inputs
RequestLogger.log_request_body = _noop
RequestLogger.log_outputs = _noop


def main() -> None:
    from vllm.entrypoints.cli.main import main as vllm_main

    sys.argv = ["vllm", *sys.argv[1:]]
    vllm_main()


if __name__ == "__main__":
    main()
