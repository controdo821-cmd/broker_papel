"""Datos públicos de Binance Futures: casi todo por WebSocket, muy poco REST.

- !markPrice@arr@1s  -> precio de marca, índice, funding y hora del próximo funding de TODAS las monedas.
- <moneda>@kline_1m   -> último precio con máximos/mínimos (solo monedas con posiciones u órdenes).
- <moneda>@depth5@500ms -> mejor bid/ask (solo esas mismas monedas).
REST: exchangeInfo y brackets una vez al día, velas solo para ponerse al día o para gráficas.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path

import aiohttp

from . import calc
from .modelos import SymbolInfo

log = logging.getLogger("mercado")


class Limitador:
    """No pasar de N de peso por minuto, y frenar si la IP (compartida con los bots) va cargada."""

    def __init__(self, por_minuto: int, guardia_ip: int):
        self.por_minuto, self.guardia_ip = por_minuto, guardia_ip
        self.hist: deque = deque()
        self.pausa_hasta = 0.0
        self.usado_ip = 0

    async def tomar(self, peso: int):
        while True:
            ahora = time.monotonic()
            while self.hist and ahora - self.hist[0][0] > 60:
                self.hist.popleft()
            if ahora >= self.pausa_hasta and sum(p for _, p in self.hist) + peso <= self.por_minuto:
                self.hist.append((ahora, peso))
                return
            await asyncio.sleep(0.5)

    def usado_ultimo_minuto(self) -> int:
        ahora = time.monotonic()
        return sum(p for t, p in self.hist if ahora - t <= 60)

    def cabecera(self, usado: str | None):
        if usado and usado.isdigit():
            self.usado_ip = int(usado)
            if self.usado_ip >= self.guardia_ip:
                espera = 61 - (time.time() % 60)
                self.pausa_hasta = time.monotonic() + espera
                log.warning("Peso de la IP en %s (>= %s). Pausa REST de %.0fs para no afectar a los bots.",
                            self.usado_ip, self.guardia_ip, espera)


class Mercado:
    def __init__(self, cfg, db):
        self.cfg, self.db = cfg, db
        self.motor = None
        self.en_vivo = False
        self.simbolos: dict[str, SymbolInfo] = {}
        self._br: dict[str, list[calc.Bracket]] = {}
        self._marca: dict[str, dict] = {}
        self._ultimo: dict[str, float] = {}
        self._libro: dict[str, tuple] = {}
        self._vela: dict[str, list] = {}
        self._interes: dict[str, float] = {}
        self._suscritos: set[str] = set()
        self._ws = None
        self._sesion: aiohttp.ClientSession | None = None
        self.lim = Limitador(cfg.peso_max_por_minuto, cfg.pausa_si_peso_ip_supera)
        self.procesado_hasta = db.get("procesado_hasta", 0)
        self._ult_guardado = 0.0
        self._recuperando = False
        self._sub_id = 1
        self.ws_conectado = False

    # ------------------------------------------------------------ consulta (lo usa el motor)
    def simbolo(self, s):
        return self.simbolos.get(s)

    def brackets(self, s):
        return self._br.get(s) or calc.DEFAULT_BRACKETS

    def marca(self, s):
        d = self._marca.get(s)
        return d["p"] if d else None

    def datos_marca(self, s) -> dict | None:
        return self._marca.get(s)

    def ultimo(self, s):
        return self._ultimo.get(s)

    def libro(self, s):
        d = self._libro.get(s)
        if not d or not self.en_vivo or time.time() - d[2] > 15:
            return None
        return d[0], d[1]

    def interesa(self, s: str, minutos: int = 15):
        self._interes[s] = time.time() + minutos * 60

    # ------------------------------------------------------------ REST
    async def rest(self, ruta: str, params: dict | None = None, peso: int = 1, base: str | None = None):
        await self.lim.tomar(peso)
        url = (base or self.cfg.rest) + ruta
        for intento in range(4):
            try:
                async with self._sesion.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    self.lim.cabecera(r.headers.get("X-MBX-USED-WEIGHT-1M"))
                    if r.status in (418, 429):
                        espera = int(r.headers.get("Retry-After", "60"))
                        log.warning("Binance pidió esperar %ss (HTTP %s).", espera, r.status)
                        self.lim.pausa_hasta = time.monotonic() + espera
                        await asyncio.sleep(espera)
                        continue
                    r.raise_for_status()
                    return await r.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("REST %s falló (%s), reintento %s", ruta, e, intento + 1)
                await asyncio.sleep(2 * (intento + 1))
        raise RuntimeError(f"No se pudo consultar {ruta}")

    async def asegurar(self, s: str):
        """Antes de operar una moneda: suscribirse y, si falta, traer bid/ask y último precio (peso 3)."""
        self.interesa(s)
        await self._sincronizar_suscripciones()
        if s not in self._libro or time.time() - self._libro[s][2] > 15:
            try:
                d = await self.rest("/fapi/v1/ticker/bookTicker", {"symbol": s}, peso=2)
                self._libro[s] = (float(d["bidPrice"]), float(d["askPrice"]), time.time())
            except Exception as e:
                log.warning("bookTicker %s: %s", s, e)
        if s not in self._ultimo:
            try:
                d = await self.rest("/fapi/v1/ticker/price", {"symbol": s}, peso=1)
                self._ultimo[s] = float(d["price"])
            except Exception as e:
                log.warning("ticker %s: %s", s, e)

    async def velas(self, s: str, intervalo: str, inicio: int | None = None, fin: int | None = None,
                    limite: int = 499, marca: bool = False) -> list:
        ruta = "/fapi/v1/markPriceKlines" if marca else "/fapi/v1/klines"
        p = {"symbol": s, "interval": intervalo, "limit": limite}
        if inicio:
            p["startTime"] = inicio
        if fin:
            p["endTime"] = fin
        peso = 1 if limite < 100 else 2 if limite < 500 else 5 if limite <= 1000 else 10
        return await self.rest(ruta, p, peso=peso)

    # ------------------------------------------------------------ info de monedas y brackets (1 vez al día)
    def _ruta(self, nombre) -> Path:
        return self.cfg.data_dir / nombre

    async def refrescar_info(self, forzar: bool = False):
        f = self._ruta("exchangeinfo.json")
        datos = None
        if f.exists() and not forzar and time.time() - f.stat().st_mtime < 86400:
            datos = json.loads(f.read_text())
        else:
            try:
                datos = await self.rest("/fapi/v1/exchangeInfo", peso=1)
                f.write_text(json.dumps(datos))
            except Exception as e:
                log.error("exchangeInfo falló: %s", e)
                if f.exists():
                    datos = json.loads(f.read_text())
        if datos:
            self._cargar_info(datos)
        await self._refrescar_brackets(forzar)

    def _cargar_info(self, datos: dict):
        nuevos = {}
        for s in datos.get("symbols", []):
            if s.get("contractType") != "PERPETUAL" or s.get("quoteAsset") != "USDT":
                continue
            si = SymbolInfo(s["symbol"], estado=s.get("status", "TRADING"))
            for f in s.get("filters", []):
                t = f.get("filterType")
                if t == "PRICE_FILTER":
                    si.tick, si.min_precio, si.max_precio = float(f["tickSize"]), float(f["minPrice"]), float(f["maxPrice"])
                elif t == "LOT_SIZE":
                    si.paso, si.min_cant, si.max_cant = float(f["stepSize"]), float(f["minQty"]), float(f["maxQty"])
                elif t == "MARKET_LOT_SIZE":
                    si.paso_mkt, si.min_cant_mkt, si.max_cant_mkt = float(f["stepSize"]), float(f["minQty"]), float(f["maxQty"])
                elif t == "MIN_NOTIONAL":
                    si.min_notional = float(f.get("notional", f.get("minNotional", 5)))
                elif t == "PERCENT_PRICE":
                    si.mult_arriba, si.mult_abajo = float(f["multiplierUp"]), float(f["multiplierDown"])
            nuevos[si.simbolo] = si
        if not nuevos:
            return
        # deslistadas: tenían posición u orden y ya no operan
        if self.motor:
            for s in self.motor.simbolos_activos():
                si = nuevos.get(s)
                if si is None or si.estado not in ("TRADING", "PENDING_TRADING"):
                    log.warning("%s se deslistó: cerrando posiciones al último precio.", s)
                    self.motor.deslistar(s)
        self.simbolos = nuevos
        log.info("Monedas USDT perpetuas: %d", sum(1 for x in nuevos.values() if x.estado == "TRADING"))

    async def _refrescar_brackets(self, forzar: bool):
        f = self._ruta("brackets.json")
        crudo = None
        if f.exists() and not forzar and time.time() - f.stat().st_mtime < 86400:
            crudo = json.loads(f.read_text())
        else:
            try:
                async with self._sesion.post(self.cfg.brackets_url, json={},
                                             timeout=aiohttp.ClientTimeout(total=30)) as r:
                    r.raise_for_status()
                    crudo = await r.json(content_type=None)
                if self._parsear_brackets(crudo):
                    f.write_text(json.dumps(crudo))
            except Exception as e:
                log.error("No se pudo bajar la tabla de brackets (%s). Uso la copia guardada o la genérica.", e)
                if f.exists():
                    crudo = json.loads(f.read_text())
        if crudo:
            br = self._parsear_brackets(crudo)
            if br:
                self._br = br
                log.info("Tabla de margen de mantenimiento: %d monedas", len(br))

    @staticmethod
    def _parsear_brackets(crudo) -> dict:
        """Acepta el formato público de la web de Binance o el de /fapi/v1/leverageBracket."""
        lista = crudo
        if isinstance(crudo, dict):
            d = crudo.get("data", crudo)
            lista = d.get("brackets", d) if isinstance(d, dict) else d
        out = {}
        if not isinstance(lista, list):
            return out
        for item in lista:
            try:
                s = item["symbol"]
                filas = item.get("riskBrackets") or item.get("brackets") or []
                brs = []
                for b in filas:
                    if "bracketNotionalFloor" in b:
                        brs.append(calc.Bracket(float(b["bracketNotionalFloor"]), float(b["bracketNotionalCap"]),
                                                float(b["bracketMaintenanceMarginRate"]),
                                                float(b.get("cumFastMaintenanceAmount", 0) or 0),
                                                int(b.get("maxOpenPosLeverage") or b.get("maxLeverage") or 1)))
                    else:
                        brs.append(calc.Bracket(float(b["notionalFloor"]), float(b["notionalCap"]),
                                                float(b["maintMarginRatio"]), float(b.get("cum", 0)),
                                                int(b["initialLeverage"])))
                if brs:
                    out[s] = sorted(brs, key=lambda x: x.floor)
            except (KeyError, TypeError, ValueError):
                continue
        return out

    # ------------------------------------------------------------ WebSocket
    def _deseados(self) -> set[str]:
        ahora = time.time()
        self._interes = {s: t for s, t in self._interes.items() if t > ahora}
        s = set(self._interes)
        if self.motor:
            s |= self.motor.simbolos_activos()
        return {x for x in s if x in self.simbolos}

    async def _enviar(self, metodo: str, streams: list[str]):
        if not streams or self._ws is None or self._ws.closed:
            return
        for i in range(0, len(streams), 50):
            self._sub_id += 1
            await self._ws.send_json({"method": metodo, "params": streams[i:i + 50], "id": self._sub_id})
            await asyncio.sleep(0.25)   # Binance: máx 10 mensajes por segundo

    async def _sincronizar_suscripciones(self):
        quiero = self._deseados()
        nuevos, viejos = quiero - self._suscritos, self._suscritos - quiero
        if nuevos:
            await self._enviar("SUBSCRIBE", [f"{s.lower()}@{x}" for s in nuevos for x in ("kline_1m", "depth5@500ms")])
        if viejos:
            await self._enviar("UNSUBSCRIBE", [f"{s.lower()}@{x}" for s in viejos for x in ("kline_1m", "depth5@500ms")])
            for s in viejos:
                self._vela.pop(s, None)
        self._suscritos = quiero

    async def correr_ws(self):
        espera = 1
        while True:
            try:
                url = f"{self.cfg.ws}/stream?streams=!markPrice@arr@1s"
                async with self._sesion.ws_connect(url, heartbeat=30, max_msg_size=0) as ws:
                    self._ws = ws
                    self._suscritos = set()
                    self.ws_conectado = True
                    log.info("WebSocket conectado.")
                    espera = 1
                    await self._ponerse_al_dia()
                    await self._sincronizar_suscripciones()
                    sync = asyncio.create_task(self._bucle_sync())
                    try:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                self._mensaje(msg.data)
                            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                                break
                    finally:
                        sync.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("WebSocket caído: %s", e)
            self.ws_conectado = False
            self.en_vivo = False
            log.info("Reconectando WebSocket en %ss…", espera)
            await asyncio.sleep(espera)
            espera = min(espera * 2, 60)

    async def _bucle_sync(self):
        while True:
            await asyncio.sleep(3)
            try:
                await self._sincronizar_suscripciones()
            except Exception as e:  # pragma: no cover
                log.warning("sync suscripciones: %s", e)

    def _mensaje(self, texto: str):
        try:
            m = json.loads(texto)
        except ValueError:
            return
        d = m.get("data")
        if d is None:
            return
        try:
            if isinstance(d, list):
                self._marcas(d)
            elif d.get("e") == "kline":
                self._kline(d)
            elif d.get("e") == "depthUpdate":
                b, a = d.get("b"), d.get("a")
                if b and a:
                    self._libro[d["s"]] = (float(b[0][0]), float(a[0][0]), time.time())
        except Exception:  # pragma: no cover
            log.exception("procesando mensaje")

    def _marcas(self, lista: list):
        activos = self.motor.simbolos_activos() if self.motor else set()
        ts_max = 0
        for x in lista:
            s = x["s"]
            ts = int(x["E"])
            ts_max = max(ts_max, ts)
            p = float(x["p"])
            r = float(x.get("r") or 0)
            T = int(x.get("T") or 0)
            prev = self._marca.get(s)
            self._marca[s] = {"p": p, "i": float(x.get("i") or 0), "r": r, "T": T, "ts": ts}
            if self._recuperando or not self.motor:
                continue
            if prev and prev["T"] and T != prev["T"] and prev["T"] <= ts:
                self.motor.aplicar_funding(s, prev["r"], prev["T"], p)
            if s in activos:
                self.motor.en_precio(s, "mark", p, ts)
        if not self._recuperando and ts_max:
            self.en_vivo = True
            self.procesado_hasta = ts_max
            if time.time() - self._ult_guardado > 15:
                self._ult_guardado = time.time()
                self.db.set("procesado_hasta", ts_max)

    def _kline(self, d: dict):
        s, k = d["s"], d["k"]
        t, o, h, l, c = int(k["t"]), float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"])
        ts = int(d["E"])
        v = self._vela.get(s)
        puntos = []
        if v is None or v[0] != t:
            v = [t, o, o, None]
            self._vela[s] = v
            puntos.append(o)
        if h > v[1]:
            v[1] = h
            puntos.append(h)
        if l < v[2]:
            v[2] = l
            puntos.append(l)
        if c != v[3]:
            v[3] = c
            puntos.append(c)
        self._ultimo[s] = c
        if self._recuperando or not self.motor:
            return
        for p in puntos:
            self._ultimo[s] = p
            self.motor.en_precio(s, "last", p, ts)
        self._ultimo[s] = c

    # ------------------------------------------------------------ ponerse al día tras una caída
    async def _ponerse_al_dia(self):
        if not self.motor or not self.procesado_hasta:
            return
        desde = self.procesado_hasta
        hasta = int(time.time() * 1000)
        if hasta - desde < 70_000:
            return
        desde = max(desde, hasta - 7 * 86400_000)
        simbolos = sorted(self.motor.simbolos_activos())
        if not simbolos:
            return
        log.warning("Estuve desconectado %.1f min. Revisando velas de 1m de %s…", (hasta - desde) / 60000, ", ".join(simbolos))
        self._recuperando = True
        self.en_vivo = False
        try:
            eventos = []
            for s in simbolos:
                for marca in (False, True):
                    ini = desde - desde % 60000
                    while ini < hasta:
                        filas = await self.velas(s, "1m", ini, hasta, 499, marca=marca)
                        if not filas:
                            break
                        for f in filas:
                            t, o, h, l, c = int(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[4])
                            ruta = [o, h, l, c] if abs(o - h) <= abs(o - l) else [o, l, h, c]
                            for i, p in enumerate(ruta):
                                ts = t + (0, 15000, 30000, 59000)[i]
                                if ts >= desde:
                                    eventos.append((ts, 1 if marca else 0, s, "mark" if marca else "last", p))
                        ini = int(filas[-1][0]) + 60000
                        if len(filas) < 499:
                            break
                try:
                    fr = await self.rest("/fapi/v1/fundingRate", {"symbol": s, "startTime": desde, "endTime": hasta, "limit": 100}, peso=1)
                    for f in fr:
                        eventos.append((int(f["fundingTime"]), 2, s, "funding", (float(f["fundingRate"]), float(f.get("markPrice") or 0))))
                except Exception as e:
                    log.warning("historial de funding %s: %s", s, e)
            eventos.sort()
            for ts, _, s, tipo, val in eventos:
                if tipo == "funding":
                    self.motor.aplicar_funding(s, val[0], ts, val[1] or None)
                    continue
                if tipo == "mark":
                    d = self._marca.setdefault(s, {"p": val, "i": 0, "r": 0, "T": 0, "ts": ts})
                    d["p"] = val
                else:
                    self._ultimo[s] = val
                self.motor.en_precio(s, tipo, val, ts)
            log.info("Al día: %d eventos de precio revisados.", len(eventos))
            self.procesado_hasta = hasta
            self.db.set("procesado_hasta", hasta)
        except Exception:
            log.exception("No se pudo completar la puesta al día")
        finally:
            self._recuperando = False

    # ------------------------------------------------------------ arranque
    async def iniciar(self):
        self._sesion = aiohttp.ClientSession(headers={"User-Agent": "broker-papel/1.0"})
        await self.refrescar_info()

    async def cerrar(self):
        if self._sesion:
            await self._sesion.close()
