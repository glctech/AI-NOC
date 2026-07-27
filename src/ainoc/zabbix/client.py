"""Cliente assíncrono da API JSON-RPC do Zabbix 7.4.x (autenticação Bearer)."""
import asyncio
import itertools
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class ZabbixAPIError(Exception):
    """Erro retornado pela API do Zabbix."""


class ZabbixClient:
    def __init__(self, url: str, token: str, timeout: float = 30.0, retries: int = 3):
        self._url = url
        self._retries = retries
        self._id = itertools.count(1)
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,  # frontends atrás de proxy/porta alternativa
            headers={
                "Content-Type": "application/json-rpc",
                "Authorization": f"Bearer {token}",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def call(self, method: str, params: dict | list | None = None) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": next(self._id),
        }
        last_exc: Exception | None = None
        for attempt in range(1, self._retries + 1):
            try:
                resp = await self._client.post(self._url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    raise ZabbixAPIError(f"{method}: {data['error']}")
                return data["result"]
            except (httpx.HTTPError, ZabbixAPIError) as exc:
                last_exc = exc
                if isinstance(exc, ZabbixAPIError):
                    raise  # erro lógico da API não é retryável
                logger.warning("Falha em %s (tentativa %d/%d): %s",
                               method, attempt, self._retries, exc)
                await asyncio.sleep(min(2 ** attempt, 8))
        raise ZabbixAPIError(
            f"{method} falhou após {self._retries} tentativas "
            f"(causa: {type(last_exc).__name__}: {last_exc}) — "
            f"verifique AINOC_ZABBIX_URL ({self._url})") from last_exc

    # ---- atalhos usados pelo pipeline ----

    async def api_version(self) -> str:
        return await self.call("apiinfo.version")

    async def get_event(self, eventid: str) -> dict | None:
        result = await self.call("event.get", {
            "eventids": [eventid],
            "selectTags": "extend",
            "output": "extend",
            "selectAcknowledges": "extend",
        })
        return result[0] if result else None

    async def get_trigger(self, triggerid: str) -> dict | None:
        result = await self.call("trigger.get", {
            "triggerids": [triggerid],
            "output": "extend",
            "selectHosts": "extend",
            "selectItems": "extend",
            "selectTags": "extend",
            "expandDescription": True,
        })
        return result[0] if result else None

    async def get_host(self, hostid: str) -> dict | None:
        result = await self.call("host.get", {
            "hostids": [hostid],
            "output": "extend",
            "selectInventory": "extend",
            "selectInterfaces": "extend",
            "selectTags": "extend",
            "selectHostGroups": "extend",
        })
        return result[0] if result else None

    async def get_history(self, itemid: str, value_type: int,
                          time_from: int, limit: int = 120) -> list[dict]:
        return await self.call("history.get", {
            "itemids": [itemid],
            "history": value_type,
            "time_from": time_from,
            "sortfield": "clock",
            "sortorder": "DESC",
            "limit": limit,
        })

    async def get_host_events(self, hostid: str, time_from: int,
                              limit: int = 50) -> list[dict]:
        return await self.call("event.get", {
            "hostids": [hostid],
            "time_from": time_from,
            "sortfield": ["clock"],
            "sortorder": "DESC",
            "limit": limit,
            "output": ["eventid", "name", "clock", "severity", "value"],
        })

    async def get_active_problems(self, hostid: str) -> list[dict]:
        return await self.call("problem.get", {
            "hostids": [hostid],
            "recent": False,
            "output": ["eventid", "name", "clock", "severity"],
        })

    async def acknowledge(self, eventid: str, message: str) -> Any:
        # action 4 = add message; 2 = acknowledge → 6 = ambos
        return await self.call("event.acknowledge", {
            "eventids": [eventid],
            "action": 6,
            "message": message[:2048],  # limite de campo do Zabbix
        })
