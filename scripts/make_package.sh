#!/usr/bin/env bash
# Gera o pacote de distribuição com manifesto SHA-256.
# Rode isto sempre que alterar qualquer arquivo do projeto.
set -euo pipefail
cd "$(dirname "$0")/.."

# 1) gerar manifesto de TODOS os arquivos distribuíveis
find src scripts docs tests playbooks kb -type f ! -name '*.pyc' ! -path '*__pycache__*' \
  | sort > /tmp/ainoc_files
printf '%s\n' requirements.txt .env.example README.md Dockerfile docker-compose.yml install.sh >> /tmp/ainoc_files
sha256sum $(cat /tmp/ainoc_files) > MANIFEST.sha256
echo "[OK] MANIFEST.sha256 com $(wc -l < MANIFEST.sha256) arquivos."

# 2) empacotar
VERSION="${1:-0.1.0}"
tar czf "ai-noc-analyst-${VERSION}.tar.gz" \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='.venv' --exclude='.env' \
  $(cat /tmp/ainoc_files) MANIFEST.sha256
sha256sum "ai-noc-analyst-${VERSION}.tar.gz"
echo "[OK] Pacote pronto: ai-noc-analyst-${VERSION}.tar.gz"
