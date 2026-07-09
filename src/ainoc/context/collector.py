"""Coleta de contexto completo de um Problem para alimentar a análise de IA."""
import asyncio
import logging
import time
from dataclasses import dataclass, field

from ainoc.zabbix.client import ZabbixClient

logger = logging.getLogger(__name__)


@dataclass
class IncidentContext:
    eventid: str
    event: dict = field(default_factory=dict)
    trigger: dict = field(default_factory=dict)
    host: dict = field(default_factory=dict)
    item_history: dict[str, list[dict]] = field(default_factory=dict)
    recent_events: list[dict] = field(default_factory=list)
    active_problems: list[dict] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # dados indisponíveis

    def to_prompt_dict(self) -> dict:
        """Versão compacta e serializável para o prompt da IA."""
        host_inv = self.host.get("inventory") or {}
        return {
            "event": {
                "id": self.eventid,
                "name": self.event.get("name"),
                "severity": self.event.get("severity"),
                "clock": self.event.get("clock"),
                "tags": self.event.get("tags", []),
            },
            "trigger": {
                "description": self.trigger.get("description"),
                "expression": self.trigger.get("expression"),
                "priority": self.trigger.get("priority"),
            },
            "host": {
                "name": self.host.get("name"),
                "groups": [g.get("name") for g in self.host.get("hostgroups", [])],
                "interfaces": [
                    {"ip": i.get("ip"), "type": i.get("type")}
                    for i in self.host.get("interfaces", [])
                ],
                "inventory": {k: v for k, v in host_inv.items() if v} if isinstance(host_inv, dict) else {},
                "tags": self.host.get("tags", []),
            },
            "item_history_last_hour": self.item_history,
            "recent_events_24h": self.recent_events,
            "other_active_problems": self.active_problems,
            "missing_data": self.missing,
        }


class ContextCollector:
    def __init__(self, zabbix: ZabbixClient,
                 history_hours: int = 1, events_hours: int = 24):
        self._zbx = zabbix
        self._history_hours = history_hours
        self._events_hours = events_hours

    async def collect(self, eventid: str) -> IncidentContext:
        ctx = IncidentContext(eventid=eventid)

        event = await self._zbx.get_event(eventid)
        if not event:
            ctx.missing.append("event")
            return ctx
        ctx.event = event

        trigger = None
        if event.get("object") == "0" and event.get("objectid"):
            trigger = await self._zbx.get_trigger(event["objectid"])
        if trigger:
            ctx.trigger = trigger
        else:
            ctx.missing.append("trigger")
            return ctx

        hosts = trigger.get("hosts") or []
        if hosts:
            host = await self._zbx.get_host(hosts[0]["hostid"])
            if host:
                ctx.host = host
        if not ctx.host:
            ctx.missing.append("host")

        now = int(time.time())
        hist_from = now - self._history_hours * 3600
        events_from = now - self._events_hours * 3600

        # História dos itens da trigger, eventos recentes e problems ativos em paralelo
        items = trigger.get("items") or []
        history_tasks = {
            item["name"]: self._zbx.get_history(
                item["itemid"], int(item.get("value_type", 0)), hist_from
            )
            for item in items[:5]
        }
        gathered = await asyncio.gather(
            *history_tasks.values(),
            self._zbx.get_host_events(ctx.host["hostid"], events_from)
            if ctx.host else _empty(),
            self._zbx.get_active_problems(ctx.host["hostid"])
            if ctx.host else _empty(),
            return_exceptions=True,
        )

        names = list(history_tasks.keys())
        for name, result in zip(names, gathered[: len(names)]):
            if isinstance(result, Exception):
                ctx.missing.append(f"history:{name}")
            else:
                ctx.item_history[name] = _compact_history(result)

        recent, problems = gathered[len(names):]
        ctx.recent_events = recent if isinstance(recent, list) else []
        ctx.active_problems = problems if isinstance(problems, list) else []
        if isinstance(recent, Exception):
            ctx.missing.append("recent_events")
        if isinstance(problems, Exception):
            ctx.missing.append("active_problems")

        return ctx


async def _empty() -> list:
    return []


def _compact_history(rows: list[dict], max_points: int = 30) -> list[dict]:
    """Reduz a série a no máximo max_points para não estourar o prompt."""
    if len(rows) <= max_points:
        return [{"clock": r["clock"], "value": r["value"]} for r in rows]
    step = len(rows) / max_points
    picked = [rows[int(i * step)] for i in range(max_points)]
    return [{"clock": r["clock"], "value": r["value"]} for r in picked]
