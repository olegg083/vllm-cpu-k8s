#!/usr/bin/env bash
# Создаёт k8s/secrets.env и .api_key для curl
# Существующий API-ключ переиспользуется
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -s .api_key ]]; then
  API_KEY="$(<.api_key)"
else
  API_KEY="$(openssl rand -hex 32)"
fi

HF_TOKEN="${HF_TOKEN:-}"
if [[ -z "$HF_TOKEN" && -t 0 ]]; then
  read -rsp "HF_TOKEN (Enter, если модель публичная): " HF_TOKEN
  echo
fi

umask 077
printf 'HF_TOKEN=%s\nVLLM_API_KEY=%s\n' "$HF_TOKEN" "$API_KEY" > k8s/secrets.env
printf '%s\n' "$API_KEY" > .api_key
echo "Готово: k8s/secrets.env и .api_key (оба в .gitignore)."
