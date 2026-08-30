# agent/tools.py — Herramientas del agente
# Generado por AgentKit

"""
Herramientas especificas de DavaDigital.

OJO: estas funciones NO se ejecutan solas todavia. La informacion del negocio le
llega al agente por el system prompt (config/prompts.yaml), asi que para CONTESTAR
preguntas no hace falta nada de aca. Este archivo es el lugar para las ACCIONES
—agendar, registrar un lead, tomar un pedido, abrir un ticket— y conectarlas al
ciclo de tool use de Claude/DeepSeek es un paso aparte.

Casos de uso elegidos para Dano (DavaDigital):
  1. FAQ                  -> buscar_en_knowledge() ya esta listo abajo
  2. Agendar citas         -> obtener_slots_disponibles() / reservar_cita() (stub)
  3. Calificar leads        -> registrar_lead()
  4. Tomar pedidos          -> agregar_al_carrito() / confirmar_pedido()
  5. Soporte post-venta      -> crear_ticket()

Las funciones marcadas como "(stub)" guardan la informacion en un archivo JSON local
a modo de demo, para que puedas probar el flujo end-to-end. Cuando quieras que se
conecten a un sistema real (Google Calendar, tu CRM, tu inventario, etc.) o que Claude
las llame automaticamente durante la conversacion, avisame y lo armamos juntos.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import yaml

logger = logging.getLogger("agentkit")

CARPETA_KNOWLEDGE = Path("knowledge")
CARPETA_DATOS = Path("data")
CARPETA_DATOS.mkdir(exist_ok=True)

ARCHIVO_CITAS = CARPETA_DATOS / "citas.json"
ARCHIVO_LEADS = CARPETA_DATOS / "leads.json"
ARCHIVO_PEDIDOS = CARPETA_DATOS / "pedidos.json"
ARCHIVO_TICKETS = CARPETA_DATOS / "tickets.json"


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


def _leer_json(ruta: Path) -> list:
    if not ruta.exists():
        return []
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _agregar_json(ruta: Path, registro: dict):
    datos = _leer_json(ruta)
    datos.append(registro)
    ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")


# ════════════════════════════════════════════════════════════
# 1. FAQ — informacion del negocio
# ════════════════════════════════════════════════════════════


def cargar_info_negocio() -> dict:
    """Carga la informacion del negocio desde config/business.yaml."""
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/business.yaml no encontrado")
        return {}


def obtener_horario() -> dict:
    """Retorna el horario de atencion del negocio."""
    info = cargar_info_negocio()
    return {
        "horario": info.get("negocio", {}).get("horario", "No disponible"),
        "esta_abierto": True,  # TODO: calcular segun la hora actual y el horario
    }


def buscar_en_knowledge(consulta: str) -> str:
    """
    Busca informacion en los archivos de /knowledge.
    Retorna los fragmentos que coinciden con la consulta.
    """
    if not CARPETA_KNOWLEDGE.is_dir():
        return "No hay archivos de conocimiento disponibles."

    resultados = []
    for ruta in sorted(CARPETA_KNOWLEDGE.iterdir()):
        if ruta.name.startswith(".") or not ruta.is_file():
            continue
        try:
            contenido = ruta.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binarios y archivos ilegibles se saltean
        if consulta.lower() in contenido.lower():
            resultados.append(f"[{ruta.name}]: {contenido[:500]}")

    if resultados:
        return "\n---\n".join(resultados)
    return "No encontre informacion especifica sobre eso en mis archivos."


# ════════════════════════════════════════════════════════════
# 2. Agendar citas (reparaciones, consultas de desarrollo, etc.)
# ════════════════════════════════════════════════════════════


def obtener_slots_disponibles(fecha: str) -> list[dict]:
    """
    Retorna los horarios disponibles para agendar en una fecha (YYYY-MM-DD).

    STUB: hoy no esta conectado a un calendario real. Devuelve slots fijos dentro
    del horario de atencion de DavaDigital, sin chequear ocupacion real.
    Cuando conectes Google Calendar / Calendly / tu sistema, esta funcion es la
    que hay que reemplazar.
    """
    return [
        {"hora": "11:30", "disponible": True},
        {"hora": "13:00", "disponible": True},
        {"hora": "14:00", "disponible": True},
        {"hora": "17:30", "disponible": True},
        {"hora": "19:00", "disponible": True},
    ]


def reservar_cita(telefono: str, fecha: str, hora: str, servicio: str) -> dict:
    """
    Registra una solicitud de cita. NO es una confirmacion definitiva: queda
    pendiente de que el equipo de DavaDigital la valide.
    """
    registro = {
        "telefono": telefono,
        "fecha": fecha,
        "hora": hora,
        "servicio": servicio,
        "estado": "pendiente_confirmacion",
        "creado_en": _ahora(),
    }
    _agregar_json(ARCHIVO_CITAS, registro)
    logger.info(f"Solicitud de cita registrada: {telefono} — {fecha} {hora} — {servicio}")
    return registro


def cancelar_cita(telefono: str, fecha: str, hora: str) -> bool:
    """Marca una cita como cancelada. STUB: reescribe el registro por telefono+fecha+hora."""
    citas = _leer_json(ARCHIVO_CITAS)
    encontrada = False
    for cita in citas:
        if cita.get("telefono") == telefono and cita.get("fecha") == fecha and cita.get("hora") == hora:
            cita["estado"] = "cancelada"
            encontrada = True
    if encontrada:
        ARCHIVO_CITAS.write_text(json.dumps(citas, ensure_ascii=False, indent=2), encoding="utf-8")
    return encontrada


# ════════════════════════════════════════════════════════════
# 3. Calificar y atender leads (consultoria / desarrollo)
# ════════════════════════════════════════════════════════════


def registrar_lead(telefono: str, nombre: str, interes: str, urgencia: str = "no especificada") -> dict:
    """Guarda un lead interesado en consultoria o desarrollo de software."""
    registro = {
        "telefono": telefono,
        "nombre": nombre,
        "interes": interes,
        "urgencia": urgencia,
        "creado_en": _ahora(),
    }
    _agregar_json(ARCHIVO_LEADS, registro)
    logger.info(f"Lead registrado: {telefono} — {nombre} — {interes}")
    return registro


def escalar_a_vendedor(telefono: str, contexto: str) -> bool:
    """
    Marca un lead como listo para que un asesor humano de DavaDigital lo tome.
    STUB: por ahora solo lo loguea. Conectalo a Slack/email/CRM cuando quieras
    notificaciones en tiempo real.
    """
    logger.info(f"Lead listo para escalar a un asesor humano: {telefono} — {contexto}")
    return True


# ════════════════════════════════════════════════════════════
# 4. Tomar pedidos (equipos a reparacion, presupuestos)
# ════════════════════════════════════════════════════════════


def confirmar_pedido(telefono: str, equipo: str, falla: str, datos_contacto: str) -> dict:
    """Registra una solicitud de reparacion/pedido. Queda pendiente de confirmacion."""
    registro = {
        "telefono": telefono,
        "equipo": equipo,
        "falla": falla,
        "datos_contacto": datos_contacto,
        "estado": "pendiente_confirmacion",
        "creado_en": _ahora(),
    }
    _agregar_json(ARCHIVO_PEDIDOS, registro)
    logger.info(f"Pedido registrado: {telefono} — {equipo} — {falla}")
    return registro


# ════════════════════════════════════════════════════════════
# 5. Soporte post-venta
# ════════════════════════════════════════════════════════════


def crear_ticket(telefono: str, problema: str) -> str:
    """Abre un ticket de soporte post-venta. Retorna el id del ticket."""
    tickets = _leer_json(ARCHIVO_TICKETS)
    ticket_id = f"TCK-{len(tickets) + 1:04d}"
    registro = {
        "ticket_id": ticket_id,
        "telefono": telefono,
        "problema": problema,
        "estado": "abierto",
        "creado_en": _ahora(),
    }
    _agregar_json(ARCHIVO_TICKETS, registro)
    logger.info(f"Ticket creado: {ticket_id} — {telefono} — {problema}")
    return ticket_id


def consultar_ticket(ticket_id: str) -> dict | None:
    """Busca el estado de un ticket por su id."""
    tickets = _leer_json(ARCHIVO_TICKETS)
    for t in tickets:
        if t.get("ticket_id") == ticket_id:
            return t
    return None


def escalar_ticket(ticket_id: str, razon: str) -> bool:
    """Marca un ticket como escalado a un tecnico/humano."""
    tickets = _leer_json(ARCHIVO_TICKETS)
    encontrado = False
    for t in tickets:
        if t.get("ticket_id") == ticket_id:
            t["estado"] = "escalado"
            t["razon_escalado"] = razon
            encontrado = True
    if encontrado:
        ARCHIVO_TICKETS.write_text(json.dumps(tickets, ensure_ascii=False, indent=2), encoding="utf-8")
    return encontrado
