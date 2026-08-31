# agent/memory.py — Memoria de conversaciones
# Generado por AgentKit

"""
Guarda el historial de cada conversacion por numero de telefono, y lleva registro de
que eventos de webhook ya se atendieron.

SQLite en local, PostgreSQL en produccion.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from sqlalchemy import DateTime, Integer, String, Text, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

load_dotenv()
logger = logging.getLogger("agentkit")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./agentkit.db")

# Railway entrega la URL de PostgreSQL con el esquema "postgresql://" (o "postgres://").
# SQLAlchemy en modo asincrono necesita que el driver sea explicito.
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

# En produccion, SQLite vive dentro del contenedor y el disco del contenedor es efimero:
# cada redespliegue borra el historial de todas las conversaciones. Avisarlo fuerte, porque
# el agente arranca igual y el problema recien se nota cuando un cliente vuelve a escribir.
if DATABASE_URL.startswith("sqlite") and os.getenv("ENVIRONMENT") == "production":
    logger.warning(
        "Estas en produccion con SQLite. El historial se va a borrar en cada redespliegue. "
        "Agrega PostgreSQL y configura DATABASE_URL para que el agente recuerde a sus clientes."
    )

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def ahora() -> datetime:
    """Hora actual en UTC, con zona horaria."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Mensaje(Base):
    """Un mensaje del historial de conversacion."""

    __tablename__ = "mensajes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    role: Mapped[str] = mapped_column(String(20))  # "user" o "assistant"
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


class EventoProcesado(Base):
    """
    Eventos de webhook que ya se atendieron.

    Los proveedores entregan "al menos una vez": el mismo evento puede llegar dos veces.
    Sin esta tabla, el cliente recibiria la misma respuesta repetida.
    """

    __tablename__ = "eventos_procesados"

    evento_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, index=True)


class ConversacionPausada(Base):
    """
    Conversaciones donde un humano del equipo tomo el control.

    Mientras una conversacion esta aca, el bot recibe el mensaje (se marca como
    procesado para no reintentar), pero NO llama al LLM ni responde: se asume que
    alguien del equipo esta contestando a mano desde el dashboard del proveedor.
    """

    __tablename__ = "conversaciones_pausadas"

    telefono: Mapped[str] = mapped_column(String(50), primary_key=True)
    pausado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)
    # None = pausado indefinidamente, hasta que alguien mande /reanudar a mano
    pausado_hasta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


async def inicializar_db():
    """Crea las tablas si no existen."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def marcar_evento_procesado(evento_id: str) -> bool:
    """
    Registra un evento. Retorna True si es nuevo, False si ya se habia procesado.

    La unicidad la garantiza la base de datos (clave primaria), no una consulta previa:
    asi dos webhooks que llegan al mismo tiempo no pasan los dos.
    """
    if not evento_id:
        return True  # sin id no podemos deduplicar: se procesa

    async with async_session() as session:
        session.add(EventoProcesado(evento_id=evento_id, creado_en=ahora()))
        try:
            await session.commit()
            return True
        except IntegrityError:
            await session.rollback()
            return False


async def liberar_evento(evento_id: str):
    """
    Borra la marca de un evento para que el reintento del proveedor SI se procese.

    Se usa cuando el mensaje se marco como procesado pero despues fallo el envio de la
    respuesta. Sin esto, el reintento se descartaria por duplicado y el cliente se
    quedaria sin respuesta para siempre.
    """
    if not evento_id:
        return
    async with async_session() as session:
        await session.execute(delete(EventoProcesado).where(EventoProcesado.evento_id == evento_id))
        await session.commit()


async def limpiar_eventos_viejos(dias: int = 7):
    """Borra los eventos de hace mas de N dias para que la tabla no crezca sin fin."""
    limite = ahora() - timedelta(days=dias)
    async with async_session() as session:
        resultado = await session.execute(
            delete(EventoProcesado).where(EventoProcesado.creado_en < limite)
        )
        await session.commit()
    if resultado.rowcount:
        logger.info(f"Se limpiaron {resultado.rowcount} eventos de mas de {dias} dias")


async def guardar_mensaje(telefono: str, role: str, content: str):
    """Guarda un mensaje en el historial de esa conversacion."""
    async with async_session() as session:
        session.add(Mensaje(telefono=telefono, role=role, content=content, timestamp=ahora()))
        await session.commit()


async def obtener_historial(telefono: str, limite: int = 20) -> list[dict]:
    """
    Devuelve los ultimos N mensajes de una conversacion, en orden cronologico.

    Se ordena por id y no por timestamp: dos mensajes guardados en el mismo instante
    tienen el mismo timestamp, y el orden entre ellos quedaria librado al azar.
    """
    async with async_session() as session:
        resultado = await session.execute(
            select(Mensaje)
            .where(Mensaje.telefono == telefono)
            .order_by(Mensaje.id.desc())
            .limit(limite)
        )
        mensajes = list(resultado.scalars().all())

    mensajes.reverse()  # vienen del mas nuevo al mas viejo: los damos vuelta

    # La API de Claude exige que el historial empiece con un mensaje del usuario.
    # Si por un error anterior quedo un "assistant" suelto al principio, lo sacamos.
    while mensajes and mensajes[0].role != "user":
        mensajes.pop(0)

    return [{"role": m.role, "content": m.content} for m in mensajes]


async def limpiar_historial(telefono: str):
    """Borra todo el historial de una conversacion."""
    async with async_session() as session:
        await session.execute(delete(Mensaje).where(Mensaje.telefono == telefono))
        await session.commit()


async def pausar_conversacion(telefono: str, horas: float | None = None):
    """
    Pausa el bot para este telefono: deja de responder automaticamente.

    Si "horas" es None, queda pausado indefinidamente hasta que alguien mande
    /reanudar. Si se da un numero, se reanuda solo despues de esas horas.
    """
    hasta = ahora() + timedelta(hours=horas) if horas else None
    async with async_session() as session:
        existente = await session.get(ConversacionPausada, telefono)
        if existente:
            existente.pausado_en = ahora()
            existente.pausado_hasta = hasta
        else:
            session.add(ConversacionPausada(telefono=telefono, pausado_en=ahora(), pausado_hasta=hasta))
        await session.commit()


async def reanudar_conversacion(telefono: str) -> bool:
    """Quita la pausa de un telefono. Retorna True si de verdad estaba pausado."""
    async with async_session() as session:
        existente = await session.get(ConversacionPausada, telefono)
        if not existente:
            return False
        await session.delete(existente)
        await session.commit()
        return True


async def esta_pausada(telefono: str) -> bool:
    """
    True si el bot NO debe responder a este telefono ahora mismo.

    Si la pausa ya vencio (pausado_hasta quedo en el pasado), se limpia sola aca
    y se reanuda: asi "/pausar X 2" de verdad dura solo 2 horas sin que nadie
    tenga que acordarse de reanudar a mano.
    """
    async with async_session() as session:
        existente = await session.get(ConversacionPausada, telefono)
        if not existente:
            return False
        pausado_hasta = existente.pausado_hasta
        # SQLite no conserva la zona horaria al guardar: al leerlo de vuelta viene
        # "naive" aunque se haya guardado como aware. Sin esto, comparar contra
        # ahora() (aware) truena con TypeError.
        if pausado_hasta and pausado_hasta.tzinfo is None:
            pausado_hasta = pausado_hasta.replace(tzinfo=timezone.utc)
        if pausado_hasta and pausado_hasta < ahora():
            await session.delete(existente)
            await session.commit()
            return False
        return True


async def listar_pausadas() -> list[dict]:
    """Todas las conversaciones pausadas ahora mismo, para el comando /estado."""
    async with async_session() as session:
        resultado = await session.execute(select(ConversacionPausada))
        filas = list(resultado.scalars().all())
    return [
        {"telefono": f.telefono, "pausado_en": f.pausado_en, "pausado_hasta": f.pausado_hasta}
        for f in filas
    ]