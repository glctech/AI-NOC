"""AI NOC Analyst — serviço FastAPI.

Fluxo: Zabbix webhook media type → POST /webhook/zabbix → fila asyncio →
coleta de contexto → primeira análise (IA) → ACK no evento.
"""
import asyncio
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
    settings = get_settings()
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
    logger.info("AI NOC Analyst iniciado (provider=%s model=%s)",
                settings.ai_provider, settings.ai_model)
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
    message = format_ack(analysis)
    await app.state.zabbix.acknowledge(eventid, message)
    await app.state.store.save_incident(
        eventid,
        ctx.host.get("name", ""),
        ctx.trigger.get("description", "") or ctx.event.get("name", ""),
        ctx.event.get("severity", ""),
        analysis.model_dump() if hasattr(analysis, "model_dump")
        else analysis.__dict__,
    )
    logger.info("ACK publicado no evento %s (confiança=%d%% nível=%s)",
                eventid, analysis.confianca_pct, analysis.nivel_recomendado)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "queue_size": app.state.queue.qsize()}


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
    note_path = None
    if payload.adicionar_kb:
        incident = await app.state.store.get_incident(payload.eventid)
        confirmada = ("sim" if payload.hipotese_correta
                      else "não" if payload.hipotese_correta is False else "n/d")
        note = (
            f"# Aprendizado — evento {payload.eventid}\n\n"
            f"- Trigger: {incident['trigger_name']}\n"
            f"- Host: {incident['host']}\n"
            f"- Hipótese da IA confirmada: {confirmada}\n\n"
            f"## Causa real\n{payload.causa_real or 'n/d'}\n\n"
            f"## Solução aplicada\n{payload.solucao or 'n/d'}\n"
        )
        note_path = str(app.state.kb.add_note(
            f"evento_{payload.eventid}.md", note))
    return {"saved": True, "kb_note": note_path}


@app.post("/webhook/zabbix", status_code=202)
async def zabbix_webhook(
    payload: ZabbixWebhookPayload,
    x_ainoc_secret: str = Header(default=""),
) -> dict:
    settings = app.state.settings
    if settings.webhook_shared_secret and x_ainoc_secret != settings.webhook_shared_secret:
        raise HTTPException(status_code=401, detail="invalid secret")
    try:
        app.state.queue.put_nowait(payload.eventid)
    except asyncio.QueueFull:
        raise HTTPException(status_code=503, detail="queue full") from None
    return {"accepted": True, "eventid": payload.eventid}
