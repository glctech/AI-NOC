"""Debounce de flapping: evita reanalisar o mesmo trigger repetidamente.

Protege o uso da IA (assinatura/API) quando um trigger oscila (flapping),
disparando vários Problems em sequência. A chave é o triggerid; a janela
é configurável (AINOC_DEBOUNCE_MINUTES, padrão 10).
"""
import time


class Debouncer:
    def __init__(self, window_minutes: int = 10, max_entries: int = 5000):
        self._window = window_minutes * 60
        self._max = max_entries
        self._seen: dict[str, float] = {}

    def should_suppress(self, key: str) -> bool:
        """True se este trigger já foi analisado dentro da janela."""
        if not key:
            return False
        last = self._seen.get(key)
        return last is not None and (time.monotonic() - last) < self._window

    def mark(self, key: str) -> None:
        if not key:
            return
        if len(self._seen) >= self._max:
            self._evict()
        self._seen[key] = time.monotonic()

    def unmark(self, key: str) -> None:
        """Libera o trigger (ex.: análise falhou; permitir novo processamento)."""
        self._seen.pop(key, None)

    def seconds_remaining(self, key: str) -> int:
        last = self._seen.get(key)
        if last is None:
            return 0
        return max(0, int(self._window - (time.monotonic() - last)))

    def _evict(self) -> None:
        """Remove entradas expiradas; se nada expirou, remove as mais antigas."""
        now = time.monotonic()
        expired = [k for k, t in self._seen.items() if now - t >= self._window]
        for k in expired:
            del self._seen[k]
        if len(self._seen) >= self._max:
            for k in sorted(self._seen, key=self._seen.get)[: self._max // 10]:
                del self._seen[k]
