#!/usr/bin/env bash
# Проверка приватности логов: промпты и ответы не должны попадать в stdout/stderr пода,
# а факт обращения к модели - должен.
# Требуется: запущенный под и `kubectl port-forward svc/vllm 8000:8000` в другом терминале.
set -uo pipefail
cd "$(dirname "$0")/.."

URL="${URL:-http://localhost:8000}"
KEY="${VLLM_API_KEY:-$(cat .api_key)}"
MODEL="${MODEL:-Qwen/Qwen2.5-0.5B-Instruct}"
MARK="SECRET_MARKER_$(date +%s)"

call() { # $1 = endpoint, $2 = json
  curl -s -o /dev/null -w "%{http_code}" "$URL$1" \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d "$2"
}

echo "1) обычный запрос:        HTTP $(call /v1/chat/completions "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"${MARK}_chat\"}],\"max_tokens\":8}")"
echo "2) стриминг:              HTTP $(call /v1/chat/completions "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"${MARK}_stream\"}],\"max_tokens\":8,\"stream\":true}")"
echo "3) /v1/completions:       HTTP $(call /v1/completions "{\"model\":\"$MODEL\",\"prompt\":\"${MARK}_compl\",\"max_tokens\":4}")"
echo "4) невалидный запрос:     HTTP $(call /v1/chat/completions "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"${MARK}_bad\"}],\"max_tokens\":-5}")"

sleep 2
LOGS="$(kubectl logs deploy/vllm -c vllm 2>&1)"

LEAKS=$(grep -c "$MARK" <<<"$LOGS" || true)
FACTS=$(grep -c "Model request received" <<<"$LOGS" || true)

echo
echo "Строк с содержимым запросов в логах (должно быть 0): $LEAKS"
echo "Строк 'Model request received' в логах (должно быть >= 3): $FACTS"
grep "Model request received" <<<"$LOGS" | tail -3

if [[ "$LEAKS" -eq 0 && "$FACTS" -ge 3 ]]; then
  echo "OK: содержимое не логируется, факт обращения логируется."
else
  echo "FAIL: приватность логов нарушена или факт обращения не логируется." >&2
  exit 1
fi
