"""API compatible con Binance Futures USDⓈ-M (mismas rutas y respuestas) para que tus bots prueben sin dinero real.

Los bots solo cambian la URL base (p. ej. http://127.0.0.1:8090) y usan la llave falsa de `broker crear-llave`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qsl

from aiohttp import web

from . import calc
from .modelos import BinanceError, Orden

log = logging.getLogger("api")


def _b(v) -> bool:
    return str(v).lower() == "true"


class ApiBinance:
    def __init__(self, cfg, motor, mercado, auth):
        self.cfg, self.motor, self.m, self.auth = cfg, motor, mercado, auth

    # ------------------------------------------------------------ utilidades
    @staticmethod
    def err(code: int, msg: str, status: int = 400):
        return web.json_response({"code": code, "msg": msg}, status=status)

    def f(self, simbolo: str, x, cual: str = "precio") -> str:
        if x is None:
            return "0"
        si = self.m.simbolo(simbolo)
        paso = (si.tick if cual == "precio" else si.paso) if si else None
        return calc.fmt_num(float(x), paso) if paso else repr(float(x))

    async def _params(self, req) -> tuple[dict, str, str]:
        crudo_q = req.query_string or ""
        crudo_b = ""
        if req.method in ("POST", "PUT", "DELETE") and req.can_read_body:
            crudo_b = (await req.read()).decode()
        p = dict(parse_qsl(crudo_q, keep_blank_values=True))
        p.update(dict(parse_qsl(crudo_b, keep_blank_values=True)))
        return p, crudo_q, crudo_b

    @staticmethod
    def _sin_firma(s: str) -> str:
        partes = [x for x in s.split("&") if x and not x.startswith("signature=")]
        return "&".join(partes)

    async def _auth(self, req, firmado=True):
        p, q, b = await self._params(req)
        llave = req.headers.get("X-MBX-APIKEY", "")
        fila = self.motor.db.one("SELECT * FROM llaves_api WHERE llave=?", (llave,)) if llave else None
        if not fila:
            raise web.HTTPUnauthorized(text=json.dumps({"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."}),
                                       content_type="application/json")
        if firmado:
            total = self._sin_firma(q) + self._sin_firma(b)
            firma = hmac.new(fila["secreto"].encode(), total.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(firma, p.get("signature", "")):
                raise web.HTTPBadRequest(text=json.dumps({"code": -1022, "msg": "Signature for this request is not valid."}),
                                         content_type="application/json")
            ts = int(p.get("timestamp", 0))
            ventana = int(p.get("recvWindow", 5000))
            ahora = int(time.time() * 1000)
            if ts > ahora + 1000 or ahora - ts > ventana:
                raise web.HTTPBadRequest(text=json.dumps({"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."}),
                                         content_type="application/json")
        return fila["cuenta"], p

    def ruta(self, f, firmado=True):
        async def h(req):
            try:
                cid, p = await self._auth(req, firmado)
                return web.json_response(await f(cid, p))
            except web.HTTPException as e:
                return e
            except BinanceError as e:
                return self.err(e.code, e.msg)
            except KeyError as e:
                return self.err(-1102, f"Mandatory parameter '{e.args[0]}' was not sent, was empty/null, or malformed.")
            except ValueError as e:
                return self.err(-1100, f"Illegal characters found in a parameter. ({e})")
        return h

    def publica(self, f):
        async def h(req):
            try:
                p = dict(req.query)
                return web.json_response(await f(p))
            except BinanceError as e:
                return self.err(e.code, e.msg)
            except KeyError as e:
                return self.err(-1102, f"Mandatory parameter '{e.args[0]}' was not sent, was empty/null, or malformed.")
        return h

    def orden_json(self, o: Orden) -> dict:
        s = o.simbolo
        tipo = o.tipo
        if o.tipo in ("STOP", "TAKE_PROFIT") and o.disparada:
            tipo = o.tipo
        return {
            "orderId": o.id, "symbol": s, "status": o.estado, "clientOrderId": o.cliente_id or f"autoclose-{o.id}",
            "price": self.f(s, o.precio), "avgPrice": self.f(s, o.precio_prom), "origQty": self.f(s, o.cantidad, "cant"),
            "executedQty": self.f(s, o.ejecutada, "cant"), "cumQuote": repr(round(o.ejecutada * o.precio_prom, 8)),
            "timeInForce": o.tif, "type": tipo, "origType": o.tipo, "reduceOnly": o.solo_reducir or o.cerrar_posicion,
            "closePosition": o.cerrar_posicion, "side": o.lado, "positionSide": o.lado_pos,
            "stopPrice": self.f(s, o.precio_stop), "workingType": o.disparo, "priceProtect": False,
            "activatePrice": self.f(s, o.activacion) if o.activacion else None,
            "priceRate": str(o.retroceso) if o.retroceso else None,
            "priceMatch": "NONE", "selfTradePreventionMode": "NONE", "goodTillDate": 0,
            "time": o.creada, "updateTime": o.actualizada,
        }

    def _buscar(self, cid: int, p: dict) -> Orden:
        if "orderId" in p:
            o = self.motor.ordenes.get(int(p["orderId"]))
            if not o:
                r = self.motor.db.one("SELECT datos FROM ordenes WHERE id=? AND cuenta=?", (int(p["orderId"]), cid))
                o = Orden(**json.loads(r["datos"])) if r else None
        elif "origClientOrderId" in p:
            o = next((x for x in self.motor.ordenes.values() if x.cliente_id == p["origClientOrderId"] and x.cuenta == cid), None)
            if not o:
                for r in self.motor.db.all("SELECT datos FROM ordenes WHERE cuenta=? AND simbolo=? ORDER BY id DESC LIMIT 1000",
                                           (cid, p.get("symbol", ""))):
                    d = json.loads(r["datos"])
                    if d.get("cliente_id") == p["origClientOrderId"]:
                        o = Orden(**d)
                        break
        else:
            raise BinanceError(-1102, "Param 'origClientOrderId' or 'orderId' must be sent, but both were empty/null!")
        if not o or o.cuenta != cid:
            raise BinanceError(-2013, "Order does not exist.")
        return o

    # ------------------------------------------------------------ públicas
    async def ping(self, p):
        return {}

    async def tiempo(self, p):
        return {"serverTime": int(time.time() * 1000)}

    async def exchange_info(self, p):
        f = self.cfg.data_dir / "exchangeinfo.json"
        return json.loads(f.read_text()) if f.exists() else {"symbols": []}

    async def premium(self, p):
        def uno(s):
            d = self.m.datos_marca(s) or {}
            return {"symbol": s, "markPrice": str(d.get("p", "0")), "indexPrice": str(d.get("i", "0")),
                    "estimatedSettlePrice": str(d.get("p", "0")), "lastFundingRate": str(d.get("r", "0")),
                    "interestRate": "0.00010000", "nextFundingTime": d.get("T", 0), "time": d.get("ts", 0)}
        if "symbol" in p:
            return uno(p["symbol"])
        return [uno(s) for s in self.m.simbolos if self.m.datos_marca(s)]

    async def precio(self, p):
        s = p["symbol"]
        await self.m.asegurar(s)
        return {"symbol": s, "price": str(self.m.ultimo(s)), "time": int(time.time() * 1000)}

    async def libro(self, p):
        s = p["symbol"]
        await self.m.asegurar(s)
        b = self.m.libro(s) or (self.m.ultimo(s), self.m.ultimo(s))
        return {"symbol": s, "bidPrice": str(b[0]), "bidQty": "1", "askPrice": str(b[1]), "askQty": "1",
                "time": int(time.time() * 1000)}

    async def velas(self, p):
        return await self.m.velas(p["symbol"], p.get("interval", "1m"), int(p["startTime"]) if "startTime" in p else None,
                                  int(p["endTime"]) if "endTime" in p else None, min(499, int(p.get("limit", 499))))

    # ------------------------------------------------------------ órdenes
    async def nueva_orden(self, cid, p):
        s = p["symbol"]
        await self.m.asegurar(s)
        num = lambda k: float(p[k]) if p.get(k) not in (None, "") else None
        c = self.motor.cuentas[cid]
        tif = p.get("timeInForce", "GTC")
        lado_pos = p.get("positionSide") or ("BOTH" if not c.cobertura else None)
        o = self.motor.colocar(cid, s, p["side"], p["type"], cantidad=num("quantity"), precio=num("price"),
                               precio_stop=num("stopPrice"), lado_pos=lado_pos, tif=tif,
                               solo_reducir=_b(p.get("reduceOnly", "false")), cerrar_posicion=_b(p.get("closePosition", "false")),
                               disparo=p.get("workingType", "CONTRACT_PRICE"), activacion=num("activationPrice"),
                               retroceso=num("callbackRate"), origen="api", cliente_id=p.get("newClientOrderId"))
        return self.orden_json(o)

    async def ver_orden(self, cid, p):
        return self.orden_json(self._buscar(cid, p))

    async def cancelar_orden(self, cid, p):
        o = self._buscar(cid, p)
        if o.id not in self.motor.ordenes:
            raise BinanceError(-2011, "Unknown order sent.")
        return self.orden_json(self.motor.cancelar(cid, o.id))

    async def cancelar_todas(self, cid, p):
        self.motor.cancelar_todas(cid, p["symbol"])
        return {"code": 200, "msg": "The operation of cancel all open order is done."}

    async def abiertas(self, cid, p):
        return [self.orden_json(o) for o in self.motor.ordenes_abiertas(cid, p.get("symbol"))]

    async def todas(self, cid, p):
        lim = min(1000, int(p.get("limit", 500)))
        filas = self.motor.db.all("SELECT datos FROM ordenes WHERE cuenta=? AND simbolo=? ORDER BY id DESC LIMIT ?",
                                  (cid, p["symbol"], lim))
        return [self.orden_json(Orden(**json.loads(r["datos"]))) for r in reversed(filas)]

    # ------------------------------------------------------------ cuenta
    def _posiciones(self, cid, simbolo=None) -> list[dict]:
        c = self.motor.cuentas[cid]
        out = []
        for p in self.motor.posiciones_abiertas(c):
            if simbolo and p.simbolo != simbolo:
                continue
            d = self.motor.detalle_pos(c, p)
            out.append({
                "symbol": p.simbolo, "positionAmt": self.f(p.simbolo, p.amt, "cant"), "entryPrice": repr(p.entrada),
                "breakEvenPrice": repr(p.entrada), "markPrice": repr(d["marca"]), "unRealizedProfit": repr(round(d["upnl"], 8)),
                "liquidationPrice": repr(d["liquidacion"] or 0), "leverage": str(d["apalancamiento"]),
                "maxNotionalValue": repr(calc.max_notional_at_leverage(self.m.brackets(p.simbolo), d["apalancamiento"])),
                "marginType": "cross" if p.tipo_margen == "CROSSED" else "isolated",
                "isolatedMargin": repr(round(p.margen_aislado + (d["upnl"] if p.tipo_margen == "ISOLATED" else 0), 8)),
                "isolatedWallet": repr(round(p.margen_aislado, 8)), "isAutoAddMargin": "false",
                "positionSide": p.lado, "notional": repr(round(p.amt * d["marca"], 8)),
                "initialMargin": repr(round(abs(p.amt) * d["marca"] / d["apalancamiento"], 8)),
                "maintMargin": repr(round(d["mm"], 8)), "updateTime": p.abierta,
            })
        return out

    async def riesgo(self, cid, p):
        return self._posiciones(cid, p.get("symbol"))

    async def balance(self, cid, p):
        c = self.motor.cuentas[cid]
        met = self.motor.metricas(c, con_detalle=False)
        return [{"accountAlias": c.nombre, "asset": "USDT", "balance": repr(round(c.saldo, 8)),
                 "crossWalletBalance": repr(round(met["billetera_cruzada"], 8)), "crossUnPnl": repr(round(met["upnl_cruzado"], 8)),
                 "availableBalance": repr(round(max(0, met["disponible"]), 8)),
                 "maxWithdrawAmount": repr(round(max(0, met["disponible"]), 8)), "marginAvailable": True,
                 "updateTime": int(time.time() * 1000)}]

    async def cuenta(self, cid, p):
        c = self.motor.cuentas[cid]
        met = self.motor.metricas(c, con_detalle=False)
        r = lambda x: repr(round(x, 8))
        pos = self._posiciones(cid)
        return {
            "feeTier": 0, "canTrade": True, "canDeposit": True, "canWithdraw": True, "updateTime": 0, "multiAssetsMargin": False,
            "totalInitialMargin": r(met["margen_inicial"] + met["margen_ordenes"]), "totalMaintMargin": r(met["mm_cruzado"]),
            "totalWalletBalance": r(c.saldo), "totalUnrealizedProfit": r(met["upnl"]), "totalMarginBalance": r(met["balance_margen"]),
            "totalPositionInitialMargin": r(met["margen_inicial"]), "totalOpenOrderInitialMargin": r(met["margen_ordenes"]),
            "totalCrossWalletBalance": r(met["billetera_cruzada"]), "totalCrossUnPnl": r(met["upnl_cruzado"]),
            "availableBalance": r(max(0, met["disponible"])), "maxWithdrawAmount": r(max(0, met["disponible"])),
            "assets": [{"asset": "USDT", "walletBalance": r(c.saldo), "unrealizedProfit": r(met["upnl"]),
                        "marginBalance": r(met["balance_margen"]), "maintMargin": r(met["mm_cruzado"]),
                        "initialMargin": r(met["margen_inicial"]), "positionInitialMargin": r(met["margen_inicial"]),
                        "openOrderInitialMargin": r(met["margen_ordenes"]), "crossWalletBalance": r(met["billetera_cruzada"]),
                        "crossUnPnl": r(met["upnl_cruzado"]), "availableBalance": r(max(0, met["disponible"])),
                        "maxWithdrawAmount": r(max(0, met["disponible"])), "marginAvailable": True, "updateTime": 0}],
            "positions": pos,
        }

    async def apalancamiento(self, cid, p):
        s, lev = p["symbol"], int(p["leverage"])
        self.motor.cambiar_apalancamiento(cid, s, lev)
        return {"leverage": lev, "maxNotionalValue": repr(calc.max_notional_at_leverage(self.m.brackets(s), lev)), "symbol": s}

    async def tipo_margen(self, cid, p):
        self.motor.cambiar_tipo_margen(cid, p["symbol"], p["marginType"])
        return {"code": 200, "msg": "success"}

    async def modo_dual(self, cid, p):
        self.motor.cambiar_modo_posicion(cid, _b(p["dualSidePosition"]))
        return {"code": 200, "msg": "success"}

    async def ver_modo_dual(self, cid, p):
        return {"dualSidePosition": self.motor.cuentas[cid].cobertura}

    async def margen_pos(self, cid, p):
        monto = float(p["amount"]) * (1 if str(p["type"]) == "1" else -1)
        self.motor.margen_aislado(cid, p["symbol"], monto, p.get("positionSide") if p.get("positionSide") not in (None, "BOTH") else None)
        return {"amount": float(p["amount"]), "code": 200, "msg": "Successfully modify position margin.", "type": int(p["type"])}

    async def trades(self, cid, p):
        lim = min(1000, int(p.get("limit", 500)))
        filas = self.motor.db.all("SELECT * FROM llenados WHERE cuenta=? AND simbolo=? ORDER BY tiempo DESC LIMIT ?",
                                  (cid, p["symbol"], lim))
        return [{"buyer": f["lado"] == "BUY", "commission": repr(f["comision"]), "commissionAsset": "USDT", "id": f["id"],
                 "maker": bool(f["maker"]), "orderId": f["orden"], "price": repr(f["precio"]), "qty": repr(f["cantidad"]),
                 "quoteQty": repr(round(f["precio"] * f["cantidad"], 8)), "realizedPnl": repr(f["pnl"]), "side": f["lado"],
                 "positionSide": f["lado_pos"], "symbol": f["simbolo"], "time": f["tiempo"]} for f in reversed(filas)]

    async def ingresos(self, cid, p):
        q, a = "SELECT * FROM transacciones WHERE cuenta=?", [cid]
        if p.get("symbol"):
            q += " AND simbolo=?"; a.append(p["symbol"])
        if p.get("incomeType"):
            q += " AND tipo=?"; a.append(p["incomeType"])
        if p.get("startTime"):
            q += " AND tiempo>=?"; a.append(int(p["startTime"]))
        if p.get("endTime"):
            q += " AND tiempo<=?"; a.append(int(p["endTime"]))
        q += " ORDER BY tiempo LIMIT ?"; a.append(min(1000, int(p.get("limit", 100))))
        return [{"symbol": f["simbolo"], "incomeType": f["tipo"], "income": repr(round(f["monto"], 8)), "asset": "USDT",
                 "info": f["info"], "time": f["tiempo"], "tranId": f["id"], "tradeId": ""} for f in self.motor.db.all(q, a)]

    async def brackets(self, cid, p):
        def uno(s):
            return {"symbol": s, "brackets": [
                {"bracket": i + 1, "initialLeverage": b.max_lev, "notionalCap": b.cap, "notionalFloor": b.floor,
                 "maintMarginRatio": b.mmr, "cum": b.cum} for i, b in enumerate(self.m.brackets(s))]}
        return [uno(p["symbol"])] if p.get("symbol") else [uno(s) for s in self.m.simbolos]

    async def comision(self, cid, p):
        c = self.motor.cuentas[cid]
        return {"symbol": p["symbol"], "makerCommissionRate": repr(c.maker), "takerCommissionRate": repr(c.taker)}

    async def listen_key(self, cid, p):
        return {"listenKey": "broker-papel-sin-websocket-de-usuario"}

    # ------------------------------------------------------------ app
    def app(self) -> web.Application:
        a = web.Application(client_max_size=64 * 1024)
        r, pub = self.ruta, self.publica
        a.router.add_get("/fapi/v1/ping", pub(self.ping))
        a.router.add_get("/fapi/v1/time", pub(self.tiempo))
        a.router.add_get("/fapi/v1/exchangeInfo", pub(self.exchange_info))
        a.router.add_get("/fapi/v1/premiumIndex", pub(self.premium))
        a.router.add_get("/fapi/v1/ticker/price", pub(self.precio))
        a.router.add_get("/fapi/v2/ticker/price", pub(self.precio))
        a.router.add_get("/fapi/v1/ticker/bookTicker", pub(self.libro))
        a.router.add_get("/fapi/v1/klines", pub(self.velas))
        a.router.add_post("/fapi/v1/order", r(self.nueva_orden))
        a.router.add_get("/fapi/v1/order", r(self.ver_orden))
        a.router.add_delete("/fapi/v1/order", r(self.cancelar_orden))
        a.router.add_delete("/fapi/v1/allOpenOrders", r(self.cancelar_todas))
        a.router.add_get("/fapi/v1/openOrders", r(self.abiertas))
        a.router.add_get("/fapi/v1/openOrder", r(self.ver_orden))
        a.router.add_get("/fapi/v1/allOrders", r(self.todas))
        for v in ("v2", "v3"):
            a.router.add_get(f"/fapi/{v}/positionRisk", r(self.riesgo))
            a.router.add_get(f"/fapi/{v}/balance", r(self.balance))
            a.router.add_get(f"/fapi/{v}/account", r(self.cuenta))
        a.router.add_post("/fapi/v1/leverage", r(self.apalancamiento))
        a.router.add_post("/fapi/v1/marginType", r(self.tipo_margen))
        a.router.add_post("/fapi/v1/positionSide/dual", r(self.modo_dual))
        a.router.add_get("/fapi/v1/positionSide/dual", r(self.ver_modo_dual))
        a.router.add_post("/fapi/v1/positionMargin", r(self.margen_pos))
        a.router.add_get("/fapi/v1/userTrades", r(self.trades))
        a.router.add_get("/fapi/v1/income", r(self.ingresos))
        a.router.add_get("/fapi/v1/leverageBracket", r(self.brackets))
        a.router.add_get("/fapi/v1/commissionRate", r(self.comision))
        a.router.add_post("/fapi/v1/listenKey", r(self.listen_key, firmado=False))
        a.router.add_put("/fapi/v1/listenKey", r(self.listen_key, firmado=False))
        return a

    async def iniciar(self):
        runner = web.AppRunner(self.app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, self.cfg.api_host, self.cfg.api_puerto).start()
        log.info("API compatible con Binance en http://%s:%s", self.cfg.api_host, self.cfg.api_puerto)
        return runner
