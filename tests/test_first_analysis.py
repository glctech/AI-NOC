"""Testes unitários do pipeline de primeira análise (IA e Zabbix mockados)."""
import json

import pytest

from ainoc.ai.providers import AIProvider
from ainoc.analysis.first_analysis import (
    INSUFFICIENT,
    FirstAnalysisService,
    format_ack,
)
from ainoc.context.collector import IncidentContext

VALID_RESPONSE = {
    "resumo_executivo": "CPU do host web-01 acima de 95% há 40 minutos.",
    "impacto": "Lentidão nas aplicações hospedadas no host.",
    "servicos_afetados": ["Portal Web"],
    "causa_provavel": "Processo de backup consumindo CPU fora da janela.",
    "hipoteses_alternativas": ["Loop em aplicação PHP"],
    "evidencias": ["history: cpu.util médio 96% na última hora"],
    "confianca_pct": 72,
    "criticidade": "alta",
    "urgencia": "alta",
    "proximos_passos_n1": ["Verificar top de processos via item de sistema"],
}


class FakeProvider(AIProvider):
    def __init__(self, response: str):
        self._response = response

    async def complete(self, system: str, user: str) -> str:
        return self._response


def _ctx() -> IncidentContext:
    ctx = IncidentContext(eventid="123")
    ctx.event = {"name": "High CPU", "severity": "4", "clock": "1", "tags": []}
    ctx.trigger = {"description": "CPU > 90%", "expression": "x", "priority": "4"}
    ctx.host = {"name": "web-01", "hostgroups": [], "interfaces": [], "tags": []}
    return ctx


@pytest.mark.asyncio
async def test_analyze_valid_json():
    service = FirstAnalysisService(FakeProvider(json.dumps(VALID_RESPONSE)))
    result = await service.analyze(_ctx())
    assert result.confianca_pct == 72
    assert result.criticidade == "alta"
    assert result.evidencias


@pytest.mark.asyncio
async def test_analyze_json_com_markdown_fence():
    raw = "```json\n" + json.dumps(VALID_RESPONSE) + "\n```"
    service = FirstAnalysisService(FakeProvider(raw))
    result = await service.analyze(_ctx())
    assert result.resumo_executivo.startswith("CPU do host")


@pytest.mark.asyncio
async def test_analyze_resposta_invalida_faz_fallback_seguro():
    service = FirstAnalysisService(FakeProvider("não sou json"))
    result = await service.analyze(_ctx())
    assert result.confianca_pct == 0
    assert INSUFFICIENT in result.causa_provavel


def test_format_ack_prefixo_e_limite():
    from ainoc.analysis.first_analysis import FirstAnalysis
    ack = format_ack(FirstAnalysis(**VALID_RESPONSE))
    assert ack.startswith("[NOC AI]")
    assert len(ack) <= 2048
    assert "CONFIANÇA: 72%" in ack


def test_context_to_prompt_dict_marca_dados_faltantes():
    ctx = IncidentContext(eventid="9")
    ctx.missing.append("host")
    d = ctx.to_prompt_dict()
    assert d["missing_data"] == ["host"]


# ---------------- ClaudeCodeProvider ----------------

def test_parse_claude_code_output_ok():
    from ainoc.ai.providers import parse_claude_code_output
    raw = json.dumps({"result": '{"ok": true}', "session_id": "abc",
                      "total_cost_usd": 0.01, "is_error": False})
    assert parse_claude_code_output(raw) == '{"ok": true}'


def test_parse_claude_code_output_erro():
    from ainoc.ai.providers import AIProviderError, parse_claude_code_output
    with pytest.raises(AIProviderError):
        parse_claude_code_output(json.dumps({"is_error": True, "result": "limite"}))
    with pytest.raises(AIProviderError):
        parse_claude_code_output("saida nao-json do cli")
    with pytest.raises(AIProviderError):
        parse_claude_code_output(json.dumps({"session_id": "x"}))  # sem result


@pytest.mark.asyncio
async def test_claude_code_provider_com_cli_falso(tmp_path):
    """Simula o binário `claude` com um script que devolve o JSON esperado."""
    import stat
    from ainoc.ai.providers import ClaudeCodeProvider

    fake = tmp_path / "claude"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'result': json.dumps(" + repr(VALID_RESPONSE) + "),"
        " 'is_error': False, 'session_id': 't'}))\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    provider = ClaudeCodeProvider(binary=str(fake), timeout=15)
    data = await provider.complete_json("sys", "user")
    assert data["confianca_pct"] == 72


@pytest.mark.asyncio
async def test_claude_code_provider_binario_inexistente():
    from ainoc.ai.providers import AIProviderError, ClaudeCodeProvider
    provider = ClaudeCodeProvider(binary="/nao/existe/claude")
    with pytest.raises(AIProviderError, match="não encontrado"):
        await provider.complete("s", "u")
