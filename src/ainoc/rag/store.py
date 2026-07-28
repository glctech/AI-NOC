"""Persistência da Fase 3 (SQLite, stdlib) — memória do AI NOC Analyst.

Guarda cada análise gerada e o feedback do operador. Alimenta:
  - busca de incidentes semelhantes (injetados no prompt);
  - aprendizado contínuo (feedback vira nota na base de conhecimento);
  - métricas futuras de precisão da IA (Fase 5).
"""
import asyncio
import json
import sqlite3
import time
from pathlib import Path

from ainoc.rag.indexer import score_texts  # ranking lexical compartilhado

# causas que não representam conhecimento real (testes, vazias, sem conclusão)
_TRIVIAL_CAUSES = {"teste", "test", "apenas teste", "so teste", "só teste",
                   "n/a", "na", "-", "sem causa", "x", "xx", "xxx", "asd"}


def _is_trivial(causa: str, solucao: str) -> bool:
    c = (causa or "").strip().lower()
    if not c:
        return True
    if c in _TRIVIAL_CAUSES:
        return True
    if c.startswith("não há evidências suficientes") \
            or c.startswith("nao ha evidencias suficientes"):
        return True
    # causa muito curta e sem solução: provavelmente placeholder
    if len(c) < 6 and not (solucao or "").strip():
        return True
    return False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    eventid       TEXT PRIMARY KEY,
    host          TEXT NOT NULL DEFAULT '',
    trigger_name  TEXT NOT NULL DEFAULT '',
    severity      TEXT NOT NULL DEFAULT '',
    analysis_json TEXT NOT NULL,
    created_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback (
    eventid          TEXT PRIMARY KEY REFERENCES incidents(eventid),
    hipotese_correta INTEGER,          -- 1 sim / 0 nao / NULL sem resposta
    causa_real       TEXT DEFAULT '',
    solucao          TEXT DEFAULT '',
    created_at       INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS assignments (
    eventid      TEXT PRIMARY KEY,
    usuario      TEXT NOT NULL,
    atribuido_em INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incidents_created ON incidents(created_at DESC);
"""


class IncidentStore:
    def __init__(self, db_path: str | Path):
        self._path = str(db_path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as con:
            con.executescript(_SCHEMA)
            # migração leve: bancos criados antes da 0.7.1 não têm objectid
            cols = [r[1] for r in con.execute("PRAGMA table_info(incidents)")]
            if "objectid" not in cols:
                con.execute("ALTER TABLE incidents ADD COLUMN objectid TEXT "
                            "NOT NULL DEFAULT ''")
            if "status" not in cols:
                con.execute("ALTER TABLE incidents ADD COLUMN status TEXT "
                            "NOT NULL DEFAULT 'ok'")

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._path, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    # ------------------------------------------------ escrita
    async def save_incident(self, eventid: str, host: str, trigger_name: str,
                            severity: str, analysis: dict,
                            objectid: str = "", status: str = "ok") -> None:
        def _run():
            with self._conn() as con:
                con.execute(
                    """INSERT OR REPLACE INTO incidents
                       (eventid, host, trigger_name, severity, analysis_json,
                        created_at, objectid, status)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (eventid, host, trigger_name, severity,
                     json.dumps(analysis, ensure_ascii=False),
                     int(time.time()), objectid, status),
                )
        await asyncio.to_thread(_run)

    async def save_feedback(self, eventid: str, hipotese_correta: bool | None,
                            causa_real: str, solucao: str) -> bool:
        def _run() -> bool:
            with self._conn() as con:
                row = con.execute("SELECT 1 FROM incidents WHERE eventid=?",
                                  (eventid,)).fetchone()
                if not row:
                    return False
                con.execute(
                    "INSERT OR REPLACE INTO feedback VALUES (?,?,?,?,?)",
                    (eventid,
                     None if hipotese_correta is None else int(hipotese_correta),
                     causa_real, solucao, int(time.time())),
                )
                return True
        return await asyncio.to_thread(_run)

    async def assign(self, eventid: str, usuario: str) -> None:
        """Registra o responsável pela tratativa do alerta."""
        def _run():
            with self._conn() as con:
                con.execute(
                    "INSERT OR REPLACE INTO assignments VALUES (?,?,?)",
                    (eventid, usuario, int(time.time())),
                )
        await asyncio.to_thread(_run)

    async def get_assignee(self, eventid: str) -> str | None:
        def _run():
            with self._conn() as con:
                row = con.execute(
                    "SELECT usuario FROM assignments WHERE eventid=?",
                    (eventid,)).fetchone()
                return row["usuario"] if row else None
        return await asyncio.to_thread(_run)

    async def get_incident(self, eventid: str) -> dict | None:
        def _run():
            with self._conn() as con:
                row = con.execute(
                    "SELECT * FROM incidents WHERE eventid=?", (eventid,)
                ).fetchone()
                return dict(row) if row else None
        return await asyncio.to_thread(_run)

    # ------------------------------------------------ leitura / similares
    async def find_similar(self, query: str, exclude_eventid: str = "",
                           top_k: int = 3, pool: int = 500) -> list[dict]:
        """Incidentes passados mais parecidos (ranking lexical), com feedback."""
        def _run() -> list[dict]:
            with self._conn() as con:
                rows = con.execute(
                    """SELECT i.eventid, i.host, i.trigger_name, i.severity,
                              i.analysis_json, i.created_at,
                              f.hipotese_correta, f.causa_real, f.solucao,
                              a.usuario AS tratado_por
                       FROM incidents i
                       LEFT JOIN feedback f USING (eventid)
                       LEFT JOIN assignments a USING (eventid)
                       WHERE i.status = 'ok'
                       ORDER BY i.created_at DESC LIMIT ?""", (pool,),
                ).fetchall()
            docs, meta = [], []
            for r in rows:
                if r["eventid"] == exclude_eventid:
                    continue
                docs.append(f'{r["trigger_name"]} {r["host"]}')
                meta.append(r)
            results = []
            for idx, score in score_texts(query, docs, top_k):
                r = meta[idx]
                analysis = json.loads(r["analysis_json"])
                results.append({
                    "eventid": r["eventid"], "host": r["host"],
                    "trigger": r["trigger_name"], "score": round(score, 3),
                    "causa_provavel_na_epoca": analysis.get("causa_provavel", ""),
                    "hipotese_confirmada": (
                        None if r["hipotese_correta"] is None
                        else bool(r["hipotese_correta"])),
                    "causa_real": r["causa_real"] or "",
                    "solucao_aplicada": r["solucao"] or "",
                    "tratado_por": r["tratado_por"] or "",
                })
            return results
        return await asyncio.to_thread(_run)

    async def recent(self, limit: int = 20) -> list[dict]:
        """Últimos incidentes com análise resumida, responsável e feedback."""
        def _run() -> list[dict]:
            with self._conn() as con:
                rows = con.execute(
                    """SELECT i.eventid, i.host, i.trigger_name, i.severity,
                              i.analysis_json, i.created_at, i.objectid,
                              i.status,
                              f.hipotese_correta, a.usuario AS tratado_por
                       FROM incidents i
                       LEFT JOIN feedback f USING (eventid)
                       LEFT JOIN assignments a USING (eventid)
                       ORDER BY i.created_at DESC LIMIT ?""", (limit,),
                ).fetchall()
            out = []
            for r in rows:
                an = json.loads(r["analysis_json"])
                out.append({
                    "eventid": r["eventid"], "host": r["host"],
                    "trigger": r["trigger_name"], "severity": r["severity"],
                    "created_at": r["created_at"],
                    "triggerid": r["objectid"] or "",
                    "status": r["status"] or "ok",
                    "confianca_pct": an.get("confianca_pct", 0),
                    "nivel": an.get("nivel_recomendado", "N1"),
                    "playbook": an.get("playbook_aplicado", ""),
                    "causa_provavel": (an.get("causa_provavel") or "")[:160],
                    "tratado_por": r["tratado_por"] or "",
                    "hipotese_correta": (None if r["hipotese_correta"] is None
                                         else bool(r["hipotese_correta"])),
                })
            return out
        return await asyncio.to_thread(_run)

    async def learnings(self, limit: int = 8) -> list[dict]:
        """Feedbacks recentes com contexto: o painel 'aprendizados' do NOC."""
        def _run() -> list[dict]:
            with self._conn() as con:
                rows = con.execute(
                    """SELECT f.eventid, f.hipotese_correta, f.causa_real,
                              f.solucao, f.created_at,
                              i.trigger_name, i.host, i.analysis_json,
                              a.usuario AS tratado_por
                       FROM feedback f
                       JOIN incidents i USING (eventid)
                       LEFT JOIN assignments a USING (eventid)
                       ORDER BY f.created_at DESC LIMIT ?""",
                    (limit * 4,),  # margem para o filtro de ruído
                ).fetchall()
            out = []
            for r in rows:
                if _is_trivial(r["causa_real"], r["solucao"]):
                    continue
                try:
                    playbook = json.loads(r["analysis_json"]).get(
                        "playbook_aplicado", "")
                except (TypeError, ValueError):
                    playbook = ""
                out.append({
                    "eventid": r["eventid"],
                    "trigger": r["trigger_name"], "host": r["host"],
                    "playbook": playbook,
                    "hipotese_correta": (None if r["hipotese_correta"] is None
                                         else bool(r["hipotese_correta"])),
                    "causa_real": r["causa_real"] or "",
                    "solucao": r["solucao"] or "",
                    "tratado_por": r["tratado_por"] or "",
                    "created_at": r["created_at"],
                })
                if len(out) >= limit:
                    break
            return out
        return await asyncio.to_thread(_run)

    async def stats(self) -> dict:
        def _run():
            with self._conn() as con:
                total = con.execute("SELECT COUNT(*) c FROM incidents").fetchone()["c"]
                fb = con.execute("SELECT COUNT(*) c FROM feedback").fetchone()["c"]
                acertos = con.execute(
                    "SELECT COUNT(*) c FROM feedback WHERE hipotese_correta=1"
                ).fetchone()["c"]
                atribuidos = con.execute(
                    "SELECT COUNT(*) c FROM assignments").fetchone()["c"]
            precisao = round(100 * acertos / fb, 1) if fb else None
            return {"incidentes": total, "com_feedback": fb,
                    "atribuidos": atribuidos, "precisao_ia_pct": precisao}
        return await asyncio.to_thread(_run)
