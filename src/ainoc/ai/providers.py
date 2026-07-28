"""Camada de provedores de IA.

`OpenAICompatProvider` cobre OpenAI, Azure OpenAI, Ollama, LM Studio, OpenRouter,
vLLM e llama.cpp — todos expõem o protocolo /v1/chat/completions.
"""
import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod

import httpx

logger = logging.getLogger(__name__)


class AIProviderError(Exception):
    pass


class AIProvider(ABC):
    model_name: str = ""  # rótulo do modelo efetivo (para calibração)

    @abstractmethod
    async def complete(self, system: str, user: str) -> str:
        """Retorna o texto bruto da resposta do modelo."""

    async def complete_json(self, system: str, user: str) -> dict:
        """Chama o modelo e faz parse defensivo de JSON."""
        raw = await self.complete(system, user)
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise AIProviderError(f"Resposta não é JSON válido: {raw[:200]}") from exc


class AnthropicProvider(AIProvider):
    def __init__(self, api_key: str, model: str,
                 max_tokens: int = 2000, timeout: float = 60.0):
        self._model = model
        self.model_name = "anthropic:" + model
        self._max_tokens = max_tokens
        self._client = httpx.AsyncClient(
            base_url="https://api.anthropic.com",
            timeout=timeout,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )

    async def complete(self, system: str, user: str) -> str:
        resp = await self._client.post("/v1/messages", json={
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        })
        if resp.status_code != 200:
            raise AIProviderError(f"Anthropic {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        return "".join(b.get("text", "") for b in data.get("content", []))


class OpenAICompatProvider(AIProvider):
    def __init__(self, base_url: str, api_key: str, model: str,
                 max_tokens: int = 2000, timeout: float = 60.0):
        self._model = model
        self.model_name = "openai_compat:" + model
        self._max_tokens = max_tokens
        headers = {"content-type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout, headers=headers
        )

    async def complete(self, system: str, user: str) -> str:
        resp = await self._client.post("/chat/completions", json={
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        })
        if resp.status_code != 200:
            raise AIProviderError(f"Provider {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise AIProviderError(f"Resposta inesperada: {data}") from exc


class ClaudeCodeProvider(AIProvider):
    """Usa o Claude Code CLI em modo headless: `claude -p ... --output-format json`.

    Vantagem: reutiliza a autenticação já feita no laboratório (`claude` logado
    na conta), sem precisar de chave de API separada no .env.

    O prompt é passado via stdin (evita limites de argv) e o resultado vem no
    campo `result` do JSON emitido pelo CLI. `--max-turns 1` e nenhuma
    ferramenta liberada: aqui o Claude Code atua só como modelo de texto.
    """

    def __init__(self, binary: str = "claude", model: str = "",
                 timeout: float = 120.0, bare: bool = False, api_key: str = ""):
        self._binary = binary
        self._model = model
        self._timeout = timeout
        self._bare = bare
        self._api_key = api_key
        self.model_name = "claude_code:" + (model or "default")

    async def complete(self, system: str, user: str) -> str:
        cmd = [
            self._binary, "-p",
            "--output-format", "json",
            "--max-turns", "1",
            "--append-system-prompt", system,
        ]
        if self._model:
            cmd += ["--model", self._model]
        if self._bare:
            # --bare ignora OAuth/keychain: exige credencial explícita
            cmd += ["--bare"]

        env = os.environ.copy()
        if self._bare and self._api_key:
            env["ANTHROPIC_API_KEY"] = self._api_key
        elif not self._bare:
            # fora do --bare, a chave de API teria precedência sobre o token
            # OAuth (CLAUDE_CODE_OAUTH_TOKEN) e quebraria a autenticação
            env.pop("ANTHROPIC_API_KEY", None)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as exc:
            raise AIProviderError(
                f"Claude Code CLI não encontrado ('{self._binary}'). "
                "Instale com: npm install -g @anthropic-ai/claude-code "
                "e autentique com o comando 'claude'."
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=user.encode()), timeout=self._timeout
            )
        except asyncio.TimeoutError as exc:
            proc.kill()
            raise AIProviderError(
                f"Claude Code excedeu o timeout de {self._timeout}s"
            ) from exc

        out_text = stdout.decode(errors="replace")
        if proc.returncode != 0:
            # Em headless, o CLI reporta erros no stdout (JSON com is_error)
            # e sai com código != 0 — a causa real está lá, não no stderr.
            detail = stderr.decode(errors="replace").strip()
            try:
                data = json.loads(out_text)
                if isinstance(data, dict) and data.get("result"):
                    detail = str(data["result"])
            except json.JSONDecodeError:
                if not detail and out_text.strip():
                    detail = out_text.strip()
            raise AIProviderError(
                f"Claude Code saiu com código {proc.returncode}: "
                f"{detail[:400] or '(sem mensagem)'}"
            )
        return parse_claude_code_output(out_text)


def parse_claude_code_output(raw: str) -> str:
    """Extrai o campo `result` do JSON emitido por `claude -p --output-format json`."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AIProviderError(
            f"Saída do Claude Code não é JSON: {raw[:200]}"
        ) from exc
    if isinstance(data, dict) and data.get("is_error"):
        raise AIProviderError(f"Claude Code retornou erro: {data.get('result')}")
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, str) or not result.strip():
        raise AIProviderError(f"Campo 'result' ausente ou vazio: {raw[:200]}")
    return result


def build_provider(settings) -> AIProvider:
    if settings.ai_provider == "claude_code":
        return ClaudeCodeProvider(
            settings.claude_code_bin, settings.ai_model,
            settings.ai_timeout_seconds,
            bare=settings.claude_code_bare, api_key=settings.ai_api_key,
        )
    if settings.ai_provider == "anthropic":
        return AnthropicProvider(
            settings.ai_api_key, settings.ai_model or "claude-sonnet-4-6",
            settings.ai_max_tokens, settings.ai_timeout_seconds,
        )
    return OpenAICompatProvider(
        settings.ai_base_url, settings.ai_api_key, settings.ai_model,
        settings.ai_max_tokens, settings.ai_timeout_seconds,
    )
