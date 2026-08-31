# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Generado por AgentKit

"""
Servidor principal del agente.
Funciona con cualquier proveedor (Zernio, Meta) gracias a la capa de providers.
"""

import asyncio
import logging
import os
from collections import defaultdict
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from agent.brain import generar_respuesta, obtener_mensaje_error
from agent.memory import (
    esta_pausada,
    guardar_mensaje,
    inicializar_db,
    liberar_evento,
    limpiar_eventos_viejos,
    listar_pausadas,
    marcar_evento_procesado,
    obtener_historial,
    pausar_conversacion,
    reanudar_conversacion,
)
from agent.providers import obtener_proveedor
from agent.providers.base import MensajeEntrante

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("agentkit")
# En desarrollo queremos el detalle de NUESTRO agente, no el de las librerias.
# Poner el nivel raiz en DEBUG llena la terminal de ruido de aiosqlite y httpx
# y hace imposible leer lo que hizo el agente.
logger.setLevel(logging.DEBUG if ENVIRONMENT == "development" else logging.INFO)

PORT = int(os.getenv("PORT", "8000"))

# Numeros del equipo de DavaDigital que pueden mandarle comandos al bot (/pausar,
# /reanudar, /estado) para tomar el control de una conversacion a mano. Se
# configuran en .env como ADMIN_PHONE_NUMBERS, separados por coma.
ADMIN_PHONE_NUMBERS = {
    numero.strip().lstrip("+")
    for numero in os.getenv("ADMIN_PHONE_NUMBERS", "").split(",")
    if numero.strip()
}
if not ADMIN_PHONE_NUMBERS:
    logger.warning(
        "ADMIN_PHONE_NUMBERS no esta configurado: nadie va a poder usar "
        "/pausar, /reanudar o /estado para tomar conversaciones a mano."
    )


async def manejar_comando_admin(texto: str) -> str | None:
    """
    Interpreta un comando de un numero admin (/pausar, /reanudar, /estado).

    Retorna el texto de confirmacion a mandarle de vuelta al admin, o None si el
    mensaje no empieza con "/" (no es un comando: el admin esta hablando normal,
    por ejemplo probando el bot como si fuera cliente).
    """
    texto = texto.strip()
    if not texto.startswith("/"):
        return None

    partes = texto.split()
    comando = partes[0].lower()

    if comando == "/pausar":
        if len(partes) < 2:
            return "Uso: /pausar <telefono> [horas]\nEj: /pausar 5215512345678 2"
        telefono = partes[1].lstrip("+")
        horas = None
        if len(partes) >= 3:
            try:
                horas = float(partes[2])
            except ValueError:
                return f"'{partes[2]}' no es un numero valido de horas."
        await pausar_conversacion(telefono, horas)
        vigencia = f"por {horas}h" if horas else "hasta que mandes /reanudar"
        return f"Bot pausado para {telefono} ({vigencia}). No le va a responder automatico."

    if comando == "/reanudar":
        if len(partes) < 2:
            return "Uso: /reanudar <telefono>"
        telefono = partes[1].lstrip("+")
        ok = await reanudar_conversacion(telefono)
        return f"Bot reanudado para {telefono}." if ok else f"{telefono} no estaba pausado."

    if comando == "/estado":
        pausadas = await listar_pausadas()
        if not pausadas:
            return "No hay conversaciones pausadas ahora mismo."
        lineas = []
        for p in pausadas:
            vigencia = (
                p["pausado_hasta"].strftime("%d/%m %H:%M") if p["pausado_hasta"] else "indefinido"
            )
            lineas.append(f"- {p['telefono']} (hasta: {vigencia})")
        return "Conversaciones pausadas:\n" + "\n".join(lineas)

    return f"Comando no reconocido: {comando}\nComandos: /pausar, /reanudar, /estado"

# Un candado por numero de telefono. En WhatsApp es normal que alguien mande "hola" y
# medio segundo despues la pregunta de verdad: sin esto los dos mensajes se procesarian
# en paralelo, los dos leerian el mismo historial y las escrituras quedarian intercaladas.
_candados: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# Si la configuracion esta mal, guardamos el error y lo mostramos en el health check,
# en vez de reventar en el import y dejar a Railway reiniciando el contenedor a ciegas.
proveedor = None
error_configuracion: str | None = None
try:
    proveedor = obtener_proveedor()
except Exception as e:  # noqa: BLE001 — cualquier problema de configuracion
    error_configuracion = str(e)

# Resultado del chequeo de credenciales que se hace al arrancar. Se expone en el health
# check: que el servidor conteste no significa que el agente pueda responder por WhatsApp.
estado_proveedor: dict = {"ok": None, "detalle": "sin verificar"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Prepara la base de datos y chequea el proveedor al arrancar."""
    await inicializar_db()
    await limpiar_eventos_viejos()
    logger.info("Base de datos lista")
    logger.info(f"Servidor AgentKit escuchando en el puerto {PORT}")

    global estado_proveedor
    if proveedor is not None:
        logger.info(f"Proveedor de WhatsApp: {proveedor.__class__.__name__}")
        ok, detalle = await proveedor.verificar_conexion()
        estado_proveedor = {"ok": ok, "detalle": detalle}
        logger.info(f"Conexion con el proveedor: {'OK' if ok else 'ERROR'} — {detalle}")
    else:
        logger.error(f"Proveedor de WhatsApp NO configurado: {error_configuracion}")

    yield


app = FastAPI(title="AgentKit — WhatsApp AI Agent", version="2.0.0", lifespan=lifespan)


@app.get("/")
async def health_check():
    """Endpoint de salud para Railway y monitoreo."""
    if error_configuracion:
        return {"status": "error", "service": "agentkit", "detalle": error_configuracion}

    # Se responde 200 aunque las credenciales esten mal, para que Railway no marque el
    # deploy como caido y puedas leer el diagnostico. El detalle esta en el cuerpo.
    return {
        "status": "ok" if estado_proveedor["ok"] else "degradado",
        "service": "agentkit",
        "proveedor": proveedor.__class__.__name__ if proveedor else None,
        "conexion": estado_proveedor,
    }


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    """Verificacion GET del webhook. La pide Meta; para Zernio no hace nada."""
    if proveedor is None:
        raise HTTPException(status_code=503, detail=error_configuracion or "Proveedor no configurado")

    respuesta = await proveedor.validar_webhook(request)
    if respuesta is not None:
        return PlainTextResponse(respuesta)

    # Meta pide un 403 cuando manda hub.mode=subscribe y el verify_token no coincide.
    # Devolverle 200 le hace creer que la URL quedo verificada cuando no es cierto.
    if request.query_params.get("hub.mode") == "subscribe":
        raise HTTPException(status_code=403, detail="Verify token incorrecto")

    return {"status": "ok"}


@app.post("/webhook")
async def webhook_handler(request: Request, tareas: BackgroundTasks):
    """
    Recibe los mensajes de WhatsApp.

    Contesta 200 de inmediato y procesa el mensaje en segundo plano.

    Esto NO es un detalle de estilo. Los proveedores esperan un 2xx en unos 5
    segundos y, si no lo reciben, reintentan el mismo evento hasta 7 veces. Como
    llamar al LLM tarda mas que eso, procesar antes de contestar hace que el
    cliente reciba la misma respuesta repetida. Por eso: responder primero,
    trabajar despues.
    """
    if proveedor is None:
        raise HTTPException(status_code=503, detail=error_configuracion or "Proveedor no configurado")

    if not await proveedor.verificar_firma(request):
        raise HTTPException(status_code=401, detail="Firma del webhook invalida")

    try:
        mensajes = await proveedor.parsear_webhook(request)
    except Exception as e:  # noqa: BLE001
        # Un payload raro no debe hacer que el proveedor reintente para siempre
        logger.error(f"No se pudo leer el webhook: {e}")
        return {"status": "ignorado"}

    encolados = 0
    for msg in mensajes:
        if msg.es_propio or not msg.texto.strip():
            continue

        # La entrega es "al menos una vez": el mismo evento puede llegar dos veces
        evento_id = msg.contexto.get("evento_id") or msg.mensaje_id
        if evento_id and not await marcar_evento_procesado(evento_id):
            logger.info(f"Evento repetido, se ignora: {evento_id}")
            continue

        logger.info(f"Mensaje de {msg.telefono}: {msg.texto}")
        tareas.add_task(procesar_mensaje, msg)
        encolados += 1

    return {"status": "ok", "encolados": encolados}


async def procesar_mensaje(msg: MensajeEntrante):
    """
    Genera la respuesta y la manda de vuelta. Corre fuera del ciclo del webhook.

    Se toma un candado por telefono: dos mensajes seguidos del mismo cliente se
    atienden en orden, no en paralelo, para que el historial no se mezcle.
    """
    evento_id = msg.contexto.get("evento_id") or msg.mensaje_id
    telefono_normalizado = msg.telefono.lstrip("+")

    async with _candados[msg.telefono]:
        try:
            # Comandos de administrador (/pausar, /reanudar, /estado): solo si el
            # mensaje viene de un numero en ADMIN_PHONE_NUMBERS Y empieza con "/".
            # No pasan por el LLM ni se guardan en el historial del cliente.
            if telefono_normalizado in ADMIN_PHONE_NUMBERS:
                respuesta_comando = await manejar_comando_admin(msg.texto)
                if respuesta_comando is not None:
                    await proveedor.enviar_mensaje(msg.telefono, respuesta_comando, msg.contexto)
                    logger.info(f"Comando de admin ejecutado por {msg.telefono}: {msg.texto}")
                    return

            # Si alguien del equipo tomo esta conversacion a mano (/pausar), el bot
            # se queda callado: no llama al LLM ni responde. El evento ya se marco
            # como procesado en el webhook, asi que no se reintenta de mas.
            if await esta_pausada(msg.telefono):
                logger.info(f"Conversacion pausada, el bot no responde: {msg.telefono}")
                return

            # El historial se lee ANTES de guardar el mensaje actual: brain.py agrega
            # el mensaje nuevo al final, y asi no queda duplicado.
            historial = await obtener_historial(msg.telefono)
            respuesta, es_respuesta_real = await generar_respuesta(msg.texto, historial)

            enviado = await proveedor.enviar_mensaje(msg.telefono, respuesta, msg.contexto)

            if not enviado:
                # El evento se marco como procesado ANTES de llegar hasta aca, para que dos
                # entregas simultaneas no se dupliquen. Si el envio fallo, hay que soltarlo:
                # si no, el reintento del proveedor se descartaria por duplicado y el cliente
                # se quedaria sin respuesta para siempre.
                logger.error(f"No se pudo enviar la respuesta a {msg.telefono}; se libera el evento")
                await liberar_evento(evento_id)
                return

            # Solo se guarda en el historial lo que de verdad es conversacion. Los avisos
            # tecnicos ("estoy teniendo problemas") no son un turno del agente: guardarlos
            # los deja contaminando el contexto de todos los mensajes que vengan despues.
            if es_respuesta_real:
                await guardar_mensaje(msg.telefono, "user", msg.texto)
                await guardar_mensaje(msg.telefono, "assistant", respuesta)

            logger.info(f"Respuesta enviada a {msg.telefono}: {respuesta}")

        except Exception as e:  # noqa: BLE001
            logger.exception(f"Error procesando el mensaje de {msg.telefono}: {e}")
            await liberar_evento(evento_id)
            try:
                await proveedor.enviar_mensaje(msg.telefono, obtener_mensaje_error(), msg.contexto)
            except Exception:  # noqa: BLE001
                logger.error("Tampoco se pudo avisarle al cliente del error")