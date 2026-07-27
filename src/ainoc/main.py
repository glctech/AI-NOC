"""AI NOC Analyst — serviço FastAPI.

Fluxo: Zabbix webhook media type → POST /webhook/zabbix → fila asyncio →
coleta de contexto → primeira análise (IA) → ACK no evento.
"""
import asyncio
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from ainoc.ai.providers import build_provider
from ainoc.analysis.first_analysis import FirstAnalysisService, format_ack
from ainoc.config import get_settings
from ainoc.context.collector import ContextCollector
from ainoc.core.debounce import Debouncer
from ainoc.correlation.engine import CorrelationEngine
from ainoc.playbooks.loader import PlaybookRegistry
from ainoc.rag.indexer import KnowledgeBase
from ainoc.rag.store import IncidentStore
from ainoc.zabbix.client import ZabbixClient

logger = logging.getLogger("ainoc")


class ZabbixWebhookPayload(BaseModel):
    eventid: str
    event_name: str = ""
    severity: str = ""
    hostname: str = ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        settings = get_settings()
    except Exception as exc:
        logging.basicConfig(level="ERROR")
        logger.error(
            "CONFIGURAÇÃO INVÁLIDA no .env — corrija e reinicie. Detalhe: %s. "
            "Dica: sem comentários na mesma linha do valor; use "
            "'ainoc env set CHAVE VALOR' para editar com segurança.", exc)
        raise
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    zabbix = ZabbixClient(settings.zabbix_url, settings.zabbix_token)
    app.state.settings = settings
    app.state.zabbix = zabbix
    app.state.collector = ContextCollector(
        zabbix, settings.analysis_history_hours, settings.analysis_events_hours
    )
    app.state.analysis = FirstAnalysisService(build_provider(settings))
    app.state.debouncer = Debouncer(settings.debounce_minutes)
    app.state.correlation = CorrelationEngine(
        zabbix, settings.correlation_window_minutes)
    app.state.playbooks = PlaybookRegistry(settings.playbooks_dir)
    app.state.kb = KnowledgeBase(settings.kb_dir)
    app.state.store = IncidentStore(settings.db_path)
    app.state.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
    app.state.worker = asyncio.create_task(_worker(app))
    if not settings.zabbix_token:
        logger.warning("AINOC_ZABBIX_TOKEN vazio — a coleta de contexto vai "
                       "falhar. Configure com: ainoc token <valor>")
    logger.info("AI NOC Analyst iniciado (provider=%s model=%s)",
                settings.ai_provider, settings.ai_model or "(padrão)")
    yield
    app.state.worker.cancel()
    await zabbix.close()


app = FastAPI(title="AI NOC Analyst for Zabbix", lifespan=lifespan)


async def _worker(app: FastAPI) -> None:
    """Consumidor da fila: processa um evento por vez com tratamento de erro."""
    while True:
        eventid = await app.state.queue.get()
        try:
            await process_event(app, eventid)
        except Exception:  # noqa: BLE001 — worker nunca pode morrer
            logger.exception("Erro ao processar evento %s", eventid)
        finally:
            app.state.queue.task_done()


async def process_event(app: FastAPI, eventid: str) -> None:
    logger.info("Coletando contexto do evento %s", eventid)
    ctx = await app.state.collector.collect(eventid)
    if "event" in ctx.missing:
        logger.warning("Evento %s não encontrado no Zabbix", eventid)
        return

    # Debounce de flapping: chave = trigger (objectid do evento)
    trigger_key = ctx.event.get("objectid", "")
    deb: Debouncer = app.state.debouncer
    if deb.should_suppress(trigger_key):
        logger.info(
            "Evento %s suprimido por flapping (trigger %s ja analisada; "
            "nova analise em %ds)",
            eventid, trigger_key, deb.seconds_remaining(trigger_key))
        return
    deb.mark(trigger_key)
    try:
        await _analyze_and_ack(app, eventid, ctx)
    except Exception:
        deb.unmark(trigger_key)  # falhou: não punir o trigger por 10 min
        raise


async def _analyze_and_ack(app: FastAPI, eventid: str, ctx) -> None:
    related = await app.state.correlation.correlate(ctx)
    if related:
        logger.info("Evento %s correlacionado com %d problem(s): %s",
                    eventid, len(related),
                    ", ".join(r.eventid for r in related))
    playbook = app.state.playbooks.select(ctx)
    if playbook:
        logger.info("Playbook selecionado para o evento %s: %s",
                    eventid, playbook.nome)

    # Fase 3: RAG (base de conhecimento) + incidentes semelhantes
    settings = app.state.settings
    query = " ".join(filter(None, [
        ctx.trigger.get("description", ""),
        ctx.host.get("name", ""),
        " ".join(f'{t.get("tag")} {t.get("value", "")}'
                 for t in ctx.event.get("tags", [])),
    ]))
    kb_excerpts = app.state.kb.search(query, settings.rag_top_k)
    similar = await app.state.store.find_similar(
        query, exclude_eventid=eventid, top_k=settings.rag_top_k)
    if kb_excerpts:
        logger.info("KB: %d trecho(s) relevante(s) para o evento %s (%s)",
                    len(kb_excerpts), eventid,
                    ", ".join(e["fonte"] for e in kb_excerpts))
    if similar:
        logger.info("Incidentes semelhantes para %s: %s", eventid,
                    ", ".join(i["eventid"] for i in similar))

    analysis = await app.state.analysis.analyze(
        ctx, related, playbook, kb_excerpts, similar)
    # campo canônico: o playbook foi selecionado por regra, não pelo modelo
    analysis.playbook_aplicado = playbook.nome if playbook else ""
    message = format_ack(analysis)
    await app.state.zabbix.acknowledge(eventid, message)
    status = ("falha" if analysis.resumo_executivo.startswith(
        "Falha ao gerar análise automática") else "ok")
    await app.state.store.save_incident(
        eventid,
        ctx.host.get("name", ""),
        ctx.trigger.get("description", "") or ctx.event.get("name", ""),
        ctx.event.get("severity", ""),
        analysis.model_dump() if hasattr(analysis, "model_dump")
        else analysis.__dict__,
        objectid=ctx.event.get("objectid", ""),
        status=status,
    )
    logger.info("ACK publicado no evento %s (confiança=%d%% nível=%s)",
                eventid, analysis.confianca_pct, analysis.nivel_recomendado)
    if analysis.resumo_executivo.startswith("Falha ao gerar análise automática"):
        # análise não aconteceu de fato: liberar o trigger para retry imediato
        app.state.debouncer.unmark(ctx.event.get("objectid", ""))
        logger.info("Debounce liberado para o trigger do evento %s "
                    "(análise falhou)", eventid)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "queue_size": app.state.queue.qsize()}


@app.get("/api/incidents")
async def api_incidents(limit: int = 20) -> dict:
    """Incidentes recentes para o dashboard/módulo Zabbix."""
    incidents = await app.state.store.recent(min(max(limit, 1), 100))
    stats = await app.state.store.stats()
    conf = [i["confianca_pct"] for i in incidents
            if i["confianca_pct"] and i.get("status") == "ok"]
    return {"incidents": incidents,
            "confianca_media": round(sum(conf) / len(conf)) if conf else 0,
            **stats}


@app.get("/api/kb/docs")
async def api_kb_docs() -> dict:
    """Documentos da base de conhecimento com contagem de trechos."""
    docs: dict[str, int] = {}
    for c in app.state.kb._chunks:
        docs[c.source] = docs.get(c.source, 0) + 1
    return {"docs": [{"nome": k, "trechos": v} for k, v in sorted(docs.items())],
            "total_chunks": app.state.kb.size}


@app.get("/api/kb/learnings")
async def api_kb_learnings(limit: int = 8) -> dict:
    return {"learnings": await app.state.store.learnings(min(max(limit, 1), 30))}


@app.get("/api/kb/search")
async def api_kb_search(q: str) -> dict:
    return {"resultados": app.state.kb.search(q, 5)}


class ReanalyzePayload(BaseModel):
    eventid: str


@app.post("/reanalyze", status_code=202)
async def reanalyze(payload: ReanalyzePayload) -> dict:
    """Reprocessa um evento (ex.: análise anterior falhou)."""
    incident = await app.state.store.get_incident(payload.eventid)
    objectid = (incident or {}).get("objectid", "")
    if not objectid:
        event = await app.state.zabbix.get_event(payload.eventid)
        if not event:
            raise HTTPException(status_code=404,
                                detail=f"evento {payload.eventid} não encontrado")
        objectid = event.get("objectid", "")
    app.state.debouncer.unmark(objectid)
    try:
        app.state.queue.put_nowait(payload.eventid)
    except asyncio.QueueFull:
        raise HTTPException(status_code=503, detail="queue full") from None
    logger.info("Evento %s reenfileirado para reanálise", payload.eventid)
    return {"accepted": True, "eventid": payload.eventid}


class AssignPayload(BaseModel):
    eventid: str
    usuario: str


@app.post("/assign")
async def assign(payload: AssignPayload) -> dict:
    """Atribui a tratativa de um alerta a um usuário (accountability)."""
    incident = await app.state.store.get_incident(payload.eventid)
    event = None
    if not incident:
        event = await app.state.zabbix.get_event(payload.eventid)
        if not event:
            raise HTTPException(
                status_code=404,
                detail=f"evento {payload.eventid} não encontrado")
    await app.state.store.assign(payload.eventid, payload.usuario)
    ack_ok = True
    try:
        await app.state.zabbix.acknowledge(
            payload.eventid,
            f"[NOC AI] Atribuído a: {payload.usuario} — responsável pela "
            f"tratativa deste alerta.")
    except Exception:  # atribuição vale mesmo se o ACK falhar
        ack_ok = False
        logger.exception("ACK de atribuição falhou para %s", payload.eventid)
    logger.info("Evento %s atribuído a %s", payload.eventid, payload.usuario)
    return {"assigned": True, "eventid": payload.eventid,
            "usuario": payload.usuario, "ack_publicado": ack_ok}


class FeedbackPayload(BaseModel):
    eventid: str
    hipotese_correta: bool | None = None
    causa_real: str = ""
    solucao: str = ""
    adicionar_kb: bool = False


@app.get("/kb/status")
async def kb_status() -> dict:
    stats = await app.state.store.stats()
    return {"kb_chunks": app.state.kb.size, **stats}


@app.post("/kb/reindex")
async def kb_reindex() -> dict:
    return app.state.kb.reindex()


@app.post("/feedback")
async def feedback(payload: FeedbackPayload) -> dict:
    saved = await app.state.store.save_feedback(
        payload.eventid, payload.hipotese_correta,
        payload.causa_real, payload.solucao)
    if not saved:
        raise HTTPException(status_code=404,
                            detail=f"incidente {payload.eventid} não encontrado")

    # sincronizar o encerramento no próprio evento (fonte única de verdade)
    responsavel = await app.state.store.get_assignee(payload.eventid) or "n/d"
    veredicto = ("hipótese da IA CONFIRMADA" if payload.hipotese_correta
                 else "hipótese da IA REFUTADA"
                 if payload.hipotese_correta is False else "hipótese não avaliada")
    linhas = [f"[NOC AI] Incidente encerrado — {veredicto}."]
    if payload.causa_real:
        linhas.append(f"CAUSA REAL: {payload.causa_real}")
    if payload.solucao:
        linhas.append(f"SOLUÇÃO APLICADA: {payload.solucao}")
    linhas.append(f"Responsável pela tratativa: {responsavel}")
    if payload.adicionar_kb:
        linhas.append("Registrado na base de conhecimento.")
    ack_publicado = True
    try:
        await app.state.zabbix.acknowledge(
            payload.eventid, "\n".join(linhas)[:2048])
    except Exception:  # feedback vale mesmo se o ACK falhar
        ack_publicado = False
        logger.exception("ACK de encerramento falhou para %s", payload.eventid)

    note_path = None
    if payload.adicionar_kb:
        incident = await app.state.store.get_incident(payload.eventid)
        confirmada = ("sim" if payload.hipotese_correta
                      else "não" if payload.hipotese_correta is False else "n/d")
        tratado_por = await app.state.store.get_assignee(payload.eventid) or "n/d"
        note = (
            f"# Aprendizado — evento {payload.eventid}\n\n"
            f"- Trigger: {incident['trigger_name']}\n"
            f"- Host: {incident['host']}\n"
            f"- Tratado por: {tratado_por}\n"
            f"- Hipótese da IA confirmada: {confirmada}\n\n"
            f"## Causa real\n{payload.causa_real or 'n/d'}\n\n"
            f"## Solução aplicada\n{payload.solucao or 'n/d'}\n"
        )
        note_path = str(app.state.kb.add_note(
            f"evento_{payload.eventid}.md", note))
    return {"saved": True, "kb_note": note_path,
            "ack_publicado": ack_publicado}


@app.post("/webhook/zabbix", status_code=202)
async def zabbix_webhook(
    payload: ZabbixWebhookPayload,
    x_ainoc_secret: str = Header(default=""),
) -> dict:
    settings = app.state.settings
    if settings.webhook_shared_secret and not hmac.compare_digest(
            x_ainoc_secret, settings.webhook_shared_secret):
        raise HTTPException(status_code=401, detail="invalid secret")
    if not payload.eventid.isdigit():
        logger.info("Webhook com eventid não numérico (%r) — provável teste "
                    "do media type; ignorando.", payload.eventid[:40])
        return {"accepted": False,
                "reason": "eventid não numérico (macro não resolvida no teste)"}
    try:
        app.state.queue.put_nowait(payload.eventid)
    except asyncio.QueueFull:
        raise HTTPException(status_code=503, detail="queue full") from None
    return {"accepted": True, "eventid": payload.eventid}
