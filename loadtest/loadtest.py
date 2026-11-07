#!/usr/bin/env python3
"""Нагрузочный тест OpenAI-совместимого эндпоинта vLLM (только стандартная библиотека).

Метрики по каждому сценарию:
  TTFT        время до первого токена ответа (стриминг, SSE), с
  Latency     полное время запроса до конца ответа, с
  Throughput  выходные токены в секунду по всем запросам: sum(completion_tokens) / время теста
  Req/s       завершённых запросов в секунду

Особенности, чтобы цифры были честными:
  * к каждому промпту добавляется уникальный префикс -> prefix caching не искажает TTFT;
  * ignore_eos=true -> каждый запрос генерирует ровно max_tokens токенов;
  * перед замером выполняется прогревочный запрос (первый запрос на CPU заметно медленнее);
  * число токенов берётся из usage сервера (stream_options.include_usage).

Пример:
  export VLLM_API_KEY=$(cat .api_key)
  python3 loadtest/loadtest.py --url http://localhost:8000
  python3 loadtest/loadtest.py --scenarios short:1:8 long:2:4 --max-tokens 64
"""

import argparse
import http.client
import json
import os
import statistics
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

SHORT_PROMPT = "Объясни одним предложением, что такое Kubernetes."
FILLER = (
    "Kubernetes автоматизирует развёртывание, масштабирование и управление контейнеризированными "
    "приложениями. Кластер состоит из управляющей плоскости и рабочих узлов, на которых запускаются "
    "поды. Манифесты описывают желаемое состояние, а контроллеры приводят систему к нему. "
)


def build_prompt(kind: str, long_words: int) -> str:
    uniq = uuid.uuid4().hex  # уникальный префикс против prefix caching
    if kind == "short":
        return f"[{uniq}] {SHORT_PROMPT}"
    reps = max(1, long_words // len(FILLER.split()))
    return f"[{uniq}] " + FILLER * reps + "\nСформулируй краткий вывод из текста выше."


def one_request(url, api_key, model, prompt, max_tokens, timeout):
    u = urlparse(url)
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0,
            "ignore_eos": True,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    )
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    t0 = time.perf_counter()
    ttft = None
    usage = {}
    try:
        conn.request(
            "POST",
            "/v1/chat/completions",
            body,
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        if resp.status != 200:
            return {"ok": False, "error": f"HTTP {resp.status}"}
        for raw in resp:
            line = raw.decode().strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            chunk = json.loads(payload)
            if chunk.get("usage"):
                usage = chunk["usage"]
            for choice in chunk.get("choices", []):
                if ttft is None and choice.get("delta", {}).get("content"):
                    ttft = time.perf_counter() - t0
        latency = time.perf_counter() - t0
        return {
            "ok": True,
            "ttft": ttft if ttft is not None else latency,
            "latency": latency,
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


def run_scenario(args, kind, concurrency, n_requests):
    prompts = [build_prompt(kind, args.long_words) for _ in range(n_requests)]
    results = []
    lock = threading.Lock()

    def worker(prompt):
        r = one_request(args.url, args.api_key, args.model, prompt, args.max_tokens, args.timeout)
        with lock:
            results.append(r)

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(worker, prompts))
    wall = time.perf_counter() - t0

    ok = [r for r in results if r["ok"]]
    errors = len(results) - len(ok)
    if not ok:
        return {"scenario": kind, "concurrency": concurrency, "requests": n_requests, "errors": errors}
    ttft = [r["ttft"] for r in ok]
    lat = [r["latency"] for r in ok]
    out_tokens = sum(r["completion_tokens"] for r in ok)
    return {
        "scenario": kind,
        "concurrency": concurrency,
        "requests": n_requests,
        "errors": errors,
        "prompt_tokens_avg": round(statistics.mean(r["prompt_tokens"] for r in ok)),
        "output_tokens_avg": round(out_tokens / len(ok)),
        "ttft_avg": statistics.mean(ttft),
        "ttft_p95": pct(ttft, 95),
        "latency_avg": statistics.mean(lat),
        "latency_p95": pct(lat, 95),
        "throughput_tok_s": out_tokens / wall,
        "req_s": len(ok) / wall,
        "wall_s": wall,
    }


def to_markdown(rows):
    head = (
        "| Сценарий | Конкур. | Запросов | Ошибок | Токенов промпта | Токенов ответа | "
        "TTFT avg, с | TTFT p95, с | Latency avg, с | Latency p95, с | Throughput, ток/с | Req/s |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|\n"
    )
    lines = []
    for r in rows:
        if "ttft_avg" not in r:
            lines.append(f"| {r['scenario']} | {r['concurrency']} | {r['requests']} | {r['errors']} | все запросы упали |||||||| ")
            continue
        lines.append(
            f"| {r['scenario']} | {r['concurrency']} | {r['requests']} | {r['errors']} | {r['prompt_tokens_avg']} | "
            f"{r['output_tokens_avg']} | {r['ttft_avg']:.2f} | {r['ttft_p95']:.2f} | {r['latency_avg']:.2f} | "
            f"{r['latency_p95']:.2f} | {r['throughput_tok_s']:.1f} | {r['req_s']:.2f} |"
        )
    return head + "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--api-key", default=os.environ.get("VLLM_API_KEY", ""))
    p.add_argument("--model", default="Qwen/Qwen2.5-0.5B-Instruct")
    p.add_argument("--max-tokens", type=int, default=64)
    p.add_argument("--long-words", type=int, default=600, help="размер длинного промпта в словах (русский текст ~2.4 токена на слово; контекст модели 2048)")
    p.add_argument("--timeout", type=float, default=600)
    p.add_argument(
        "--scenarios",
        nargs="+",
        default=["short:1:8", "short:4:16", "long:1:4", "long:2:4"],
        help="формат вид:конкурентность:запросов, где вид = short|long",
    )
    p.add_argument("--json-out", help="сохранить сырые результаты в JSON")
    args = p.parse_args()
    if not args.api_key:
        p.error("нужен API-ключ: --api-key или переменная VLLM_API_KEY")

    print("Прогрев...", flush=True)
    for kind in ("short", "long"):
        r = one_request(args.url, args.api_key, args.model, build_prompt(kind, args.long_words), 8, args.timeout)
        if not r["ok"]:
            raise SystemExit(f"Прогревочный запрос ({kind}) не удался: {r['error']}")

    rows = []
    for spec in args.scenarios:
        kind, conc, n = spec.split(":")
        print(f"Сценарий {spec} ...", flush=True)
        rows.append(run_scenario(args, kind, int(conc), int(n)))
        print(to_markdown(rows[-1:]).splitlines()[-1], flush=True)

    print("\n" + to_markdown(rows))
    if args.json_out:
        os.makedirs(os.path.dirname(args.json_out) or ".", exist_ok=True)
        with open(args.json_out, "w") as f:
            json.dump(rows, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
