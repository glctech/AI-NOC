"""Primeira Análise automática de um Problem (papel de Analista NOC N1)."""
import json
import logging

from pydantic import BaseModel, Field, ValidationError, field_validator

from ainoc.ai.providers import AIProvider, AIProviderError
from ainoc.context.collector import IncidentContext

logger = logging.getLogger(__name__)

INSUFFICIENT = "Não há evidências suficientes para concluir."


class FirstAnalysis(BaseModel):
    resumo_executivo: str
    impacto: str
    servicos_afetados: list[str] = Field(default_factory=list)
    causa_provavel: str
    hipoteses_alternativas: list[str] = Field(default_factory=list)
    evidencias: list[str] = Field(default_factory=list)
    confianca_pct: int = Field(ge=0, le=100)
    criticidade: str
    urgencia: str
    proximos_passos_n1: list[str] = Field(default_factory=list)
    # Fase 2
    eventos_correlacionados: list[str] = Field(default_factory=list)
    playbook_aplicado: str = ""
    nivel_recomendado: str = "N1"  # N1 | N2 | N3
    # Fase 3
    referencias: list[str] = Field(default_factory=list)

    @field_validator("servicos_afetados", "hipoteses_alternativas",
                     "evidencias", "proximos_passos_n1",
                     "eventos_correlacionados", "referencias", mode="before")
    @classmethod
    def _coerce_list(cls, v):
        """LLMs às vezes devolvem string onde se espera lista; tolerar."""
        if v is None:
            return []
        if isinstance(v, str):
            return [v.strip()] if v.strip() else []
        return v

    @field_validator("confianca_pct", mode="before")
    @classmethod
    def _coerce_pct(cls, v):
        """Aceita '85', '85%', 85.0 etc.; fora disso, assume 0."""
        if isinstance(v, str):
            v = v.strip().rstrip("%")
        try:
            return max(0, min(100, int(float(v))))
        except (TypeError, ValueError):
            return 0

    @field_validator("nivel_recomendado", mode="before")
    @classmethod
    def _coerce_nivel(cls, v):
        v = str(v or "N1").strip().upper()
        return v if v in ("N1", "N2", "N3") else "N1"


SYSTEM_PROMPT = f"""Você é um Analista NOC N1 sênior analisando um alerta do Zabbix.

REGRAS INEGOCIÁVEIS:
1. Baseie TODA afirmação exclusivamente nos dados fornecidos no contexto.
2. Se os dados não suportarem uma conclusão, escreva exatamente:
   "{INSUFFICIENT}"
3. Cada item de `evidencias` deve citar um dado concreto do contexto
   (métrica, evento, tag, histórico).
4. `confianca_pct` deve refletir honestamente a qualidade das evidências;
   com dados faltantes (campo missing_data), reduza a confiança.
5. Responda APENAS com um objeto JSON válido, sem markdown, sem preâmbulo,
   com exatamente estas chaves:
   resumo_executivo, impacto, servicos_afetados, causa_provavel,
   hipoteses_alternativas, evidencias, confianca_pct, criticidade,
   urgencia, proximos_passos_n1, eventos_correlacionados,
   playbook_aplicado, nivel_recomendado, referencias
6. criticidade e urgencia: um de ["baixa", "media", "alta", "critica"].
7. Escreva em português do Brasil, tom técnico e objetivo.
   Campos de lista DEVEM ser arrays JSON mesmo com um único item
   (ex.: "hipoteses_alternativas": ["apenas uma hipótese"]).
8. CORRELAÇÃO: se o contexto trouxer `problemas_relacionados`, avalie se
   fazem parte do MESMO incidente (causa comum). Liste em
   `eventos_correlacionados` apenas os eventids que você considera parte
   do mesmo incidente, e considere-os na causa raiz.
9. PLAYBOOK: se o contexto trouxer `playbook`, use suas verificações como
   roteiro dos próximos passos, preencha `playbook_aplicado` com o nome
   dele e avalie os critérios de escalonamento para definir
   `nivel_recomendado` ("N1", "N2" ou "N3"), justificando nas evidências.
10. BASE DE CONHECIMENTO: se houver `base_conhecimento`, use apenas trechos
    realmente pertinentes; cada documento usado deve entrar em
    `referencias` no formato "arquivo — seção". NUNCA invente referências
    nem cite documento que não esteja no contexto.
11. INCIDENTES SEMELHANTES: se houver `incidentes_semelhantes`, dê peso
    especial aos que têm `causa_real`/`solucao_aplicada` confirmadas por
    feedback humano, citando o eventid antigo nas evidências ao usá-los."""


class FirstAnalysisService:
    def __init__(self, provider: AIProvider):
        self._provider = provider

    async def analyze(self, ctx: IncidentContext,
                      related: list | None = None,
                      playbook=None,
                      kb_excerpts: list[dict] | None = None,
                      similar_incidents: list[dict] | None = None) -> FirstAnalysis:
        prompt_ctx = ctx.to_prompt_dict()
        if related:
            prompt_ctx["problemas_relacionados"] = [
                r.to_prompt_dict() for r in related
            ]
        if playbook is not None:
            prompt_ctx["playbook"] = playbook.to_prompt_dict()
        if kb_excerpts:
            prompt_ctx["base_conhecimento"] = kb_excerpts
        if similar_incidents:
            prompt_ctx["incidentes_semelhantes"] = similar_incidents
        user_prompt = (
            "Contexto do incidente coletado do Zabbix:\n"
            + json.dumps(prompt_ctx, ensure_ascii=False, default=str)
        )
        try:
            data = await self._provider.complete_json(SYSTEM_PROMPT, user_prompt)
            return FirstAnalysis.model_validate(data)
        except (AIProviderError, ValidationError) as exc:
            logger.error("Análise falhou para evento %s: %s", ctx.eventid, exc)
            return FirstAnalysis(
                resumo_executivo=f"Falha ao gerar análise automática: {exc}",
                impacto=INSUFFICIENT,
                causa_provavel=INSUFFICIENT,
                confianca_pct=0,
                criticidade="media",
                urgencia="media",
                proximos_passos_n1=["Analisar manualmente o evento."],
            )


def format_ack(analysis: FirstAnalysis) -> str:
    """Formata a análise para o acknowledge do Zabbix (limite ~2048 chars)."""
    lines = [
        "[NOC AI] Primeira análise automática concluída.",
        "",
        f"RESUMO: {analysis.resumo_executivo}",
        f"IMPACTO: {analysis.impacto}",
        f"CAUSA PROVÁVEL: {analysis.causa_provavel}",
        f"CONFIANÇA: {analysis.confianca_pct}% | "
        f"CRITICIDADE: {analysis.criticidade} | URGÊNCIA: {analysis.urgencia}"
        f" | NÍVEL: {analysis.nivel_recomendado}",
    ]
    if analysis.playbook_aplicado:
        lines.append(f"PLAYBOOK: {analysis.playbook_aplicado}")
    if analysis.eventos_correlacionados:
        lines.append("CORRELACIONADO COM EVENTOS: "
                     + ", ".join(analysis.eventos_correlacionados[:8]))
    if analysis.referencias:
        lines.append("REFERÊNCIAS: " + "; ".join(analysis.referencias[:4]))
    if analysis.evidencias:
        lines.append("EVIDÊNCIAS: " + "; ".join(analysis.evidencias[:4]))
    if analysis.proximos_passos_n1:
        lines.append("PRÓXIMAS AÇÕES:")
        lines += [f"  {i}. {p}" for i, p in enumerate(analysis.proximos_passos_n1[:5], 1)]
    lines += ["", "Análise gerada automaticamente pelo AI NOC Analyst."]
    return "\n".join(lines)[:2048]
