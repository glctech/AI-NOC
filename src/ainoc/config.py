"""Configuração central do AI NOC Analyst (variáveis de ambiente)."""
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # extra="ignore": variáveis desconhecidas no .env (ex.: usadas só pela
    # unit do systemd) não derrubam o serviço
    model_config = SettingsConfigDict(env_file=".env", env_prefix="AINOC_",
                                      extra="ignore")

    # Zabbix
    zabbix_url: str = "http://zabbix-web:8080/api_jsonrpc.php"
    zabbix_token: str = ""  # API token (Bearer)

    # IA
    ai_provider: Literal["anthropic", "openai_compat", "claude_code"] = "claude_code"
    ai_model: str = ""  # vazio = modelo padrão do provedor
    ai_api_key: str = ""
    ai_base_url: str = "https://api.openai.com/v1"  # usado por openai_compat
    ai_max_tokens: int = 2000
    ai_timeout_seconds: float = 120.0

    # Provedor claude_code (CLI headless: `claude -p`)
    claude_code_bin: str = "claude"  # caminho do binário do Claude Code
    claude_code_bare: bool = False   # --bare: ignora OAuth, exige ANTHROPIC_API_KEY

    # Serviço
    webhook_shared_secret: str = ""  # validado no header X-AINOC-Secret
    bind_host: str = "127.0.0.1"     # usado pela unit systemd (AINOC_BIND_HOST)
    log_level: str = "INFO"
    analysis_history_hours: int = 1
    analysis_events_hours: int = 24
    debounce_minutes: int = 10          # supressão de flapping por trigger
    correlation_window_minutes: int = 15
    playbooks_dir: str = "playbooks"

    # Fase 3 — RAG / KB / aprendizado
    kb_dir: str = "kb"                 # base de conhecimento (md/txt/pdf/docx)
    db_path: str = "data/ainoc.db"     # histórico de incidentes + feedback
    rag_top_k: int = 3


@lru_cache
def get_settings() -> Settings:
    return Settings()
