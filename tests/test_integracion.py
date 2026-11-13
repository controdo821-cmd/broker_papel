"""Prueba de punta a punta con un 'Binance falso' local: REST + WebSocket + puesta al día + comandos."""
import asyncio
import json
import time

import pytest
from aiohttp import web

from broker_papel.comandos import Comandos
from broker_papel.config import Config, CuentaCfg
from broker_papel.db import DB
from broker_papel.mercado import Mercado
from broker_papel.motor import Motor
from broker_papel.vistas import Vistas, sin_html

EXCHANGE_INFO = {"symbols": [{
    "symbol": "BTCUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING",
    "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.10", "minPrice": "556.80", "maxPrice": "4529764"},
        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "120"},
        {"filterType": "MIN_NOTIONAL", "notional": "100"},
        {"filterType": "PERCENT_PRICE", "multiplierUp": "1.0500", "multiplierDown": "0.9500"},
    ]}]}
BRACKETS = {"code": "000000", "data": {"brackets": [{"symbol": "BTCUSDT", "riskBrackets": [
    {"bracketSeq": 1, "bracketNotionalFloor": 0, "bracketNotionalCap": 50000, "bracketMaintenanceMarginRate": 0.004,
     "cumFastMaintenanceAmount": 0, "maxOpenPosLeverage": 125},
    {"bracketSeq": 2, "bracketNotionalFloor": 50000, "bracketNotionalCap": 600000, "bracketMaintenanceMarginRate": 0.005,
     "cumFastMaintenanceAmount": 50, "maxOpenPosLeverage": 100}]}]}}


class FalsoBinance:
    def __init__(self):
        self.ws_clientes = []
        self.subs = []
        self.velas = []      # para la puesta al día
        self.pesos = 0

    def app(self):
        a = web.Application()
        a.router.add_get("/fapi/v1/exchangeInfo", self.info)
        a.router.add_post("/brackets", self.brackets)
        a.router.add_get("/fapi/v1/ticker/bookTicker", self.book)
        a.router.add_get("/fapi/v1/ticker/price", self.precio)
        a.router.add_get("/fapi/v1/klines", self.klines)
        a.router.add_get("/fapi/v1/markPriceKlines", self.klines)
        a.router.add_get("/fapi/v1/fundingRate", self.funding)
        a.router.add_get("/stream", self.ws)
        return a

    def _h(self):
        self.pesos += 1
        return {"X-MBX-USED-WEIGHT-1M": str(self.pesos)}

    async def info(self, r):
        return web.json_response(EXCHANGE_INFO, headers=self._h())

    async def brackets(self, r):
        return web.json_response(BRACKETS)

    async def book(self, r):
        return web.json_response({"bidPrice": "9999.9", "askPrice": "10000.0"}, headers=self._h())

    async def precio(self, r):
        return web.json_response({"price": "10000.0"}, headers=self._h())

    async def klines(self, r):
        ini = int(r.query.get("startTime", 0))
        return web.json_response([v for v in self.velas if v[0] >= ini - 60000], headers=self._h())

    async def funding(self, r):
        return web.json_response([], headers=self._h())

    async def ws(self, r):
        ws = web.WebSocketResponse()
        await ws.prepare(r)
        self.ws_clientes.append(ws)
        async for m in ws:
            self.subs.append(json.loads(m.data))
        return ws

    async def emitir(self, stream, data):
        for ws in self.ws_clientes:
            await ws.send_str(json.dumps({"stream": stream, "data": data}))


async def esperar(cond, t=3.0):
    fin = time.time() + t
    while time.time() < fin:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.mark.asyncio
async def test_punta_a_punta(tmp_path, aiohttp_server):
    fb = FalsoBinance()
    srv = await aiohttp_server(fb.app())
    base = f"http://127.0.0.1:{srv.port}"
    cfg = Config(cuentas=[CuentaCfg("uno"), CuentaCfg("dos"), CuentaCfg("tres")], rest=base,
                 ws=base.replace("http", "ws"), brackets_url=base + "/brackets",
                 deslizamiento_base=0, deslizamiento_por_50k=0, data_dir=tmp_path)
    db = DB(tmp_path / "b.db")
    m = Mercado(cfg, db)
    await m.iniciar()
    assert "BTCUSDT" in m.simbolos and m.brackets("BTCUSDT")[0].max_lev == 125
    motor = Motor(db, m, cfg)
    m.motor = motor
    v = Vistas(motor, m, cfg)
    cmd = Comandos(motor, m, v)
    tarea = asyncio.create_task(m.correr_ws())
    assert await esperar(lambda: fb.ws_clientes)
    now = int(time.time() * 1000)
    T = now + 3600_000
    await fb.emitir("!markPrice@arr@1s", [{"e": "markPriceUpdate", "E": now, "s": "BTCUSDT", "p": "10000.0",
                                            "i": "10001", "r": "0.0001", "T": T}])
    assert await esperar(lambda: m.marca("BTCUSDT") == 10000.0)

    r = sin_html(await cmd.ejecutar("abrir uno BTC long 100 x10 tp 11000 sl 9000"))
    assert "Llenada" in r, r
    assert await esperar(lambda: any(s.get("method") == "SUBSCRIBE" for s in fb.subs))
    assert "btcusdt@kline_1m" in sum((s["params"] for s in fb.subs), [])
    c = motor.cuenta("uno")
    assert motor.posiciones_abiertas(c)[0].amt == pytest.approx(0.1)

    # el último precio sube por encima del TP… pero el TP es por marca: debe esperar a la marca
    k = {"t": now - now % 60000, "o": "10000", "h": "11100", "l": "10000", "c": "11050", "x": False}
    await fb.emitir("btcusdt@kline_1m", {"e": "kline", "E": now + 1, "s": "BTCUSDT", "k": k})
    await asyncio.sleep(0.1)
    assert motor.posiciones_abiertas(c)

    # funding: cambia T -> se cobra con la tasa anterior
    saldo0 = c.saldo
    await fb.emitir("!markPrice@arr@1s", [{"e": "markPriceUpdate", "E": T + 5, "s": "BTCUSDT", "p": "10500.0",
                                            "i": "10500", "r": "0.0002", "T": T + 8 * 3600_000}])
    assert await esperar(lambda: c.saldo < saldo0)
    assert c.saldo == pytest.approx(saldo0 - 0.1 * 10500 * 0.0001)

    await fb.emitir("!markPrice@arr@1s", [{"e": "markPriceUpdate", "E": T + 1000, "s": "BTCUSDT", "p": "11000.0",
                                            "i": "11000", "r": "0.0002", "T": T + 8 * 3600_000}])
    assert await esperar(lambda: not motor.posiciones_abiertas(c))
    assert motor.ordenes_abiertas(c.id) == []

    texto = sin_html(await cmd.ejecutar("estado"))
    assert "conectado" in texto
    tarea.cancel()
    await m.cerrar()


@pytest.mark.asyncio
async def test_puesta_al_dia_tras_caida(tmp_path, aiohttp_server):
    fb = FalsoBinance()
    srv = await aiohttp_server(fb.app())
    base = f"http://127.0.0.1:{srv.port}"
    cfg = Config(cuentas=[CuentaCfg("uno")], rest=base, ws=base.replace("http", "ws"),
                 brackets_url=base + "/brackets", deslizamiento_base=0, deslizamiento_por_50k=0, data_dir=tmp_path)
    db = DB(tmp_path / "b.db")
    m = Mercado(cfg, db)
    await m.iniciar()
    motor = Motor(db, m, cfg)
    m.motor = motor
    m._marca["BTCUSDT"] = {"p": 10000.0, "i": 0, "r": 0, "T": 0, "ts": 0}
    m._ultimo["BTCUSDT"] = 10000.0
    o = motor.colocar("uno", "BTCUSDT", "BUY", "LIMIT", cantidad=0.1, precio=9500)
    # "se cayó" hace 30 minutos; durante la caída hubo una vela que bajó a 9,400
    ahora = int(time.time() * 1000)
    m.procesado_hasta = ahora - 30 * 60000
    t0 = (ahora - 20 * 60000) // 60000 * 60000
    fb.velas = [[t0, "10000", "10010", "9400", "9600"], [t0 + 60000, "9600", "9700", "9550", "9650"]]
    tarea = asyncio.create_task(m.correr_ws())
    assert await esperar(lambda: o.estado == "FILLED", 5)
    f = db.one("SELECT tiempo, precio FROM llenados")
    assert f["precio"] == 9500 and t0 <= f["tiempo"] < t0 + 60000     # con la hora correcta
    tarea.cancel()
    await m.cerrar()
