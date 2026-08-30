# agent/brain.py — Cerebro del agente: conexion con el LLM
# Generado por AgentKit

"""
Logica de IA del agente. Lee el system prompt de config/prompts.yaml y genera las
respuestas.

Soporta dos proveedores de IA, elegibles con LLM_PROVIDER en el .env:
  - "anthropic" (default) -> Claude, via el SDK oficial de Anthropic
  - "deepseek"             -> DeepSeek, via su API compatible con el formato de OpenAI

El resto del agente (main.py, tests/test_local.py) no sabe ni le importa cual de
los dos esta activo: solo llama a generar_respuesta(). Cambiar de proveedor es
cambiar una variable de entorno, no tocar codigo.
"""

import logging
import os

import httpx
import yaml
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("agentkit")

# ─────────────────────────────────────────────────────────────────────────
# Proveedor de IA
# ─────────────────────────────────────────────────────────────────────────

LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "anthropic").strip().lower()
PROVEEDORES_LLM_SOPORTADOS = ("anthropic", "deepseek")

if LLM_PROVIDER not in PROVEEDORES_LLM_SOPORTADOS:
    logger.warning(
        f"LLM_PROVIDER='{LLM_PROVIDER}' no es valido "
        f"({' | '.join(PROVEEDORES_LLM_SOPORTADOS)}); se usa 'anthropic' por default."
    )
    LLM_PROVIDER = "anthropic"

# ── Anthropic ────────────────────────────────────────────────────────────

client_anthropic = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# El modelo se cambia desde .env, sin tocar el codigo.
#   claude-opus-5     el mas capaz             $5 / $25 por millon de tokens
#   claude-sonnet-5   el balanceado (default)  $3 / $15
#   claude-haiku-4-5  el mas barato y rapido   $1 / $5
# El "or" y no el default de os.getenv: una variable declarada vacia en el .env
# devuelve "" y dejaria al agente sin modelo.
MODELO_ANTHROPIC = os.getenv("ANTHROPIC_MODEL") or "claude-sonnet-5"

# Es un bot de respuestas cortas: con esfuerzo bajo contesta mas rapido y mas barato.
# Dejalo vacio en el .env para no mandar el parametro. Especifico de Claude, DeepSeek
# no tiene un parametro equivalente.
ESFUERZO = os.getenv("ANTHROPIC_EFFORT", "low").strip()

# Los modelos mas viejos no aceptan output_config. Si la primera llamada falla por eso,
# se reintenta sin el parametro y se recuerda para las siguientes.
_soporta_esfuerzo = True

# ── DeepSeek ─────────────────────────────────────────────────────────────

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = (os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
# "deepseek-chat" (V3) para respuestas rapidas, "deepseek-reasoner" (R1) si necesita razonar mas
MODELO_DEEPSEEK = os.getenv("DEEPSEEK_MODEL") or "deepseek-chat"

if LLM_PROVIDER == "deepseek" and not DEEPSEEK_API_KEY:
    logger.warning("LLM_PROVIDER=deepseek pero falta DEEPSEEK_API_KEY: el agente no va a poder responder")

# ── Comun a ambos proveedores ────────────────────────────────────────────

# WhatsApp son mensajes cortos, pero este tope NO es solo la respuesta: en los modelos
# actuales el razonamiento interno tambien cuenta contra el. Con el margen justo, una
# pregunta que exija pensar un poco deja al agente sin espacio para contestar.
MAX_TOKENS = int(os.getenv("ANTHROPIC_MAX_TOKENS") or "4096")


def cargar_config_prompts() -> dict:
    """Lee toda la configuracion desde config/prompts.yaml."""
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/prompts.yaml no encontrado")
        return {}


def cargar_system_prompt() -> str:
    """El system prompt: quien es el agente y que sabe del negocio."""
    return cargar_config_prompts().get(
        "system_prompt", "Eres un asistente util. Responde siempre en espanol."
    )


def obtener_mensaje_error() -> str:
    """Que decirle al cliente cuando algo falla de nuestro lado."""
    return cargar_config_prompts().get(
        "error_message",
        "Lo siento, estoy teniendo problemas tecnicos. Por favor intenta de nuevo en unos minutos.",
    )


def obtener_mensaje_fallback() -> str:
    """Que decirle al cliente cuando no se entendio el mensaje."""
    return cargar_config_prompts().get(
        "fallback_message", "Disculpa, no entendi tu mensaje. Podrias reformularlo?"
    )


def _extraer_texto_anthropic(respuesta) -> str:
    """
    Junta el texto de la respuesta de Claude.

    Ojo: NO se puede hacer respuesta.content[0].text. La respuesta es una lista de
    bloques y el primero no siempre es texto (los modelos que razonan devuelven
    primero un bloque de pensamiento). Hay que filtrar por tipo.
    """
    partes = [bloque.text for bloque in respuesta.content if bloque.type == "text"]
    return "\n".join(p for p in partes if p).strip()


def _es_error_de_esfuerzo(error: Exception) -> bool:
    """
    True solo si el modelo rechazo la llamada POR el parametro output_config/effort.

    Se exige que sea un 400 de peticion invalida y no cualquier error que mencione la
    palabra: un 529 de sobrecarga que la nombre de paso no debe apagar el parametro
    para todo el proceso.
    """
    if getattr(error, "status_code", None) != 400:
        return False
    texto = str(error).lower()
    return "output_config" in texto or "effort" in texto


async def _generar_anthropic(mensajes: list[dict], system_prompt: str) -> str | None:
    """Llama a la API de Anthropic. Retorna el texto o None si fallo."""
    global _soporta_esfuerzo

    extras = {"output_config": {"effort": ESFUERZO}} if (_soporta_esfuerzo and ESFUERZO) else {}

    async def _llamar(parametros_extra: dict):
        return await client_anthropic.messages.create(
            model=MODELO_ANTHROPIC,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=mensajes,
            **parametros_extra,
        )

    try:
        respuesta = await _llamar(extras)
    except Exception as e:  # noqa: BLE001
        if extras and _es_error_de_esfuerzo(e):
            logger.warning(
                f"El modelo {MODELO_ANTHROPIC} no acepta output_config.effort; se reintenta sin ese parametro."
            )
            _soporta_esfuerzo = False
            try:
                respuesta = await _llamar({})
            except Exception as e2:  # noqa: BLE001
                logger.error(f"Error llamando a Claude: {e2}")
                return None
        else:
            logger.error(f"Error llamando a Claude: {e}")
            return None

    if getattr(respuesta, "stop_reason", None) == "max_tokens":
        logger.warning(
            f"La respuesta se corto por llegar al tope de {MAX_TOKENS} tokens. "
            "Si pasa seguido, sube ANTHROPIC_MAX_TOKENS o acorta el system prompt."
        )

    texto = _extraer_texto_anthropic(respuesta)
    if texto:
        logger.info(
            f"Respuesta generada con {MODELO_ANTHROPIC} "
            f"({respuesta.usage.input_tokens} in / {respuesta.usage.output_tokens} out)"
        )
    return texto or None


async def _generar_deepseek(mensajes: list[dict], system_prompt: str) -> str | None:
    """
    Llama a la API de DeepSeek (formato compatible con OpenAI Chat Completions).
    Retorna el texto o None si fallo.
    """
    if not DEEPSEEK_API_KEY:
        logger.error("No se puede llamar a DeepSeek: falta DEEPSEEK_API_KEY")
        return None

    # DeepSeek usa el formato de OpenAI: el system prompt va DENTRO de la lista de
    # mensajes con role "system", no como parametro aparte (a diferencia de Anthropic).
    mensajes_openai = [{"role": "system", "content": system_prompt}] + mensajes

    try:
        async with httpx.AsyncClient(timeout=60.0) as cliente:
            r = await cliente.post(
                f"{DEEPSEEK_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": MODELO_DEEPSEEK,
                    "messages": mensajes_openai,
                    "max_tokens": MAX_TOKENS,
                },
            )
    except httpx.HTTPError as e:
        logger.error(f"Error de red hablando con DeepSeek: {e}")
        return None

    if r.status_code != 200:
        logger.error(f"DeepSeek rechazo la llamada [{r.status_code}]: {r.text[:500]}")
        return None

    cuerpo = r.json()
    try:
        texto = cuerpo["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        logger.warning("DeepSeek devolvio una respuesta con formato inesperado")
        return None

    uso = cuerpo.get("usage", {})
    logger.info(
        f"Respuesta generada con {MODELO_DEEPSEEK} "
        f"({uso.get('prompt_tokens', '?')} in / {uso.get('completion_tokens', '?')} out)"
    )
    return (texto or "").strip() or None


async def generar_respuesta(mensaje: str, historial: list[dict]) -> tuple[str, bool]:
    """
    Genera una respuesta usando el proveedor de IA configurado (LLM_PROVIDER).

    Args:
        mensaje: el mensaje nuevo del cliente
        historial: los mensajes anteriores, [{"role": "user"|"assistant", "content": "..."}]

    Returns:
        (texto, es_respuesta_real)

        "es_respuesta_real" es False cuando lo que se devuelve es un aviso tecnico
        (error o fallback) y no una respuesta del agente. main.py lo usa para no
        guardar esos avisos en el historial: si se guardaran, quedarian contaminando
        el contexto de todos los mensajes siguientes.
    """
    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback(), False

    mensajes = [{"role": m["role"], "content": m["content"]} for m in historial]
    mensajes.append({"role": "user", "content": mensaje})

    system_prompt = cargar_system_prompt()

    if LLM_PROVIDER == "deepseek":
        texto = await _generar_deepseek(mensajes, system_prompt)
    else:
        texto = await _generar_anthropic(mensajes, system_prompt)

    if not texto:
        logger.warning(f"El proveedor '{LLM_PROVIDER}' no devolvio una respuesta valida")
        return obtener_mensaje_error(), False

    return texto, True
