"""Correlation Engine (Fase 2).

Dado o contexto de um Problem, encontra outros problems ativos relacionados
e explica POR QUE estão relacionados (Explainable AI). Critérios:
  - mesmo host
  - mesmo host group, dentro da janela temporal
  - sobreposição de tags relevantes
A saída alimenta o prompt do RCA para que a IA trate o conjunto como um
incidente único quando fizer sentido.
"""
import logging
import time
from dataclasses import dataclass, field

from ainoc.context.collector import IncidentContext
from ainoc.zabbix.client import ZabbixClient

logger = logging.getLogger(__name__)

# tags genéricas que não indicam relação real entre problems
IGNORED_TAGS = {"env", "scope", "managed_by", "class", "target"}


@dataclass
class RelatedProblem:
    eventid: str
    name: str
    host: str
    severity: str
    clock: int
    reasons: list[str] = field(default_factory=list)

    def to_prompt_dict(self) -> dict:
        return {
            "eventid": self.eventid, "name": self.name, "host": self.host,
            "severity": self.severity, "reasons": self.reasons,
        }


class CorrelationEngine:
    def __init__(self, zabbix: ZabbixClient, window_minutes: int = 15):
        self._zbx = zabbix
        self._window = window_minutes * 60

    async def correlate(self, ctx: IncidentContext) -> list[RelatedProblem]:
        if not ctx.host or not ctx.event:
            return []
        try:
            return await self._correlate(ctx)
        except Exception:  # correlação nunca pode derrubar o pipeline
            logger.exception("Correlação falhou para o evento %s", ctx.eventid)
            return []

    async def _correlate(self, ctx: IncidentContext) -> list[RelatedProblem]:
        groupids = [g["groupid"] for g in ctx.host.get("hostgroups", [])]
        hostid = ctx.host["hostid"]
        event_clock = int(ctx.event.get("clock", time.time()))
        window_from = event_clock - self._window

        problems = await self._zbx.call("problem.get", {
            "groupids": groupids or None,
            "recent": False,
            "output": ["eventid", "name", "clock", "severity", "objectid"],
            "selectTags": "extend",
            "sortfield": "eventid", "sortorder": "DESC", "limit": 100,
        })

        my_tags = _tag_set(ctx.event.get("tags", []))
        trig_hosts = await self._resolve_hosts_batch(problems)
        related: list[RelatedProblem] = []

        for p in problems:
            if p["eventid"] == ctx.eventid:
                continue
            reasons: list[str] = []
            p_clock = int(p.get("clock", 0))
            same_window = p_clock >= window_from

            p_hosts = trig_hosts.get(p.get("objectid"), set())
            if hostid in p_hosts:
                reasons.append("mesmo host")
            elif same_window and groupids:
                reasons.append("mesmo host group na janela de "
                               f"{self._window // 60} min")
            overlap = my_tags & _tag_set(p.get("tags", []))
            if overlap:
                reasons.append("tags em comum: " + ", ".join(sorted(overlap)))
            if same_window and reasons:
                delta = abs(event_clock - p_clock)
                reasons.append(f"proximidade temporal ({delta}s de diferença)")

            # forte: mesmo host exato ou tags em comum
            # médio: mesmo host group dentro da janela temporal
            strong = any(r == "mesmo host" or r.startswith("tags")
                         for r in reasons)
            medium = any(r.startswith("mesmo host group") for r in reasons)
            if strong or medium:
                related.append(RelatedProblem(
                    eventid=p["eventid"], name=p.get("name", ""),
                    host=",".join(sorted(p_hosts)) or "?",
                    severity=p.get("severity", "0"),
                    clock=p_clock, reasons=reasons,
                ))

        related.sort(key=lambda r: (-len(r.reasons), -r.clock))
        return related[:10]

    async def _resolve_hosts_batch(self, problems: list[dict]) -> dict[str, set[str]]:
        """Uma única chamada resolve os hosts de todas as triggers (evita N+1)."""
        objectids = sorted({p["objectid"] for p in problems if p.get("objectid")})
        if not objectids:
            return {}
        triggers = await self._zbx.call("trigger.get", {
            "triggerids": objectids, "output": ["triggerid"],
            "selectHosts": ["hostid"],
        })
        return {t["triggerid"]: {h["hostid"] for h in t.get("hosts", [])}
                for t in triggers}


def _tag_set(tags: list[dict]) -> set[str]:
    return {
        f'{t.get("tag")}:{t.get("value", "")}'
        for t in tags
        if t.get("tag") and t.get("tag") not in IGNORED_TAGS
    }
