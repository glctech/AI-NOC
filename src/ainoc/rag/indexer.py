"""RAG Engine (Fase 3) — indexação e busca lexical (BM25) da base de conhecimento.

Zero dependências: funciona offline e sem custo de embeddings, adequado ao
laboratório. A interface (search → trechos com fonte e score) permite trocar
o backend por embeddings vetoriais depois sem mudar o pipeline.

Formatos: .md e .txt nativos; .pdf e .docx se pypdf / python-docx existirem.
"""
import logging
import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_WORD = re.compile(r"[a-z0-9]{2,}")
_STOP = {
    "de", "da", "do", "das", "dos", "em", "no", "na", "nos", "nas", "um",
    "uma", "para", "por", "com", "sem", "que", "ou", "se", "ao", "os", "as",
    "the", "and", "for", "with", "this", "that", "from", "are", "is", "to",
}


def _tokenize(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", text.lower())
    text = text.encode("ascii", "ignore").decode()
    return [t for t in _WORD.findall(text) if t not in _STOP]


def score_texts(query: str, docs: list[str], top_k: int) -> list[tuple[int, float]]:
    """Ranking BM25 simplificado de `docs` para `query` → [(índice, score)]."""
    q = _tokenize(query)
    if not q or not docs:
        return []
    tokenized = [_tokenize(d) for d in docs]
    n = len(tokenized)
    avgdl = sum(len(t) for t in tokenized) / n or 1.0
    df: dict[str, int] = {}
    for toks in tokenized:
        for term in set(toks):
            df[term] = df.get(term, 0) + 1
    k1, b = 1.5, 0.75
    scored = []
    for i, toks in enumerate(tokenized):
        if not toks:
            continue
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        score = 0.0
        for term in q:
            f = tf.get(term)
            if not f:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * len(toks) / avgdl))
        if score > 0:
            scored.append((i, score))
    scored.sort(key=lambda x: -x[1])
    return scored[:top_k]


@dataclass
class Chunk:
    source: str    # nome do arquivo
    section: str   # último heading visto
    text: str

    def to_prompt_dict(self, score: float) -> dict:
        return {"fonte": self.source, "secao": self.section,
                "trecho": self.text[:1200], "score": round(score, 3)}


class KnowledgeBase:
    def __init__(self, directory: str | Path, max_chunk_chars: int = 1200):
        self._dir = Path(directory)
        self._max = max_chunk_chars
        self._chunks: list[Chunk] = []
        self.reindex()

    @property
    def size(self) -> int:
        return len(self._chunks)

    def reindex(self) -> dict:
        self._chunks = []
        skipped: list[str] = []
        if not self._dir.is_dir():
            logger.warning("Diretório da KB inexistente: %s", self._dir)
            return {"chunks": 0, "arquivos": 0, "ignorados": []}
        files = [p for p in sorted(self._dir.rglob("*"))
                 if p.suffix.lower() in (".md", ".txt", ".pdf", ".docx")]
        for path in files:
            text = self._read(path, skipped)
            if text:
                self._chunks.extend(self._split(path.name, text))
        logger.info("KB indexada: %d trechos de %d arquivo(s).",
                    len(self._chunks), len(files) - len(skipped))
        return {"chunks": len(self._chunks),
                "arquivos": len(files) - len(skipped), "ignorados": skipped}

    def _read(self, path: Path, skipped: list[str]) -> str:
        suffix = path.suffix.lower()
        try:
            if suffix in (".md", ".txt"):
                return path.read_text(encoding="utf-8", errors="replace")
            if suffix == ".pdf":
                try:
                    from pypdf import PdfReader
                except ImportError:
                    skipped.append(f"{path.name} (instale pypdf)")
                    return ""
                return "\n".join(p.extract_text() or ""
                                 for p in PdfReader(str(path)).pages)
            if suffix == ".docx":
                try:
                    import docx
                except ImportError:
                    skipped.append(f"{path.name} (instale python-docx)")
                    return ""
                return "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
        except Exception as exc:  # arquivo ruim não derruba a indexação
            logger.error("Falha ao ler %s: %s", path.name, exc)
            skipped.append(f"{path.name} (erro de leitura)")
        return ""

    def _split(self, source: str, text: str) -> list[Chunk]:
        """Divide por headings markdown; quebra seções longas por parágrafo."""
        chunks: list[Chunk] = []
        section, buf = "", ""

        def flush():
            nonlocal buf
            if buf.strip():
                chunks.append(Chunk(source, section, buf.strip()))
            buf = ""

        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                flush()
                section = line.strip("# ").strip()
                continue
            if len(buf) + len(line) > self._max:
                flush()
            buf += line + "\n"
        flush()
        return chunks

    def search(self, query: str, top_k: int = 3) -> list[dict]:
        ranked = score_texts(query, [c.text + " " + c.section
                                     for c in self._chunks], top_k)
        return [self._chunks[i].to_prompt_dict(score) for i, score in ranked]

    def add_note(self, filename: str, content: str) -> Path:
        """Grava uma nota de aprendizado na KB e reindexa."""
        target = self._dir / "aprendizado"
        target.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w.-]", "_", filename)
        path = target / safe
        path.write_text(content, encoding="utf-8")
        self.reindex()
        return path
