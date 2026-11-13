"""Arranque del servicio: datos de Binance + motor + Telegram + control local para la terminal."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal
import sys
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

from aiohttp import web

from .comandos import Comandos
from .config import cargar
from .db import DB
from .mercado import Mercado
from .motor import Motor
from .vistas import Vistas

log = logging.getLogger("app")


def configurar_logs(data_dir):
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    raiz = logging.getLogger()
    raiz.setLevel(logging.INFO)
    fh = RotatingFileHandler(data_dir / "broker.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    raiz.addHandler(fh)
    raiz.addHandler(sh)
    for ruido in ("httpx", "telegram", "aiohttp.access"):
        logging.getLogger(ruido).setLevel(logging.WARNING)


def token_control(data_dir) -> str:
    f = data_dir / "control.token"
    if not f.exists():
        f.write_text(secrets.token_hex(24))
        os.chmod(f, 0o600)
    return f.read_text().strip()


async def servidor_control(cfg, comandos: Comandos):
    token = token_control(cfg.data_dir)

    async def cmd(req: web.Request):
        if req.headers.get("X-Token") != token:
            return web.json_response({"ok": False, "texto": "No autorizado"}, status=403)
        datos = await req.json()
        texto = await comandos.ejecutar(datos.get("args", []), origen="terminal")
        return web.json_response({"ok": True, "texto": texto})

    app = web.Application()
    app.router.add_post("/cmd", cmd)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", cfg.puerto_control)   # solo local, nunca expuesto
    await site.start()
    log.info("Control local escuchando en 127.0.0.1:%s", cfg.puerto_control)
    return runner


async def tareas_diarias(cfg, db, mercado):
    tz = ZoneInfo(cfg.zona_horaria)
    while True:
        ahora = datetime.now(tz)
        prox = ahora.replace(hour=cfg.hora_respaldo, minute=0, second=0, microsecond=0)
        if prox <= ahora:
            prox += timedelta(days=1)
        await asyncio.sleep((prox - ahora).total_seconds())
        try:
            ruta = db.respaldo(cfg.data_dir / "respaldos", cfg.dias_respaldo)
            log.info("Respaldo diario: %s", ruta)
        except Exception:
            log.exception("Respaldo falló")
        try:
            await mercado.refrescar_info(forzar=True)
        except Exception:
            log.exception("Refresco de monedas falló")


async def principal():
    cfg = cargar()
    configurar_logs(cfg.data_dir)
    log.info("Iniciando Broker Papel (solo simulación, datos públicos de Binance).")
    db = DB(cfg.data_dir / "broker.db")
    mercado = Mercado(cfg, db)
    await mercado.iniciar()
    motor = Motor(db, mercado, cfg)
    mercado.motor = motor
    vistas = Vistas(motor, mercado, cfg)
    comandos = Comandos(motor, mercado, vistas,
                        respaldo_fn=lambda: db.respaldo(cfg.data_dir / "respaldos", cfg.dias_respaldo))
    runner = await servidor_control(cfg, comandos)

    bot = None
    if cfg.telegram_token:
        from .telegram_bot import BotTelegram
        bot = BotTelegram(cfg, motor, mercado, vistas, comandos)
        try:
            await bot.iniciar()
        except Exception:
            log.exception("No se pudo iniciar Telegram (revisa el token en config/secretos.toml)")
            bot = None
    else:
        log.warning("Sin token de Telegram: el bot no se inicia.")

    tareas = [asyncio.create_task(mercado.correr_ws()), asyncio.create_task(tareas_diarias(cfg, db, mercado))]
    parar = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, parar.set)
        except NotImplementedError:  # pragma: no cover
            pass
    await parar.wait()
    log.info("Deteniendo…")
    db.set("procesado_hasta", mercado.procesado_hasta)
    for t in tareas:
        t.cancel()
    if bot:
        await bot.detener()
    await runner.cleanup()
    await mercado.cerrar()
    log.info("Detenido.")


def main():
    asyncio.run(principal())


if __name__ == "__main__":
    main()
