"""Simula el asistente de Telegram de punta a punta (sin conectarse a Telegram)."""
import pytest

from broker_papel.comandos import Comandos
from broker_papel.informes import Informes
from broker_papel.telegram_bot import BotTelegram
from broker_papel.vistas import Vistas


class BotFalso:
    def __init__(self):
        self.msgs = []

    async def send_message(self, chat, texto, **kw):
        self.msgs.append((texto, kw.get("reply_markup")))

    async def send_photo(self, chat, png, **kw):
        assert png[:4] == b"\x89PNG"
        self.msgs.append(("FOTO", kw.get("reply_markup")))

    async def send_document(self, chat, datos, **kw):
        self.msgs.append(("DOC:" + kw["filename"], None))


class AppFalsa:
    def __init__(self):
        self.bot = BotFalso()


def botones(kb):
    return [b.callback_data for fila in kb.inline_keyboard for b in fila] if kb else []


@pytest.mark.asyncio
async def test_abrir_cerrar_por_telegram(motor):
    async def asegurar(s):
        return None
    motor.m.asegurar = asegurar
    motor.m.en_vivo = True
    motor.cfg.telegram_chats = [7]
    v = Vistas(motor, motor.m, motor.cfg)
    inf = Informes(motor, motor.m, motor.cfg, v)
    tg = BotTelegram(motor.cfg, motor, motor.m, v, Comandos(motor, motor.m, v, informes=inf), inf)
    tg.app = AppFalsa()
    c = motor.cuenta("b")
    await tg._boton(7, c, f"cu:{c.id}")
    c = tg.cuenta_de(7)
    assert c.nombre == "b"
    await tg._boton(7, c, "m:abrir")
    await tg._texto_flujo(7, tg.flujo[7], "btc")
    await tg._boton(7, c, "a:lado:short")
    await tg._boton(7, c, "a:tipo:market")
    await tg._boton(7, c, "a:lev:10")
    await tg._boton(7, c, "a:mg:ISOLATED")
    await tg._texto_flujo(7, tg.flujo[7], "100")
    await tg._texto_flujo(7, tg.flujo[7], "5%")
    await tg._texto_flujo(7, tg.flujo[7], "2%")
    texto, kb = tg.app.bot.msgs[-1]
    assert "Confirmar operación" in texto and "SHORT" in texto and "aislado" in texto
    ok = [b for b in botones(kb) if b.startswith("ok:")][0]
    await tg._boton(7, c, ok)
    p = motor.posiciones_abiertas(c)[0]
    assert p.amt == pytest.approx(-0.1) and p.tipo_margen == "ISOLATED"
    tipos = {(o.tipo, o.precio_stop) for o in motor.ordenes_abiertas(c.id)}
    assert tipos == {("TAKE_PROFIT_MARKET", 9500.0), ("STOP_MARKET", 10200.0)}
    # mover SL desde el menú de posiciones
    await tg._boton(7, c, "pt:BTCUSDT:BOTH")
    await tg._texto_flujo(7, tg.flujo[7], "-")
    await tg._texto_flujo(7, tg.flujo[7], "10150")
    ok = [b for b in botones(tg.app.bot.msgs[-1][1]) if b.startswith("ok:")][0]
    await tg._boton(7, c, ok)
    sls = [o.precio_stop for o in motor.ordenes_abiertas(c.id) if o.tipo == "STOP_MARKET"]
    assert sls == [10150.0]
    # cerrar 100 %
    await tg._boton(7, c, "pc:BTCUSDT:BOTH:100")
    ok = [b for b in botones(tg.app.bot.msgs[-1][1]) if b.startswith("ok:")][0]
    await tg._boton(7, c, ok)
    assert not motor.posiciones_abiertas(c)
    assert motor.ordenes_abiertas(c.id) == []
    # recargar
    await tg._boton(7, c, "m:recargar")
    await tg._texto_flujo(7, tg.flujo[7], "250")
    ok = [b for b in botones(tg.app.bot.msgs[-1][1]) if b.startswith("ok:")][0]
    saldo = c.saldo
    await tg._boton(7, c, ok)
    assert c.saldo == pytest.approx(saldo + 250)
    for que in ("resumen", "pos", "ord", "hist", "ajustes", "cuenta", "exportar", "curva"):
        await tg._boton(7, c, f"m:{que}")
    for d in ("st:todo", "st:hoy", "cal:0:0", "cal:2026:1", "ano:2026", "ex:xlsx", "ex:csv"):
        await tg._boton(7, c, d)
    assert any(m[0].startswith("DOC:b-historial") for m in tg.app.bot.msgs)
    assert "Operaciones cerradas: 1" in [m[0] for m in tg.app.bot.msgs if "Estadísticas" in m[0]][0]
    # gráfica de una posición (velas falsas)
    async def velas(s, i, ini, fin, lim, marca=False):
        t = ini
        return [[t + k * 60000, "10000", "10010", "9990", "10005"] for k in range(50)]
    motor.m.velas = velas
    motor.colocar("b", "BTCUSDT", "BUY", "MARKET", cantidad=0.1, tp=11000, sl=9000)
    await tg._boton(7, c, "pg:BTCUSDT:BOTH")
    assert tg.app.bot.msgs[-1][0] == "FOTO"
    
