#!/usr/bin/env bash
#===============================================================================
# AI NOC Analyst for Zabbix — Instalador para Ubuntu 22.04/24.04
#
# - Verifica a integridade de TODOS os arquivos (MANIFEST.sha256) antes de
#   instalar: se algo estiver faltando, corrompido ou trocado, ele ABORTA.
# - Instala em /opt/ai-noc-analyst com venv Python isolado.
# - Cria serviço systemd (ainoc.service) rodando com usuário dedicado.
# - Comandos utilitários: seed do laboratório e simulação de problems.
#
# Uso:
#   sudo ./install.sh install      # instalação/atualização completa
#   sudo ./install.sh uninstall    # remove serviço e arquivos
#   ./install.sh verify            # só confere integridade do pacote
#   ainoc seed                     # (pós-instalação) popula o lab Zabbix
#   ainoc simulate cpu lab-web-01  # dispara um problem de teste
#   ainoc status | logs | restart
#===============================================================================
set -euo pipefail
export PATH="/usr/local/sbin:/usr/sbin:/sbin:$PATH"

INSTALL_DIR="/opt/ai-noc-analyst"
SERVICE_NAME="ainoc"
SERVICE_USER="ainoc"
PORT="8000"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_YEL='\033[1;33m'; C_OFF='\033[0m'
ok()   { echo -e "${C_GREEN}[OK]${C_OFF} $*"; }
warn() { echo -e "${C_YEL}[!]${C_OFF} $*"; }
die()  { echo -e "${C_RED}[ERRO]${C_OFF} $*" >&2; exit 1; }

need_root() { [[ $EUID -eq 0 ]] || die "Execute com sudo: sudo $0 $*"; }

#--------------------------------------------------------------- verificação
verify_manifest() {
  cd "$SRC_DIR"
  [[ -f MANIFEST.sha256 ]] || die "MANIFEST.sha256 não encontrado. Pacote incompleto — baixe novamente."
  echo "[i] Verificando integridade do pacote..."
  local fails
  if ! fails=$(sha256sum --check --quiet MANIFEST.sha256 2>&1); then
    echo "$fails" >&2
    die "Arquivos corrompidos ou trocados detectados. NÃO instale este pacote."
  fi
  # arquivos extras que não estão no manifesto (exceto artefatos locais)
  local extra
  extra=$(find src scripts -type f ! -name '*.pyc' ! -path '*__pycache__*' 2>/dev/null \
          | sort | comm -23 - <(awk '{print $2}' MANIFEST.sha256 | grep -E '^(src|scripts)/' | sort) || true)
  [[ -z "$extra" ]] || warn "Arquivos não previstos no pacote (ignorados na cópia):\n$extra"
  ok "Integridade confirmada ($(wc -l < MANIFEST.sha256) arquivos)."
}

#--------------------------------------------------------------- dependências
check_deps() {
  command -v python3 >/dev/null || die "python3 não encontrado. apt install python3"
  local pyver
  pyver=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' \
    || die "Python >= 3.10 é necessário (encontrado: $pyver)."
  ok "Python $pyver"
  if ! python3 -m venv --help >/dev/null 2>&1; then
    echo "[i] Instalando python3-venv..."
    apt-get update -qq && apt-get install -y -qq python3-venv python3-pip
  fi
  command -v curl >/dev/null || apt-get install -y -qq curl
}

#--------------------------------------------------------------- instalação
do_install() {
  need_root install
  verify_manifest
  check_deps

  id -u "$SERVICE_USER" &>/dev/null || {
    if command -v useradd >/dev/null; then
      useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
    elif command -v adduser >/dev/null; then
      adduser --system --group --home "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
    else
      die "Nem useradd nem adduser encontrados no PATH."
    fi
    ok "Usuário de serviço '$SERVICE_USER' criado."
  }

  echo "[i] Copiando arquivos verificados para $INSTALL_DIR..."
  mkdir -p "$INSTALL_DIR"
  # copia SOMENTE o que está no manifesto — impossível levar arquivo errado
  while read -r _hash file; do
    install -D -m 0644 "$SRC_DIR/$file" "$INSTALL_DIR/$file"
  done < "$SRC_DIR/MANIFEST.sha256"
  chmod +x "$INSTALL_DIR"/scripts/*.py "$INSTALL_DIR/install.sh" 2>/dev/null || true

  echo "[i] Criando/atualizando venv..."
  python3 -m venv "$INSTALL_DIR/.venv"
  "$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
  "$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
  ok "Dependências instaladas."

  # .env: nunca sobrescrever configuração existente
  if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    configure_env
  else
    warn ".env existente preservado (edite com: sudo nano $INSTALL_DIR/.env)"
  fi
  # bind: 127.0.0.1 quando o Zabbix roda neste mesmo servidor (recomendado)
  if ! grep -q '^AINOC_BIND_HOST=' "$INSTALL_DIR/.env"; then
    read -rp "O Zabbix server roda NESTE mesmo servidor? [S/n]: " local_zbx
    if [[ "${local_zbx,,}" == "n" ]]; then
      echo "AINOC_BIND_HOST=0.0.0.0" >> "$INSTALL_DIR/.env"
      warn "Serviço exposto em todas as interfaces (porta ${PORT})."
      warn "Restrinja no firewall à origem do Zabbix, ex.:"
      warn "  nft add rule inet filter input ip saddr <IP_ZABBIX> tcp dport ${PORT} accept"
      warn "  (ou: ufw allow from <IP_ZABBIX> to any port ${PORT} proto tcp)"
    else
      echo "AINOC_BIND_HOST=127.0.0.1" >> "$INSTALL_DIR/.env"
      ok "Bind em 127.0.0.1 — porta ${PORT} inacessível pela internet."
      ok "No media type do Zabbix use: http://127.0.0.1:${PORT}/webhook/zabbix"
    fi
  fi
  chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
  chmod 600 "$INSTALL_DIR/.env"

  install_systemd
  install_cli

  systemctl restart "$SERVICE_NAME"
  sleep 2
  if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    ok "Serviço no ar: http://$(hostname -I | awk '{print $1}'):${PORT}/health"
  else
    warn "Serviço iniciado mas /health ainda não respondeu."
    warn "Veja os logs: journalctl -u $SERVICE_NAME -f"
  fi

  if grep -q '^AINOC_AI_PROVIDER=claude_code' "$INSTALL_DIR/.env"; then
    if command -v claude >/dev/null; then
      ok "Claude Code CLI encontrado: $(command -v claude)"
    else
      warn "Provider 'claude_code' selecionado mas o CLI 'claude' não está no PATH."
      warn "Instale: npm install -g @anthropic-ai/claude-code"
    fi
    warn "Autentique o Claude Code PARA O USUÁRIO DO SERVIÇO:"
    warn "  sudo -u $SERVICE_USER HOME=$INSTALL_DIR claude setup-token"
    warn "(ou use AINOC_CLAUDE_CODE_BARE=true + AINOC_AI_API_KEY no .env)"
  fi

  cat <<EOF

============================================================
 Instalação concluída!

 Próximos passos no laboratório:
   1. ainoc seed                    # cria hosts/triggers e o API token
   2. sudo nano $INSTALL_DIR/.env   # cole o AINOC_ZABBIX_TOKEN gerado
   3. ainoc restart
   4. Configure o media type no Zabbix (docs/zabbix-setup.md)
   5. ainoc simulate cpu lab-web-01 # dispara um problem de teste
============================================================
EOF
}

configure_env() {
  echo
  echo "=== Configuração inicial (Enter mantém o padrão) ==="
  read -rp "URL da API do Zabbix [http://localhost/api_jsonrpc.php]: " zurl
  read -rp "Provedor de IA (anthropic/openai_compat) [anthropic]: " prov
  read -rsp "Chave da API de IA (fica oculta): " aikey; echo
  local secret; secret=$(openssl rand -hex 16 2>/dev/null || echo "mude-este-segredo")
  sed -i \
    -e "s#^AINOC_ZABBIX_URL=.*#AINOC_ZABBIX_URL=${zurl:-http://localhost/api_jsonrpc.php}#" \
    -e "s#^AINOC_AI_PROVIDER=.*#AINOC_AI_PROVIDER=${prov:-anthropic}#" \
    -e "s#^AINOC_AI_API_KEY=.*#AINOC_AI_API_KEY=${aikey}#" \
    -e "s#^AINOC_WEBHOOK_SHARED_SECRET=.*#AINOC_WEBHOOK_SHARED_SECRET=${secret}#" \
    "$INSTALL_DIR/.env"
  ok "Segredo do webhook gerado: $secret (use no media type do Zabbix)"
  warn "AINOC_ZABBIX_TOKEN ainda vazio — rode 'ainoc seed' e cole o token no .env."
}

install_systemd() {
  cat > /etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=AI NOC Analyst for Zabbix
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
Environment=PYTHONPATH=${INSTALL_DIR}/src
Environment=HOME=${INSTALL_DIR}
Environment=PATH=/usr/local/bin:/usr/bin:/bin:${INSTALL_DIR}/.local/bin
ExecStart=${INSTALL_DIR}/.venv/bin/uvicorn ainoc.main:app --host \${AINOC_BIND_HOST} --port ${PORT}
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=${INSTALL_DIR}
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME" >/dev/null
  ok "Serviço systemd '${SERVICE_NAME}' instalado e habilitado no boot."
}

install_cli() {
  cat > /usr/local/bin/ainoc <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
DIR="/opt/ai-noc-analyst"
PY="$DIR/.venv/bin/python3"
case "${1:-help}" in
  seed)     shift; "$PY" "$DIR/scripts/seed_zabbix.py" "$@" ;;
  simulate) shift; scen="${1:-cpu}"; host="${2:-lab-web-01}"; shift 2 || true
            "$PY" "$DIR/scripts/simulate_problem.py" --scenario "$scen" --host "$host" "$@" ;;
  kb)       sub="${2:-status}"
            case "$sub" in
              status)  curl -fsS http://127.0.0.1:8000/kb/status && echo ;;
              reindex) curl -fsS -X POST http://127.0.0.1:8000/kb/reindex && echo ;;
              *) echo "uso: ainoc kb [status|reindex]  (arquivos em $DIR/kb/)" ;;
            esac ;;
  feedback) shift; evid="${1:?uso: ainoc feedback <eventid> [sim|nao] [causa] [solucao] [--kb]}"
            resp="${2:-}"; causa="${3:-}"; sol="${4:-}"; addkb=false
            [[ "${5:-}" == "--kb" || "$causa" == "--kb" || "$sol" == "--kb" ]] && addkb=true
            case "$resp" in sim) h=true;; nao|não) h=false;; *) h=null;; esac
            curl -fsS -X POST http://127.0.0.1:8000/feedback \
              -H 'Content-Type: application/json' \
              -d "{\"eventid\":\"$evid\",\"hipotese_correta\":$h,\"causa_real\":\"$causa\",\"solucao\":\"$sol\",\"adicionar_kb\":$addkb}" && echo ;;
  status)   systemctl status ainoc --no-pager ;;
  logs)     journalctl -u ainoc -f ;;
  restart)  sudo systemctl restart ainoc && echo "reiniciado." ;;
  health)   curl -fsS http://127.0.0.1:8000/health && echo ;;
  *) cat <<HLP
Uso: ainoc <comando>
  seed [--url URL --user U --password P]   popula o laboratorio Zabbix
  simulate <cpu|disk|down|recover> [host]  dispara/resolve problem de teste
  kb [status|reindex]                      base de conhecimento (RAG)
  feedback <eventid> [sim|nao] [causa] [solucao] [--kb]  aprendizado
  status | logs | restart | health         gerencia o servico
HLP
  ;;
esac
EOF
  chmod +x /usr/local/bin/ainoc
  ok "Comando 'ainoc' disponível no PATH."
}

#--------------------------------------------------------------- uninstall
do_uninstall() {
  need_root uninstall
  systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
  rm -f /etc/systemd/system/${SERVICE_NAME}.service /usr/local/bin/ainoc
  systemctl daemon-reload
  read -rp "Remover também $INSTALL_DIR (inclui .env)? [s/N]: " ans
  [[ "${ans,,}" == "s" ]] && rm -rf "$INSTALL_DIR" && ok "Diretório removido."
  ok "Desinstalação concluída."
}

case "${1:-install}" in
  install)   do_install ;;
  verify)    verify_manifest ;;
  uninstall) do_uninstall ;;
  *) die "Comando desconhecido: $1 (use install | verify | uninstall)" ;;
esac
