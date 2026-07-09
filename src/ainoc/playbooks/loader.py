"""Playbook Engine (Fase 2).

Playbooks são arquivos JSON declarativos em `playbooks/`. Cada um define:
  match: como o playbook é selecionado (regex no nome da trigger e/ou tags)
  verificacoes, comandos, evidencias_esperadas,
  criterios_escalonamento (N1→N2→N3), criterios_encerramento

O playbook selecionado é injetado no prompt da IA, que o usa como roteiro
de diagnóstico, e seu nome é registrado no ACK.
"""
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ainoc.context.collector import IncidentContext

logger = logging.getLogger(__name__)


@dataclass
class Playbook:
    id: str
    nome: str
    match_name_regex: str = ""
    match_tags: dict = field(default_factory=dict)
    verificacoes: list[str] = field(default_factory=list)
    comandos: list[str] = field(default_factory=list)
    evidencias_esperadas: list[str] = field(default_factory=list)
    criterios_escalonamento: dict = field(default_factory=dict)
    criterios_encerramento: list[str] = field(default_factory=list)

    def matches(self, trigger_name: str, tags: list[dict]) -> bool:
        if self.match_name_regex and re.search(
                self.match_name_regex, trigger_name, re.IGNORECASE):
            return True
        tagmap = {t.get("tag"): t.get("value", "") for t in tags}
        return bool(self.match_tags) and all(
            tagmap.get(k) == v for k, v in self.match_tags.items()
        )

    def to_prompt_dict(self) -> dict:
        return {
            "playbook": self.nome,
            "verificacoes": self.verificacoes,
            "comandos_sugeridos": self.comandos,
            "evidencias_esperadas": self.evidencias_esperadas,
            "criterios_escalonamento": self.criterios_escalonamento,
            "criterios_encerramento": self.criterios_encerramento,
        }


class PlaybookRegistry:
    def __init__(self, directory: str | Path):
        self._playbooks: list[Playbook] = []
        self._load(Path(directory))

    def _load(self, directory: Path) -> None:
        if not directory.is_dir():
            logger.warning("Diretório de playbooks inexistente: %s", directory)
            return
        for path in sorted(directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                match = data.pop("match", {})
                self._playbooks.append(Playbook(
                    match_name_regex=match.get("name_regex", ""),
                    match_tags=match.get("tags", {}),
                    **data,
                ))
            except (json.JSONDecodeError, TypeError) as exc:
                logger.error("Playbook inválido %s: %s", path.name, exc)
        logger.info("%d playbooks carregados.", len(self._playbooks))

    def select(self, ctx: IncidentContext) -> Playbook | None:
        trigger_name = (ctx.trigger.get("description")
                        or ctx.event.get("name") or "")
        tags = ctx.event.get("tags", []) + ctx.trigger.get("tags", [])
        for pb in self._playbooks:
            if pb.matches(trigger_name, tags):
                return pb
        return None
