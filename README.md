# AI NOC Analyst for Zabbix — Fase 1 (MVP)

Serviço externo (Python 3.10+ / FastAPI) que atua como Analista NOC N1:
recebe Problems do Zabbix via webhook, coleta contexto pela API oficial,
gera primeira análise com IA e publica ACK `[NOC AI]` no evento.

## Instalação no laboratório (Ubuntu 22.04/24.04)

```bash
tar xzf ai-noc-analyst-0.1.0.tar.gz -C ~/ainoc && cd ~/ainoc
./install.sh verify        # confere integridade (SHA-256) — opcional
sudo ./install.sh install  # instala em /opt, cria venv + systemd + comando 'ainoc'
```

O instalador copia **somente** arquivos listados e verificados no
`MANIFEST.sha256` — se algo estiver corrompido, faltando ou trocado, ele aborta.

## Fluxo do laboratório

```bash
ainoc seed --url http://SEU_ZABBIX/api_jsonrpc.php --user Admin --password zabbix
sudo nano /opt/ai-noc-analyst/.env    # colar o AINOC_ZABBIX_TOKEN exibido
ainoc restart
# configurar o media type webhook: docs/zabbix-setup.md
ainoc simulate cpu lab-web-01         # dispara CPU 97% → abre Problem
ainoc logs                            # acompanha a análise em tempo real
ainoc simulate recover lab-web-01     # resolve o Problem
```

Cenários de simulação: `cpu`, `disk`, `down`, `recover`.

## Comandos

| Comando | Ação |
|---|---|
| `ainoc seed` | cria hosts, itens trapper, triggers e API token no Zabbix |
| `ainoc simulate <cenário> [host]` | envia valores via protocolo zabbix_sender (porta 10051) |
| `ainoc status / logs / restart / health` | gerencia o serviço |
| `sudo ./install.sh uninstall` | remove tudo |

## Provedores de IA

| Provedor | Como funciona |
|---|---|
| `claude_code` (padrão) | chama o **Claude Code CLI em modo headless** (`claude -p --output-format json`), reaproveitando a autenticação da sua assinatura — sem chave de API no .env |
| `anthropic` | API Messages da Anthropic (requer `AINOC_AI_API_KEY`) |
| `openai_compat` | OpenAI, Azure, Ollama, LM Studio, OpenRouter, vLLM, llama.cpp (`AINOC_AI_BASE_URL`) |

### Autenticando o claude_code no servidor

O serviço roda como o usuário `ainoc` com `HOME=/opt/ai-noc-analyst`, então o
login precisa ser feito nesse contexto (uma única vez):

```bash
sudo npm install -g @anthropic-ai/claude-code   # se ainda não tiver o CLI
sudo -u ainoc HOME=/opt/ai-noc-analyst claude setup-token
```

Alternativa sem OAuth (CI/servidores sem browser): defina no `.env`
`AINOC_CLAUDE_CODE_BARE=true` e `AINOC_AI_API_KEY=sk-ant-...` — o serviço
invoca o CLI com `--bare`, que exige credencial explícita.

## Fase 3 — RAG, Base de Conhecimento e Aprendizado

- **KB:** coloque runbooks em `/opt/ai-noc-analyst/kb/` (`.md`/`.txt`; `.pdf`/`.docx`
  com `pypdf`/`python-docx` instalados) e rode `ainoc kb reindex`. Trechos
  relevantes entram no prompt e são citados em REFERÊNCIAS no ACK.
- **Memória:** cada análise fica em `data/ainoc.db` (SQLite); incidentes
  semelhantes do passado — e o feedback humano sobre eles — alimentam as
  próximas análises.
- **Aprendizado:** ao fechar um incidente:
  `ainoc feedback <eventid> sim|nao "causa real" "solução" --kb`
  (`--kb` gera uma nota em `kb/aprendizado/` que vira conhecimento futuro).
- `ainoc kb status` mostra chunks indexados, incidentes e precisão da IA.

## Novidades da 0.6.0

**Atribuição de responsável:** `ainoc atribuir <eventid> <usuario>` (ou
`POST /assign`) registra quem assumiu a tratativa, publica ACK
"[NOC AI] Atribuído a: ..." no problem, e o responsável passa a aparecer nos
incidentes semelhantes e nas notas de aprendizado da KB — o histórico responde
"quem tratou disso da última vez". **KB ampliada:** runbooks Linux de disco,
memória e host down/rede inclusos. **Instalador multi-distro:** Debian/Ubuntu
(apt) e RHEL/Rocky/Alma/Oracle (dnf/yum), com orientação para firewalld.

## Novidades da 0.5.0 (revisão geral)

Correlação com chamada em lote à API (antes: até 100 chamadas por análise);
debounce liberado automaticamente quando a análise falha; comparação do secret
do webhook em tempo constante; erro de configuração do `.env` agora gera
mensagem clara no log em vez de traceback; eventid não numérico do teste do
media type é respondido sem entrar na fila; `ainoc restart`/`health` aguardam
o serviço subir; e novos comandos `ainoc token <valor>` e
`ainoc env set CHAVE VALOR` para editar o `.env` sem abrir editor.

## Desenvolvimento

```bash
pip install -r requirements.txt
PYTHONPATH=src pytest -v
./scripts/make_package.sh 0.1.0   # regenera MANIFEST.sha256 + tarball
```
