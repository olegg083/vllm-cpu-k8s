# vLLM (CPU) в Kubernetes

Развёртывание `Qwen/Qwen2.5-0.5B-Instruct` на **vLLM 0.33.0** (только CPU) в Minikube:
rootless-образы с зафиксированными зависимостями, приватное логгирование запросов,
кэш весов модели в PVC, секреты и конфигурация через `Secret` и `ConfigMap`.

Бонусы: **1** - `kubectl apply -k k8s/`; **2** - загрузка весов в отдельном `initContainer`  
на лёгком rootless-образе; **3** - нагрузочное тестирование (раздел ниже).

## Структура

```text
.
├── vllm_server/        # образ vLLM: Dockerfile, custom_logger.py, requirements.txt
├── model_downloader/   # лёгкий образ для initContainer: Dockerfile, download_model.py, requirements.txt
├── k8s/                # kustomize-манифесты, config/ (ConfigMap), secrets.env.example
├── scripts/            # create-secrets.sh, check-privacy.sh
├── loadtest/           # loadtest.py и сырые результаты
```

## Требования

Docker, `kubectl`, Minikube. Проверено на машине с 16 ГБ RAM (WSL2 выдан 11 ГБ):

```bash
minikube start --driver=docker --cpus=6 --memory=7g
```

Под просит `requests: 2 CPU / 3 GiB`, `limits: 4 CPU / 6 GiB`. Лимит памяти меньше 6 ГиБ не рекомендуется.

## Запуск

Все команды - из корня репозитория.

```bash
# 1. Сборка образов
docker build -t my-vllm:0.33.0-1         vllm_server
docker build -t my-vllm-downloader:0.1.0 model_downloader

# 2. Загрузка образов в Minikube
minikube image load my-vllm:0.33.0-1
minikube image load my-vllm-downloader:0.1.0

# 3. Секреты (HF_TOKEN можно оставить пустым для публичной модели)
./scripts/create-secrets.sh

# 4. Деплой
kubectl apply -k k8s/
kubectl get pods -w
```

Первый старт: init-контейнер скачивает около 1 ГБ (1-2 минуты), затем vLLM загружает модель (около минуты).
Логи: `kubectl logs deploy/vllm -c download-model` и `kubectl logs deploy/vllm -c vllm`.

### Проверка

```bash
kubectl port-forward svc/vllm 8000:8000     # в отдельном терминале

curl -s localhost:8000/health                                      # 200 (без токена)
curl -s -o /dev/null -w "%{http_code}\n" localhost:8000/v1/models  # 401 без токена

curl -s localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $(cat .api_key)" -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen2.5-0.5B-Instruct","messages":[{"role":"user","content":"Привет! Ответь коротко."}],"max_tokens":64}'
```

Первый запрос после старта медленнее остальных (разогрев).

**Кэш модели.** `kubectl rollout restart deploy/vllm`, затем `kubectl logs deploy/vllm -c download-model`:
при повторном запуске модель берётся из PVC без обращения к сети.

**Приватность логов.** `./scripts/check-privacy.sh` отправляет запросы с уникальным маркером и проверяет,
что в логах пода нет ни маркера, ни промптов, но есть записи о факте обращения
(`Model request received: id=... prompt_tokens=... max_tokens=...`).

**Изменение параметров** без пересборки образа: правьте `k8s/config/vllm.yaml` и `k8s/config/model.env`,
затем `kubectl apply -k k8s/` (под перезапустится сам).

## Нагрузочное тестирование (бонус 3)

```bash
export VLLM_API_KEY=$(cat .api_key)
python3 loadtest/loadtest.py --max-tokens 64 --json-out loadtest/results/results.json
# свои сценарии: вид:конкурентность:запросов, вид = short | long
python3 loadtest/loadtest.py --scenarios short:8:32 long:1:4
```

TTFT - время до первого токена, Latency - полное время запроса, Throughput - выходные токены в секунду
по всем запросам. К каждому промпту добавляется уникальный префикс (prefix caching не искажает TTFT),
длина ответа фиксирована (`ignore_eos`), перед замером выполняется прогрев.

Условия: Minikube 7 GiB, 4 потока OpenMP, `enforce-eager`, `dtype=bfloat16`, `max-model-len=2048`,
`max-num-seqs=4`, `max-num-batched-tokens=1024`. Короткий промпт ~72 токена, длинный ~1800.

| Сценарий | Конкур. | Запросов | Ошибок | Токенов промпта | Токенов ответа | TTFT avg, с | TTFT p95, с | Latency avg, с | Latency p95, с | Throughput, ток/с | Req/s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| short | 1 | 8 | 0 | 73 | 64 | 1.42 | 1.58 | 6.04 | 6.59 | 10.6 | 0.17 |
| short | 4 | 16 | 0 | 72 | 64 | 3.87 | 4.96 | 15.27 | 16.01 | 16.8 | 0.26 |
| long | 1 | 4 | 0 | 1796 | 64 | 27.07 | 27.68 | 31.81 | 32.43 | 2.0 | 0.03 |
| long | 2 | 4 | 0 | 1796 | 64 | 38.41 | 54.99 | 62.11 | 76.76 | 2.1 | 0.03 |

### Аналитика 

В замерах: на коротких запросах конкурентность 1 -> 4 даёт рост throughput в 1,6 раза, но TTFT и latency растут
в 2,5-2,7 раза. На длинных префилл идёт со скоростью около 66 токенов/с, и вторая параллельная заявка
throughput не повышает (2,0 -> 2,1 ток/с), а только удваивает latency: префилл упирается в вычисления.

**А) Много запросов с малым числом токенов:**

- поднять `max-num-seqs` (память под KV-cache у модели 0.5B почти не растёт), согласовать с ним `max-num-batched-tokens`;
- оставить включёнными prefix caching и chunked prefill;
- масштабироваться репликами с непересекающимися ядрами (`VLLM_CPU_OMP_THREADS_BIND`).

**Б) Мало запросов, но с огромным контекстом:**

- подобрать `max-num-batched-tokens` под размер чанка префилла (растёт и память на прогреве);
- `long-prefill-token-threshold`, чтобы длинный запрос не задерживал короткие;
- поднять `max-model-len` и объём KV-cache;
- prefix caching для повторяющегося документа - основной рычаг по TTFT; для ускорения самого префилла нужны дополнительные ядра CPU.

Замеры сделаны на одной конфигурации одним прогоном. Рекомендации опираются на эти цифры и справку
`vllm serve --help`; сами изменения не замерялись. 

## Очистка

```bash
kubectl delete -k k8s/        # удалит и PVC с моделью
minikube delete
```
