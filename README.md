# vLLM (CPU) в Kubernetes: rootless-образ, приватные логи, PVC-кэш модели

Развёртывание `Qwen/Qwen2.5-0.5B-Instruct` на **vLLM 0.33.0** строго на CPU в Minikube.

| Требование | Как выполнено |
|---|---|
| CPU, vLLM ≥ 0.27 | официальный `vllm-openai-cpu` v0.33.0, закреплён по digest |
| Rootless | оба образа запускаются от UID 10001; в поде `runAsNonRoot`, `readOnlyRootFilesystem`, `drop: ALL`, seccomp `RuntimeDefault` |
| Зафиксированные зависимости | `vllm_server/requirements.txt` и `model_downloader/requirements.txt` - полный freeze; сборка падает при расхождении (`check_requirements.py`) |
| Приватные логи | `vllm_server/custom_logger.py`: логируется факт запроса, но не промпты/ответы |
| PVC-кэш модели | первый старт скачивает веса с HF в PVC, рестарты берут их локально без сети |
| Secret | HF-токен и Bearer-токен API в `Secret` |
| ConfigMap | все параметры vLLM в `k8s/config/` |
| Бонус 1 | `kubectl apply -k k8s/` |
| Бонус 2 | загрузка весов в отдельном `initContainer` на лёгком rootless-образе (222 МБ) |
| Бонус 3 | `loadtest/loadtest.py`, таблица метрик и аналитика ниже |

## Структура

```text
.
├── vllm_server/               # образ vLLM
│   ├── Dockerfile
│   ├── custom_logger.py       # приватное логгирование (точка входа)
│   ├── check_requirements.py  # сверка requirements.txt с окружением при сборке
│   └── requirements.txt       # все пакеты с точными версиями
├── model_downloader/          # отдельный лёгкий образ для initContainer
│   ├── Dockerfile
│   ├── download_model.py      # скрипт загрузки весов
│   └── requirements.txt
├── k8s/                       # kustomize-манифесты
│   ├── kustomization.yaml
│   ├── deployment.yaml  pvc.yaml  service.yaml
│   ├── config/vllm.yaml       # параметры vLLM -> ConfigMap
│   ├── config/model.env       # модель и CPU-переменные -> ConfigMap
│   └── secrets.env.example    # шаблон секретов (реальный secrets.env в .gitignore)
├── scripts/
│   ├── create-secrets.sh      # генерирует k8s/secrets.env и .api_key
│   └── check-privacy.sh       # проверка, что промпты не попадают в логи
└── loadtest/
    ├── loadtest.py            # нагрузочный тест (только stdlib)
    └── results/results.json   # сырые результаты замеров
```

## Требования к ресурсам

Проверено в Minikube (driver `docker`) на машине с 16 ГБ RAM (WSL2 выдан 11 ГБ):

```bash
minikube start --driver=docker --cpus=6 --memory=7g
```

Под просит `requests: 2 CPU / 3 GiB`, `limits: 4 CPU / 6 GiB`. В замере при старте под занимал около 4,1 ГиБ. Лимит меньше 6 ГиБ ставить не стоит (см. «Ограничения»).

## Быстрый старт

```bash
# 1. Образы (из корня репозитория)
docker build -t my-vllm:0.33.0-1        vllm_server
docker build -t my-vllm-downloader:0.1.0 model_downloader

# 2. Загрузить образы в Minikube
minikube image load my-vllm:0.33.0-1
minikube image load my-vllm-downloader:0.1.0

# 3. Секреты: создаёт k8s/secrets.env и .api_key (HF_TOKEN можно оставить пустым для публичной модели)
./scripts/create-secrets.sh

# 4. Деплой
kubectl apply -k k8s/

# 5. Следить за запуском
kubectl get pods -w
kubectl logs deploy/vllm -c download-model     # скачивание / "уже в PVC"
kubectl logs deploy/vllm -c vllm               # сервер
```

Первый старт: init-контейнер качает около 1 ГБ (1-2 минуты), затем vLLM грузит модель (~1 минута).

### Проверка API

```bash
kubectl port-forward svc/vllm 8000:8000     # в отдельном терминале

curl -s localhost:8000/health                                      # 200, без токена
curl -s -o /dev/null -w "%{http_code}\n" localhost:8000/v1/models  # 401 без токена

curl -s localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $(cat .api_key)" -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen2.5-0.5B-Instruct","messages":[{"role":"user","content":"Привет! Ответь коротко."}],"max_tokens":64}'
```

Первый запрос после старта заметно медленнее остальных (разогрев).

## Как это устроено

### Кэш модели в PVC
`initContainer` (`my-vllm-downloader`) запускает `download_model.py`:
- в PVC нет маркера `.download_complete` -> скачивает модель с HF (с повторами при сбоях) и пишет маркер с `model_id` и `revision`;
- маркер совпадает -> сразу выходит, **сеть не используется** (проверено: при перезапуске пода в логе `Модель ... уже в /models/..., сеть не нужна`);
- в каталоге лежит другая модель/ревизия -> ошибка, файлы разных моделей не смешиваются.

Основной контейнер монтирует PVC только на чтение и работает с `HF_HUB_OFFLINE=1`.
Проверить: `kubectl rollout restart deploy/vllm`, затем `kubectl logs deploy/vllm -c download-model`.

### Секреты
`scripts/create-secrets.sh` пишет `k8s/secrets.env` (в `.gitignore`), из которого kustomize генерирует `Secret/vllm-secrets` с ключами:
- `HF_TOKEN` - для скачивания закрытых/gated моделей (в init-контейнер);
- `VLLM_API_KEY` - Bearer-токен для клиентов (в vLLM через env, а не через `--api-key`, чтобы он не попал в argv).

В Secret-имя добавляется хэш: смена секрета перезапускает под. Шаблон: `k8s/secrets.env.example`.

### Конфигурация
- `k8s/config/vllm.yaml` - флаги `vllm serve` (`max-model-len`, `max-num-seqs`, `dtype`, ...) монтируются как `/etc/vllm/vllm.yaml`;
- `k8s/config/model.env` - `MODEL_ID`, `MODEL_REVISION`, `MODEL_DIR` и CPU-переменные (`VLLM_CPU_KVCACHE_SPACE`, `VLLM_CPU_OMP_THREADS_BIND`).

Изменили файл -> `kubectl apply -k k8s/`: ConfigMap получает новый хэш, под перезапускается сам, образ не пересобирается.

### Приватное логгирование
`custom_logger.py` перед запуском `vllm serve` подменяет методы штатного `RequestLogger`:
- `log_inputs` пишет только `id`, число токенов промпта и `max_tokens`;
- `log_request_body` и `log_outputs` отключены;
- уровень логов принудительно `INFO` (на `DEBUG` vLLM печатает промпты).

Пример строки в логе: `Model request received: id=chatcmpl-... prompt_tokens=38 max_tokens=24`.

Проверка (нужен `port-forward`):

```bash
./scripts/check-privacy.sh
```

Скрипт отправляет запросы с уникальным маркером (обычный, стриминг, `/v1/completions`, невалидный с ошибкой 400) и убеждается, что маркера в логах нет, а записи о запросах есть. Результат прогона: `0` строк с маркером, `OK`.

> Патч применяется в процессе API-сервера. Не используйте `api-server-count > 1`: дочерние API-процессы запускаются без патча.

### Безопасность
- UID 10001 с записью в `/etc/passwd` (иначе часть библиотек падает на `getpass.getuser()`).
- Pod: `runAsNonRoot`, `readOnlyRootFilesystem` (записываемые `/tmp`, `/home/vllm`, `/dev/shm` - `emptyDir`), `allowPrivilegeEscalation: false`, `drop: ALL`, seccomp `RuntimeDefault`, `automountServiceAccountToken: false`.
- API защищён Bearer-токеном (`/health` открыт для проб).
- Отключены телеметрия и статистика использования (`VLLM_NO_USAGE_STATS`, `DO_NOT_TRACK`, `HF_HUB_DISABLE_TELEMETRY`).
- Реальные секреты в git не попадают (`k8s/secrets.env`, `.api_key` в `.gitignore`).

### Воспроизводимость
Базовые образы закреплены по digest, `requirements.txt` - полный список `name==version`. На сборке `check_requirements.py` сравнивает его с реально установленными пакетами и падает при любом расхождении. Для воспроизводимости модели можно закрепить `MODEL_REVISION` (commit hash) в `k8s/config/model.env`.

## Бонус 3: нагрузочное тестирование

Запуск (при работающем `port-forward`):

```bash
export VLLM_API_KEY=$(cat .api_key)
python3 loadtest/loadtest.py --max-tokens 64 --json-out loadtest/results/results.json
# свои сценарии вида вид:конкурентность:запросов, вид = short | long
python3 loadtest/loadtest.py --scenarios short:8:32 long:1:4
```

Метрики: **TTFT** - время до первого токена (стриминг), **Latency** - полное время запроса, **Throughput** - выходные токены в секунду по всем запросам (`sum(completion_tokens) / время теста`). Чтобы цифры были честными: к каждому промпту добавляется уникальный префикс (prefix caching не искажает TTFT), `ignore_eos` фиксирует длину ответа в 64 токена, перед замером делается прогрев, число токенов берётся из `usage` сервера.

Условия: Minikube 7 GiB / 4 потока OpenMP, `enforce-eager`, `dtype=bfloat16`, `max-model-len=2048`, `max-num-seqs=4`, `max-num-batched-tokens=1024`. Короткий промпт ~72 токена, длинный ~1800 токенов (контекст модели 2048).

| Сценарий | Конкур. | Запросов | Ошибок | Токенов промпта | Токенов ответа | TTFT avg, с | TTFT p95, с | Latency avg, с | Latency p95, с | Throughput, ток/с | Req/s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| short | 1 | 8 | 0 | 73 | 64 | 1.42 | 1.58 | 6.04 | 6.59 | 10.6 | 0.17 |
| short | 4 | 16 | 0 | 72 | 64 | 3.87 | 4.96 | 15.27 | 16.01 | 16.8 | 0.26 |
| long | 1 | 4 | 0 | 1796 | 64 | 27.07 | 27.68 | 31.81 | 32.43 | 2.0 | 0.03 |
| long | 2 | 4 | 0 | 1796 | 64 | 38.41 | 54.99 | 62.11 | 76.76 | 2.1 | 0.03 |

Что видно в цифрах:
- **Короткие запросы.** С ростом конкурентности с 1 до 4 суммарный throughput растёт в 1,6 раза (10,6 -> 16,8 ток/с), а TTFT и latency растут в 2,5-2,7 раза: четыре запроса делят одни и те же 4 ядра.
- **Длинные запросы.** TTFT ≈ 27 с на ~1800 токенов, то есть префилл идёт со скоростью около 66 токенов/с. Конкурентность 2 throughput не повышает (2,0 -> 2,1 ток/с), а TTFT и latency растут почти вдвое: префилл упирается в вычисления, и запросы просто стоят в очереди.

> Это замеры на одной конкретной (скромной) конфигурации, одной выборкой; для статистики нужны повторы. Рекомендации ниже опираются на эти цифры и документацию vLLM (`vllm serve --help`), сами изменения я **не** прогонял.

### Что менять для роста Throughput

**А) Большой поток запросов с малым числом токенов.** Узкое место - декодирование небольших запросов и накладные расходы на планирование.
- Поднять `max-num-seqs` (сейчас 4 -> 16-32). KV-cache у модели 0.5B занимает около 12 КБ на токен, так что память под параллельные последовательности почти не растёт, а декод-шаг обрабатывает шире батч. Это главный рычаг: в замере throughput рос с конкурентностью, а лимит на 4 последовательности мешает ему расти дальше.
- Согласовать `max-num-batched-tokens` с `max-num-seqs` и типичным размером промпта: за шаг должны помещаться префиллы новых запросов и декод активных.
- Оставить включёнными `enable-prefix-caching` (общий системный промпт считается один раз) и chunked prefill (префилл не блокирует декод).
- `async-scheduling` (флаг есть в 0.33) скрывает паузы планировщика между шагами; проверить на вашей нагрузке.
- Если памяти хватает, убрать `enforce-eager`: `torch.compile` ускоряет CPU-инференс (но на 6 ГиБ с компиляцией под убивался по OOM - см. ниже).
- Привязать потоки к физическим ядрам (`VLLM_CPU_OMP_THREADS_BIND`) и масштабироваться репликами с непересекающимися ядрами, а не раздувать один под: суперлинейного роста от числа потоков на CPU ждать не стоит.

**Б) Меньше запросов, но с огромным контекстом.** Узкое место - префилл (compute-bound); в замере именно он дал TTFT 27-55 с.
- Включённый chunked prefill оставить; подобрать `max-num-batched-tokens` под размер чанка: крупные чанки эффективнее по матричным операциям, но дают всплеск памяти на прогреве (с значением по умолчанию у нас не хватало памяти при выделении KV-cache, помогло 1024), поэтому увеличивать вместе с лимитом памяти.
- `long-prefill-token-threshold` ограничивает долю шага, которую может занять один длинный префилл, - защищает latency коротких запросов, идущих рядом с длинными.
- Поднять `max-model-len` ровно до нужного размера и проверить `VLLM_CPU_KVCACHE_SPACE` / `kv-cache-memory-bytes`: для длинного контекста KV-cache нужно больше (для этой модели около 12 КБ на токен, для больших моделей - на порядки больше).
- Уменьшить `max-num-seqs`: при редких, но тяжёлых запросах нет смысла резервировать память под много последовательностей.
- Если один и тот же длинный документ запрашивают повторно, `enable-prefix-caching` убирает почти весь префилл (в тесте его нарочно ломали уникальным префиксом) - это самый сильный рычаг по TTFT.
- Для ускорения самого префилла помогают только дополнительные ядра CPU: рост конкурентности его не ускоряет (видно в таблице).

## Ограничения и найденные подводные камни

- **Память.** С `torch.compile` под убивался по OOM даже на 6 ГиБ; поэтому в `vllm.yaml` стоит `enforce-eager: true` (пик около 4,1 ГиБ). Большой `max-num-batched-tokens` раздувает память на прогреве (логиты по словарю 150 тыс.).
- **`VLLM_PORT` от Service.** Service с именем `vllm` заставляет k8s подставить в под переменную `VLLM_PORT=tcp://...`, и vLLM падает. Поэтому в `deployment.yaml` стоит `enableServiceLinks: false`.
- **Первый запрос медленный** (разогрев): в нагрузочном тесте он выполняется до замеров.
- Образы грузятся в Minikube локально (`imagePullPolicy: IfNotPresent`, теги не `latest`). Для реального кластера запушьте образы в registry и поправьте `images:` в `k8s/kustomization.yaml`.
- Содержимое запросов не пишется в логи приложения; логи самого Kubernetes/ноды и прокси перед сервисом этим патчем не охватываются.

## Очистка

```bash
kubectl delete -k k8s/        # удалит и PVC с моделью
minikube delete
```
