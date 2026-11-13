"""Motor de simulación: órdenes, posiciones, margen, liquidación y funding.

Todo es síncrono y corre en el mismo hilo (asyncio), así no hay carreras.
Cada cambio se guarda al momento en SQLite.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from collections import defaultdict
from typing import Callable, Optional

from . import calc
from .calc import Leg
from .modelos import BinanceError, Cuenta, Orden, Posicion, SymbolInfo

log = logging.getLogger("motor")

EPS = 1e-12
TIPOS = ("LIMIT", "MARKET", "STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET")
INGRESOS_PNL = ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE", "INSURANCE_CLEAR")


def ahora_ms() -> int:
    return int(time.time() * 1000)


def _sgn(x: float) -> int:
    return 1 if x > EPS else (-1 if x < -EPS else 0)


class Motor:
    def __init__(self, db, mercado, cfg, reloj: Callable[[], int] = ahora_ms):
        self.db, self.m, self.cfg, self.reloj = db, mercado, cfg, reloj
        self.cuentas: dict[int, Cuenta] = {}
        self.ordenes: dict[int, Orden] = {}               # solo abiertas
        self.por_simbolo: dict[str, set] = defaultdict(set)
        self.oyentes: list[Callable[[dict], None]] = []
        self._enfriar: dict[str, int] = {}
        self.cargar()

    # ------------------------------------------------------------------ carga / guardado
    def cargar(self):
        for r in self.db.all("SELECT * FROM cuentas ORDER BY id"):
            c = Cuenta(r["id"], r["nombre"], r["saldo"], r["maker"], r["taker"], bool(r["cobertura"]))
            self.cuentas[c.id] = c
        for r in self.db.all("SELECT * FROM ajustes_simbolo"):
            if r["cuenta"] in self.cuentas:
                self.cuentas[r["cuenta"]].ajustes[r["simbolo"]] = [r["apalancamiento"], r["tipo_margen"]]
        for r in self.db.all("SELECT * FROM posiciones"):
            if r["cuenta"] in self.cuentas:
                p = Posicion(**json.loads(r["datos"]))
                self.cuentas[r["cuenta"]].posiciones[(p.simbolo, p.lado)] = p
        for r in self.db.all("SELECT datos FROM ordenes WHERE estado IN ('NEW','PARTIALLY_FILLED')"):
            o = Orden(**json.loads(r["datos"]))
            self.ordenes[o.id] = o
            self.por_simbolo[o.simbolo].add(o.id)
        # crear cuentas de la configuración que falten
        nombres = {c.nombre.lower() for c in self.cuentas.values()}
        for cc in self.cfg.cuentas:
            if cc.nombre.lower() not in nombres:
                cur = self.db.ex("INSERT INTO cuentas(nombre,saldo,maker,taker,cobertura,creada) VALUES(?,?,?,?,0,?)",
                                 (cc.nombre, 0.0, cc.maker if cc.maker is not None else self.cfg.maker,
                                  cc.taker if cc.taker is not None else self.cfg.taker, self.reloj()))
                c = Cuenta(cur.lastrowid, cc.nombre, 0.0,
                           cc.maker if cc.maker is not None else self.cfg.maker,
                           cc.taker if cc.taker is not None else self.cfg.taker)
                self.cuentas[c.id] = c
                if self.cfg.saldo_inicial > 0:
                    self.transferir(c.id, self.cfg.saldo_inicial, "Saldo inicial")

    def _guardar_cuenta(self, c: Cuenta):
        self.db.ex("UPDATE cuentas SET saldo=?, maker=?, taker=?, cobertura=? WHERE id=?",
                   (c.saldo, c.maker, c.taker, int(c.cobertura), c.id))

    def _guardar_pos(self, cid: int, p: Posicion):
        if abs(p.amt) <= EPS:
            self.db.ex("DELETE FROM posiciones WHERE cuenta=? AND simbolo=? AND lado=?", (cid, p.simbolo, p.lado))
        else:
            self.db.ex("INSERT OR REPLACE INTO posiciones VALUES(?,?,?,?)",
                       (cid, p.simbolo, p.lado, json.dumps(p.dict())))

    def _guardar_orden(self, o: Orden):
        o.actualizada = self.reloj() if not o.actualizada else o.actualizada
        self.db.ex("INSERT OR REPLACE INTO ordenes(id,cuenta,simbolo,estado,creada,actualizada,datos) VALUES(?,?,?,?,?,?,?)",
                   (o.id, o.cuenta, o.simbolo, o.estado, o.creada, o.actualizada, json.dumps(o.dict())))

    def _tx(self, cid, tipo, monto, simbolo="", info="", ts=None):
        self.db.ex("INSERT INTO transacciones(cuenta,tipo,monto,simbolo,info,tiempo) VALUES(?,?,?,?,?,?)",
                   (cid, tipo, monto, simbolo, info, ts or self.reloj()))

    def emitir(self, tipo: str, cuenta: int, **datos):
        ev = {"tipo": tipo, "cuenta": cuenta, "nombre_cuenta": self.cuentas[cuenta].nombre if cuenta in self.cuentas else "", **datos}
        for f in self.oyentes:
            try:
                f(ev)
            except Exception:  # pragma: no cover
                log.exception("oyente falló")

    # ------------------------------------------------------------------ utilidades
    def cuenta(self, ref) -> Cuenta:
        if isinstance(ref, int) and ref in self.cuentas:
            return self.cuentas[ref]
        s = str(ref).strip().lower()
        for c in self.cuentas.values():
            if c.nombre.lower() == s:
                return c
        if s.isdigit():
            lista = sorted(self.cuentas.values(), key=lambda c: c.id)
            i = int(s) - 1
            if 0 <= i < len(lista):
                return lista[i]
        raise BinanceError(-1000, "Account not found.", f"No existe la cuenta '{ref}'.")

    def info(self, simbolo: str) -> SymbolInfo:
        si = self.m.simbolo(simbolo)
        if not si:
            raise BinanceError(-1121, "Invalid symbol.", f"La moneda {simbolo} no existe en Binance Futures USDT.")
        return si

    def ajuste(self, c: Cuenta, simbolo: str):
        if simbolo not in c.ajustes:
            maxlev = calc.max_leverage(self.m.brackets(simbolo))
            c.ajustes[simbolo] = [min(self.cfg.apalancamiento_inicial, maxlev), "CROSSED"]
        return c.ajustes[simbolo]

    def _guardar_ajuste(self, c: Cuenta, simbolo: str):
        lev, tm = c.ajustes[simbolo]
        self.db.ex("INSERT OR REPLACE INTO ajustes_simbolo VALUES(?,?,?,?)", (c.id, simbolo, lev, tm))

    def pos(self, c: Cuenta, simbolo: str, lado: str) -> Posicion:
        k = (simbolo, lado)
        if k not in c.posiciones:
            c.posiciones[k] = Posicion(simbolo, lado)
        return c.posiciones[k]

    def posiciones_abiertas(self, c: Cuenta) -> list[Posicion]:
        return [p for p in c.posiciones.values() if abs(p.amt) > EPS]

    def marca(self, simbolo: str, defecto: float | None = None) -> float | None:
        return self.m.marca(simbolo) or self.m.ultimo(simbolo) or defecto

    def ultimo(self, simbolo: str) -> float | None:
        return self.m.ultimo(simbolo) or self.m.marca(simbolo)

    def simbolos_activos(self) -> set[str]:
        s = {o.simbolo for o in self.ordenes.values()}
        for c in self.cuentas.values():
            s |= {p.simbolo for p in self.posiciones_abiertas(c)}
        return s

    # ------------------------------------------------------------------ métricas
    def margen_ordenes(self, c: Cuenta) -> float:
        tot = 0.0
        for o in self.ordenes.values():
            if o.cuenta != c.id or o.solo_reducir or o.cerrar_posicion:
                continue
            if not (o.tipo == "LIMIT" or (o.tipo in ("STOP", "TAKE_PROFIT") and o.disparada)):
                continue
            inc = self._cantidad_que_aumenta(c, o.simbolo, o.lado_pos, o.lado, o.restante)
            if inc > 0:
                tot += inc * o.precio / self.ajuste(c, o.simbolo)[0]
        return tot

    def detalle_pos(self, c: Cuenta, p: Posicion, met: dict | None = None) -> dict:
        lev, _ = self.ajuste(c, p.simbolo)
        mk = self.marca(p.simbolo, p.entrada)
        br = self.m.brackets(p.simbolo)
        notional = abs(p.amt) * mk
        upnl = calc.unrealized_pnl(p.amt, p.entrada, mk)
        mm = calc.maint_margin(br, notional)
        if p.tipo_margen == "ISOLATED":
            margen = p.margen_aislado
            liq = calc.liquidation_price(p.margen_aislado, 0, 0, [Leg(_sgn(p.amt), abs(p.amt), p.entrada)], br, mk)
            ratio = mm / (margen + upnl) if margen + upnl > 0 else 1.0
        else:
            margen = notional / lev
            met = met or self.metricas(c, con_detalle=False)
            otros_mm = otros_upnl = 0.0
            legs = []
            for q in self.posiciones_abiertas(c):
                if q.tipo_margen != "CROSSED":
                    continue
                if q.simbolo == p.simbolo:
                    legs.append(Leg(_sgn(q.amt), abs(q.amt), q.entrada))
                else:
                    mq = self.marca(q.simbolo, q.entrada)
                    otros_mm += calc.maint_margin(self.m.brackets(q.simbolo), abs(q.amt) * mq)
                    otros_upnl += calc.unrealized_pnl(q.amt, q.entrada, mq)
            liq = calc.liquidation_price(met["billetera_cruzada"], otros_mm, otros_upnl, legs, br, mk)
            ratio = met["ratio_margen"]
        dist = abs(mk - liq) / mk * 100 if liq and mk else None
        return {
            "simbolo": p.simbolo, "lado": p.lado, "amt": p.amt, "entrada": p.entrada, "marca": mk,
            "notional": notional, "upnl": upnl, "roe": calc.roe(p.amt, p.entrada, mk, lev),
            "apalancamiento": lev, "tipo_margen": p.tipo_margen, "margen": margen, "mm": mm,
            "liquidacion": liq, "distancia_liq": dist, "ratio_margen": ratio, "abierta": p.abierta,
        }

    def metricas(self, c: Cuenta, con_detalle: bool = True) -> dict:
        iso = 0.0
        upnl_c = upnl_t = im_c = mm_c = 0.0
        for p in self.posiciones_abiertas(c):
            lev, _ = self.ajuste(c, p.simbolo)
            mk = self.marca(p.simbolo, p.entrada)
            u = calc.unrealized_pnl(p.amt, p.entrada, mk)
            upnl_t += u
            if p.tipo_margen == "ISOLATED":
                iso += p.margen_aislado
            else:
                upnl_c += u
                im_c += abs(p.amt) * mk / lev
                mm_c += calc.maint_margin(self.m.brackets(p.simbolo), abs(p.amt) * mk)
        billetera_c = c.saldo - iso
        mo = self.margen_ordenes(c)
        bal_c = billetera_c + upnl_c
        met = {
            "saldo": c.saldo, "billetera_cruzada": billetera_c, "margen_aislado": iso,
            "upnl": upnl_t, "upnl_cruzado": upnl_c, "margen_inicial": im_c + iso, "margen_ordenes": mo,
            "mm_cruzado": mm_c, "balance_margen": c.saldo + upnl_t,
            "disponible": bal_c - im_c - mo,
            "ratio_margen": (mm_c / bal_c) if bal_c > 0 else (1.0 if mm_c > 0 else 0.0),
        }
        if con_detalle:
            met["posiciones"] = [self.detalle_pos(c, p, met) for p in self.posiciones_abiertas(c)]
        return met

    def pnl_desde(self, c: Cuenta, desde_ms: int) -> float:
        r = self.db.one(
            f"SELECT COALESCE(SUM(monto),0) s FROM transacciones WHERE cuenta=? AND tiempo>=? AND tipo IN ({','.join('?'*len(INGRESOS_PNL))})",
            (c.id, desde_ms, *INGRESOS_PNL))
        return r["s"]

    # ------------------------------------------------------------------ helpers de posición
    def _lado_pos_ok(self, c: Cuenta, lado_pos: str):
        if c.cobertura and lado_pos not in ("LONG", "SHORT"):
            raise BinanceError(-4061, "Order's position side does not match user's setting.",
                               "La cuenta está en modo cobertura: indica si es LONG o SHORT.")
        if not c.cobertura and lado_pos != "BOTH":
            raise BinanceError(-4061, "Order's position side does not match user's setting.",
                               "La cuenta está en modo unidireccional: no uses LONG/SHORT como lado de posición.")

    def _cantidad_que_aumenta(self, c: Cuenta, simbolo: str, lado_pos: str, lado: str, qty: float) -> float:
        p = c.posiciones.get((simbolo, lado_pos))
        amt = p.amt if p else 0.0
        if lado_pos == "LONG":
            return qty if lado == "BUY" else 0.0
        if lado_pos == "SHORT":
            return qty if lado == "SELL" else 0.0
        s = 1 if lado == "BUY" else -1
        if _sgn(amt) == 0 or _sgn(amt) == s:
            return qty
        return max(0.0, qty - abs(amt))

    def _cantidad_cerrable(self, c: Cuenta, simbolo: str, lado_pos: str, lado: str) -> float:
        p = c.posiciones.get((simbolo, lado_pos))
        amt = p.amt if p else 0.0
        if lado_pos == "LONG":
            return amt if lado == "SELL" else 0.0
        if lado_pos == "SHORT":
            return -amt if lado == "BUY" else 0.0
        s = 1 if lado == "BUY" else -1
        return abs(amt) if _sgn(amt) == -s else 0.0

    def _es_reductora(self, o: Orden) -> bool:
        if o.solo_reducir or o.cerrar_posicion:
            return True
        return (o.lado_pos == "LONG" and o.lado == "SELL") or (o.lado_pos == "SHORT" and o.lado == "BUY")

    def _revisar_margen(self, c: Cuenta, simbolo: str, lado_pos: str, lado: str, qty: float, precio: float,
                        con_comision: bool = True):
        inc = self._cantidad_que_aumenta(c, simbolo, lado_pos, lado, qty)
        if inc <= 0:
            return
        lev, _ = self.ajuste(c, simbolo)
        # límite de posición según apalancamiento (brackets reales)
        p = c.posiciones.get((simbolo, lado_pos))
        amt = p.amt if p else 0.0
        if lado_pos == "BOTH":
            proy = abs(amt + (qty if lado == "BUY" else -qty))
        else:
            proy = abs(amt) + inc
        maxn = calc.max_notional_at_leverage(self.m.brackets(simbolo), lev)
        if proy * precio > maxn:
            raise BinanceError(-2027, "Exceeded the maximum allowable position at current leverage.",
                               f"Con {lev}x la posición máxima es {maxn:,.0f} USDT. Baja el apalancamiento o el tamaño.")
        req = inc * precio / lev + (qty * precio * c.taker if con_comision else 0.0)
        disp = self.metricas(c, con_detalle=False)["disponible"]
        if req > disp + 1e-9:
            raise BinanceError(-2019, "Margin is insufficient.",
                               f"Margen insuficiente: se necesitan {req:,.2f} USDT y hay {max(disp, 0):,.2f} disponibles.")

    # ------------------------------------------------------------------ precios de llenado
    def precio_mercado(self, simbolo: str, lado: str, qty: float, ref: float | None = None) -> float:
        si = self.info(simbolo)
        base = None
        if ref is None:
            libro = self.m.libro(simbolo)
            if libro:
                base = libro[1] if lado == "BUY" else libro[0]
        if base is None:
            base = ref or self.ultimo(simbolo)
        if not base:
            raise BinanceError(-1001, "Internal error; unable to process your request. Please try again.",
                               f"Todavía no hay precio de {simbolo}. Intenta en unos segundos.")
        notional = qty * base
        desl = self.cfg.deslizamiento_base + self.cfg.deslizamiento_por_50k * notional / 50_000
        if lado == "BUY":
            return calc.round_step(base * (1 + desl), si.tick, "up")
        return max(si.tick, calc.round_step(base * (1 - desl), si.tick, "down"))

    def _precio_disparo(self, o: Orden) -> float | None:
        return self.marca(o.simbolo) if o.disparo == "MARK_PRICE" else self.ultimo(o.simbolo)

    @staticmethod
    def _toca_stop(o: Orden, precio: float) -> bool:
        if o.tipo in ("STOP", "STOP_MARKET"):
            return precio >= o.precio_stop if o.lado == "BUY" else precio <= o.precio_stop
        if o.tipo in ("TAKE_PROFIT", "TAKE_PROFIT_MARKET"):
            return precio <= o.precio_stop if o.lado == "BUY" else precio >= o.precio_stop
        return False

    # ------------------------------------------------------------------ colocar órdenes
    def colocar(self, cuenta, simbolo: str, lado: str, tipo: str, cantidad: float | None = None,
                precio: float | None = None, precio_stop: float | None = None, lado_pos: str | None = None,
                tif: str = "GTC", solo_reducir: bool = False, cerrar_posicion: bool = False,
                disparo: str = "CONTRACT_PRICE", activacion: float | None = None, retroceso: float | None = None,
                tp: float | None = None, sl: float | None = None, tpsl_disparo: str = "MARK_PRICE",
                origen: str = "manual", ts: int | None = None, post_only: bool = False,
                cliente_id: str | None = None) -> Orden:
        c = self.cuenta(cuenta)
        simbolo = simbolo.upper().strip()
        if not simbolo.endswith("USDT"):
            simbolo += "USDT"
        si = self.info(simbolo)
        if si.estado != "TRADING":
            raise BinanceError(-4140, "Invalid symbol status for opening position.", f"{simbolo} no está operando ahora.")
        lado, tipo, tif = lado.upper(), tipo.upper(), tif.upper()
        if post_only:
            tif = "GTX"
        if lado not in ("BUY", "SELL"):
            raise BinanceError(-1102, "Mandatory parameter 'side' was not sent, was empty/null, or malformed.", "Lado inválido (BUY/SELL).")
        if tipo not in TIPOS:
            raise BinanceError(-1116, "Invalid orderType.", f"Tipo de orden inválido: {tipo}.")
        if tif not in ("GTC", "IOC", "FOK", "GTX"):
            raise BinanceError(-1115, "Invalid timeInForce.", "Time in force inválido (GTC, IOC, FOK o post only).")
        lado_pos = (lado_pos or ("BOTH" if not c.cobertura else None) or "").upper()
        if not lado_pos:
            lado_pos = "LONG" if (lado == "BUY") != solo_reducir else "SHORT"
        self._lado_pos_ok(c, lado_pos)
        if c.cobertura and solo_reducir:
            raise BinanceError(-1106, "Parameter 'reduceonly' sent when not required.",
                               "En modo cobertura no se usa 'solo reducir': cierra con el lado contrario de esa posición.")
        if cerrar_posicion and tipo not in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
            raise BinanceError(-1106, "Parameter 'closePosition' sent when not required.",
                               "'Cerrar posición' solo sirve en Stop Market y Take Profit Market.")

        ts = ts or self.reloj()
        o = Orden(cuenta=c.id, simbolo=simbolo, lado=lado, lado_pos=lado_pos, tipo=tipo, tif=tif,
                  solo_reducir=solo_reducir, cerrar_posicion=cerrar_posicion, disparo=disparo.upper(),
                  tp=tp, sl=sl, tpsl_disparo=tpsl_disparo.upper(), origen=origen, creada=ts, actualizada=ts,
                  cliente_id=cliente_id or ("bp_" + secrets.token_hex(10)))

        # ---- cantidad
        if cerrar_posicion:
            o.cantidad = 0.0
        else:
            if cantidad is None or cantidad <= 0:
                raise BinanceError(-4003, "Quantity less than or equal to zero.", "La cantidad debe ser mayor que cero.")
            paso, mn, mx = (si.paso_mkt, si.min_cant_mkt, si.max_cant_mkt) if tipo in ("MARKET", "STOP_MARKET", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET") else (si.paso, si.min_cant, si.max_cant)
            if calc.too_many_decimals(cantidad, paso):
                raise BinanceError(-1111, "Precision is over the maximum defined for this asset.",
                                   f"Demasiados decimales en la cantidad. El paso de {simbolo} es {calc.fmt_num(paso, paso)}.")
            if not calc.is_multiple(cantidad, paso):
                raise BinanceError(-4023, "Quantity not increased by step size.", f"La cantidad debe ir en pasos de {calc.fmt_num(paso, paso)}.")
            if cantidad < mn:
                raise BinanceError(-4003, "Quantity less than or equal to zero.", f"Cantidad mínima de {simbolo}: {calc.fmt_num(mn, paso)}.")
            if cantidad > mx:
                raise BinanceError(-4005, "Quantity greater than max quantity.", f"Cantidad máxima de {simbolo}: {calc.fmt_num(mx, paso)}.")
            o.cantidad = cantidad

        # ---- precios
        mk = self.marca(simbolo)
        if tipo in ("LIMIT", "STOP", "TAKE_PROFIT"):
            if not precio or precio <= 0:
                raise BinanceError(-1102, "Mandatory parameter 'price' was not sent, was empty/null, or malformed.", "Falta el precio.")
            self._validar_precio(si, precio, "precio")
            if mk:
                if lado == "BUY" and precio > mk * si.mult_arriba:
                    lim = calc.round_step(mk * si.mult_arriba, si.tick)
                    raise BinanceError(-4016, f"Limit price can't be higher than {lim}.", f"El precio límite no puede ser mayor a {lim}.")
                if lado == "SELL" and precio < mk * si.mult_abajo:
                    lim = calc.round_step(mk * si.mult_abajo, si.tick, "up")
                    raise BinanceError(-4024, f"Limit price can't be lower than {lim}.", f"El precio límite no puede ser menor a {lim}.")
            o.precio = precio
        if tipo in ("STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"):
            if not precio_stop or precio_stop <= 0:
                raise BinanceError(-1102, "Mandatory parameter 'stopPrice' was not sent, was empty/null, or malformed.", "Falta el precio de disparo (stop).")
            self._validar_precio(si, precio_stop, "precio de disparo")
            o.precio_stop = precio_stop
            ref = self._precio_disparo(o)
            if ref and self._toca_stop(o, ref):
                raise BinanceError(-2021, "Order would immediately trigger.",
                                   f"La orden se dispararía de inmediato (precio actual {calc.fmt_num(ref, si.tick)}).")
        if tipo == "TRAILING_STOP_MARKET":
            if retroceso is None or not (0.1 <= retroceso <= 10):
                raise BinanceError(-2007, "Invalid callBackRate.", "El % de retroceso debe estar entre 0.1 y 10.")
            o.retroceso = retroceso
            if activacion:
                self._validar_precio(si, activacion, "precio de activación")
                o.activacion = activacion
            else:
                ref = self._precio_disparo(o)
                o.activado, o.extremo = True, ref

        # ---- notional mínimo
        ref_precio = o.precio or o.precio_stop or o.activacion or mk or 0
        if not (solo_reducir or cerrar_posicion or (c.cobertura and self._es_reductora(o))) and ref_precio:
            if o.cantidad * ref_precio < si.min_notional:
                raise BinanceError(-4164, f"Order's notional must be no smaller than {si.min_notional:g} (unless you choose reduce only).",
                                   f"El tamaño mínimo es {si.min_notional:g} USDT (ahora {o.cantidad * ref_precio:,.2f}).")

        # ---- solo reducir
        if self._es_reductora(o) and not cerrar_posicion and not o.es_condicional:
            if self._cantidad_cerrable(c, simbolo, lado_pos, lado) <= EPS:
                raise BinanceError(-2022, "ReduceOnly Order is rejected.", "No hay posición que reducir en ese sentido.")

        # ---- TP/SL adjuntos: deben estar del lado correcto
        if tp or sl:
            entrada = o.precio or mk
            largo = lado == "BUY"
            if tp and entrada and ((largo and tp <= entrada) or (not largo and tp >= entrada)):
                raise BinanceError(-2021, "Order would immediately trigger.", "El Take Profit está del lado equivocado del precio de entrada.")
            if sl and entrada and ((largo and sl >= entrada) or (not largo and sl <= entrada)):
                raise BinanceError(-2021, "Order would immediately trigger.", "El Stop Loss está del lado equivocado del precio de entrada.")
            for v in (tp, sl):
                if v:
                    self._validar_precio(si, v, "precio TP/SL")

        # ---- margen (market y límites que se ejecutan o quedan puestos)
        if tipo == "MARKET":
            self._revisar_margen(c, simbolo, lado_pos, lado, o.cantidad, self.precio_mercado(simbolo, lado, o.cantidad))
        elif tipo == "LIMIT":
            self._revisar_margen(c, simbolo, lado_pos, lado, o.cantidad, o.precio)

        # ---- guardar y ejecutar
        cur = self.db.ex("INSERT INTO ordenes(cuenta,simbolo,estado,creada,actualizada,datos) VALUES(?,?,?,?,?,?)",
                         (c.id, simbolo, "NEW", ts, ts, "{}"))
        o.id = cur.lastrowid
        self._guardar_orden(o)
        self.ordenes[o.id] = o
        self.por_simbolo[simbolo].add(o.id)

        if tipo == "MARKET":
            self._ejecutar_mercado(o, ts)
        elif tipo == "LIMIT":
            self._limite_entrante(o, ts)
        return o

    def _validar_precio(self, si: SymbolInfo, p: float, que: str):
        if calc.too_many_decimals(p, si.tick):
            raise BinanceError(-1111, "Precision is over the maximum defined for this asset.",
                               f"Demasiados decimales en el {que}. El tick de {si.simbolo} es {calc.fmt_num(si.tick, si.tick)}.")
        if not calc.is_multiple(p, si.tick):
            raise BinanceError(-4014, "Price not increased by tick size.", f"El {que} debe ir en pasos de {calc.fmt_num(si.tick, si.tick)}.")
        if si.min_precio and p < si.min_precio:
            raise BinanceError(-4013, "Price less than min price.", f"El {que} es menor al mínimo permitido.")
        if si.max_precio and p > si.max_precio:
            raise BinanceError(-4015, "Price greater than max price.", f"El {que} es mayor al máximo permitido.")

    # ------------------------------------------------------------------ ejecución
    def _cerrar_orden(self, o: Orden, estado: str, motivo: str = "", ts: int | None = None):
        o.estado, o.motivo = estado, motivo or o.motivo
        o.actualizada = ts or self.reloj()
        self._guardar_orden(o)
        self.ordenes.pop(o.id, None)
        self.por_simbolo[o.simbolo].discard(o.id)

    def _ejecutar_mercado(self, o: Orden, ts: int, ref: float | None = None):
        c = self.cuentas[o.cuenta]
        qty = o.restante if not o.cerrar_posicion else self._cantidad_cerrable(c, o.simbolo, o.lado_pos, o.lado)
        if qty <= EPS:
            self._cerrar_orden(o, "EXPIRED", "No había posición que cerrar", ts)
            self.emitir("orden_cancelada", c.id, orden=o.dict())
            return
        try:
            precio = self.precio_mercado(o.simbolo, o.lado, qty, ref)
            if o.origen != "liquidacion" and o.es_condicional:
                self._revisar_margen(c, o.simbolo, o.lado_pos, o.lado, qty, precio)
        except BinanceError as e:
            self._cerrar_orden(o, "EXPIRED", e.es, ts)
            self.emitir("orden_rechazada", c.id, orden=o.dict(), error=str(e))
            if o.tipo == "MARKET" and not o.es_condicional and o.origen not in ("liquidacion",):
                raise
            return
        self._llenar(o, qty, precio, maker=False, ts=ts)

    def _limite_entrante(self, o: Orden, ts: int):
        """Límite nueva (o stop-límite recién disparado): ¿cruza el libro?"""
        c = self.cuentas[o.cuenta]
        libro = self.m.libro(o.simbolo)
        ref = (libro[1] if o.lado == "BUY" else libro[0]) if libro else self.ultimo(o.simbolo)
        cruza = ref is not None and ((o.lado == "BUY" and o.precio >= ref) or (o.lado == "SELL" and o.precio <= ref))
        if o.tif == "GTX":
            if cruza:
                self._cerrar_orden(o, "EXPIRED", "Post only: se habría ejecutado como taker", ts)
                raise BinanceError(-5022, "Due to the order could not be executed as maker, the Post Only order will be rejected.",
                                   "Post only rechazada: tu precio se habría llenado al instante (taker).")
            return
        if not cruza:
            if o.tif == "IOC":
                self._cerrar_orden(o, "EXPIRED", "IOC sin llenado", ts)
            elif o.tif == "FOK":
                self._cerrar_orden(o, "EXPIRED", "FOK sin llenado", ts)
                raise BinanceError(-5021, "Due to the order could not be filled immediately, the FOK order has been rejected.",
                                   "FOK rechazada: no se podía llenar completa al instante.")
            return
        qty = o.restante
        try:
            px = self.precio_mercado(o.simbolo, o.lado, qty)
        except BinanceError:
            px = o.precio
        pasa = (o.lado == "BUY" and px <= o.precio) or (o.lado == "SELL" and px >= o.precio)
        if not pasa:
            if o.tif == "FOK":
                self._cerrar_orden(o, "EXPIRED", "FOK sin llenado", ts)
                raise BinanceError(-5021, "Due to the order could not be filled immediately, the FOK order has been rejected.",
                                   "FOK rechazada: con el deslizamiento no alcanzaba tu precio.")
            px = o.precio
        self._llenar(o, qty, px, maker=False, ts=ts)

    def _llenar(self, o: Orden, qty: float, precio: float, maker: bool, ts: int):
        c = self.cuentas[o.cuenta]
        si = self.info(o.simbolo)
        lev, tipo_margen = self.ajuste(c, o.simbolo)
        p = self.pos(c, o.simbolo, o.lado_pos)

        if self._es_reductora(o):
            cerrable = self._cantidad_cerrable(c, o.simbolo, o.lado_pos, o.lado)
            if cerrable <= EPS:
                self._cerrar_orden(o, "EXPIRED" if o.solo_reducir or o.cerrar_posicion else "CANCELED",
                                   "Solo reducir: no había posición", ts)
                self.emitir("orden_cancelada", c.id, orden=o.dict())
                return
            qty = min(qty, cerrable)

        s = 1 if o.lado == "BUY" else -1
        viejo = p.amt
        cerrada = 0.0
        realizado = 0.0
        if _sgn(viejo) == 0:
            p.tipo_margen, p.abierta = tipo_margen, ts
            p.pnl = p.comisiones = p.funding = p.cant_max = p.valor_salida = p.cant_salida = 0.0
            p.margen_aislado = 0.0
            p.ultimo_funding = ts
        if _sgn(viejo) == 0 or _sgn(viejo) == s:
            nuevo = viejo + s * qty
            p.entrada = (abs(viejo) * p.entrada + qty * precio) / abs(nuevo)
            aumento = qty
        else:
            cerrada = min(qty, abs(viejo))
            realizado = cerrada * (precio - p.entrada) * _sgn(viejo)
            if p.tipo_margen == "ISOLATED":
                p.margen_aislado -= p.margen_aislado * cerrada / abs(viejo)
            nuevo = viejo + s * qty
            aumento = qty - cerrada
        nuevo = calc.round_step(nuevo, si.paso, "near") if abs(nuevo) > EPS else 0.0
        comision = calc.fee(qty, precio, c.maker if maker else c.taker)

        c.saldo += realizado - comision
        p.pnl += realizado
        p.comisiones += comision
        p.valor_salida += cerrada * precio
        p.cant_salida += cerrada

        if realizado:
            self._tx(c.id, "REALIZED_PNL", realizado, o.simbolo, f"orden {o.id}", ts)
        self._tx(c.id, "COMMISSION", -comision, o.simbolo, f"orden {o.id}", ts)
        self.db.ex("INSERT INTO llenados(orden,cuenta,simbolo,lado,lado_pos,cantidad,precio,comision,pnl,maker,tiempo) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (o.id, c.id, o.simbolo, o.lado, o.lado_pos, qty, precio, comision, realizado, int(maker), ts))

        # ¿se cerró (o volteó) la posición?
        cruzo_cero = _sgn(viejo) != 0 and (_sgn(nuevo) == 0 or _sgn(nuevo) != _sgn(viejo))
        if cruzo_cero:
            self._registrar_cerrada(c, p, viejo, ts, motivo=o.origen)
            if _sgn(nuevo) != 0:   # volteo: lo que sobra abre al precio del llenado
                p.tipo_margen, p.abierta, p.entrada = tipo_margen, ts, precio
                p.pnl, p.comisiones, p.funding = 0.0, 0.0, 0.0
                p.valor_salida = p.cant_salida = 0.0
                p.margen_aislado = 0.0
                p.ultimo_funding = ts
        p.amt = nuevo
        if p.tipo_margen == "ISOLATED" and aumento > 0 and _sgn(nuevo) != 0:
            p.margen_aislado += aumento * precio / lev
        if _sgn(nuevo) == 0:
            p.margen_aislado = 0.0
        p.cant_max = max(p.cant_max, abs(nuevo))

        # orden
        tot = o.ejecutada + qty
        o.precio_prom = (o.precio_prom * o.ejecutada + precio * qty) / tot
        o.ejecutada = tot
        terminado = o.cerrar_posicion or o.ejecutada >= o.cantidad - EPS or self._es_reductora(o)
        if terminado:
            if not o.cerrar_posicion and o.ejecutada < o.cantidad - EPS:
                o.motivo = "Solo reducir: se llenó solo lo que había de posición"
            self._cerrar_orden(o, "FILLED", ts=ts)
        else:
            o.estado = "PARTIALLY_FILLED"
            self._guardar_orden(o)

        self._guardar_cuenta(c)
        self._guardar_pos(c.id, p)
        self.emitir("llenado", c.id, orden=o.dict(), cantidad=qty, precio=precio, comision=comision,
                    pnl=realizado, maker=maker, ts=ts)

        if cruzo_cero:
            self._cancelar_reductoras(c, o.simbolo, o.lado_pos, ts, excepto=o.id)

        # TP/SL adjuntos de la orden de entrada
        if (o.tp or o.sl) and not o.tpsl_creados and aumento > 0:
            o.tpsl_creados = True
            self._guardar_orden(o)
            self._crear_tpsl_adjuntos(c, o, ts)

    def _crear_tpsl_adjuntos(self, c: Cuenta, o: Orden, ts: int):
        lado_c = "SELL" if o.lado == "BUY" else "BUY"
        for precio_s, tipo, origen in ((o.tp, "TAKE_PROFIT_MARKET", "tp"), (o.sl, "STOP_MARKET", "sl")):
            if not precio_s:
                continue
            try:
                self.colocar(c.id, o.simbolo, lado_c, tipo, cantidad=o.cantidad, precio_stop=precio_s,
                             lado_pos=o.lado_pos, solo_reducir=not c.cobertura, disparo=o.tpsl_disparo,
                             origen=origen, ts=ts)
            except BinanceError as e:
                if e.code == -2021:   # ya se habría disparado: cerrar al momento
                    try:
                        self.colocar(c.id, o.simbolo, lado_c, "MARKET", cantidad=o.cantidad, lado_pos=o.lado_pos,
                                     solo_reducir=not c.cobertura, origen=origen, ts=ts)
                    except BinanceError as e2:
                        self.emitir("orden_rechazada", c.id, orden=o.dict(), error=str(e2))
                else:
                    self.emitir("orden_rechazada", c.id, orden=o.dict(), error=str(e))

    def _cancelar_reductoras(self, c: Cuenta, simbolo: str, lado_pos: str, ts: int, excepto: int = 0):
        for oid in list(self.por_simbolo.get(simbolo, ())):
            o = self.ordenes.get(oid)
            if not o or o.id == excepto or o.cuenta != c.id or o.lado_pos != lado_pos:
                continue
            if self._es_reductora(o):
                self._cerrar_orden(o, "CANCELED", "La posición se cerró", ts)
                self.emitir("orden_cancelada", c.id, orden=o.dict(), silenciosa=True)

    def _registrar_cerrada(self, c: Cuenta, p: Posicion, amt_viejo: float, ts: int, motivo: str = ""):
        salida = p.valor_salida / p.cant_salida if p.cant_salida else 0.0
        lado = "LONG" if amt_viejo > 0 else "SHORT"
        neto = p.pnl - p.comisiones + p.funding
        self.db.ex("INSERT INTO posiciones_cerradas(cuenta,simbolo,lado,entrada,salida,cantidad_max,pnl,comisiones,funding,abierta,cerrada,motivo) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (c.id, p.simbolo, lado, p.entrada, salida, p.cant_max, p.pnl, p.comisiones, p.funding, p.abierta, ts, motivo))
        self.emitir("posicion_cerrada", c.id, simbolo=p.simbolo, lado=lado, entrada=p.entrada, salida=salida,
                    pnl=p.pnl, neto=neto, motivo=motivo, ts=ts)

    # ------------------------------------------------------------------ eventos de precio
    def en_precio(self, simbolo: str, tipo: str, precio: float, ts: int | None = None):
        """tipo: 'last' (último negociado) o 'mark' (precio de marca)."""
        ts = ts or self.reloj()
        for oid in sorted(self.por_simbolo.get(simbolo, ())):
            o = self.ordenes.get(oid)
            if not o:
                continue
            try:
                self._revisar_orden(o, tipo, precio, ts)
            except BinanceError as e:
                self.emitir("orden_rechazada", o.cuenta, orden=o.dict(), error=str(e))
            except Exception:  # pragma: no cover
                log.exception("error revisando orden %s", oid)
        if tipo == "mark":
            self._revisar_cuentas(simbolo, ts)

    def _revisar_orden(self, o: Orden, tipo: str, precio: float, ts: int):
        es_limite = o.tipo == "LIMIT" or (o.tipo in ("STOP", "TAKE_PROFIT") and o.disparada)
        if es_limite:
            if tipo != "last":
                return
            toca = self.cfg.llenado_limite == "toca"
            if o.lado == "BUY":
                ok = precio <= o.precio if toca else precio < o.precio
            else:
                ok = precio >= o.precio if toca else precio > o.precio
            if ok:
                self._llenar(o, o.restante, o.precio, maker=True, ts=ts)
            return
        quiere = "mark" if o.disparo == "MARK_PRICE" else "last"
        if tipo != quiere:
            return
        if o.tipo == "TRAILING_STOP_MARKET":
            r = (o.retroceso or 1) / 100
            if not o.activado:
                if o.activacion is None or (o.lado == "SELL" and precio >= o.activacion) or (o.lado == "BUY" and precio <= o.activacion):
                    o.activado, o.extremo = True, precio
                    self._guardar_orden(o)
                return
            viejo = o.extremo
            if o.lado == "SELL":
                o.extremo = max(o.extremo or precio, precio)
                hit = precio <= o.extremo * (1 - r)
            else:
                o.extremo = min(o.extremo or precio, precio)
                hit = precio >= o.extremo * (1 + r)
            if hit:
                o.disparada = True
                self._ejecutar_mercado(o, ts, ref=None if self.m.en_vivo else precio)
            elif o.extremo != viejo:
                self._guardar_orden(o)
            return
        if self._toca_stop(o, precio):
            o.disparada = True
            if o.tipo.endswith("_MARKET"):
                self._ejecutar_mercado(o, ts, ref=None if self.m.en_vivo else precio)
            else:
                c = self.cuentas[o.cuenta]
                if not self._es_reductora(o):
                    try:
                        self._revisar_margen(c, o.simbolo, o.lado_pos, o.lado, o.restante, o.precio)
                    except BinanceError as e:
                        self._cerrar_orden(o, "EXPIRED", e.es, ts)
                        raise
                self._guardar_orden(o)
                self.emitir("orden_disparada", o.cuenta, orden=o.dict())
                self._limite_entrante(o, ts)

    # ------------------------------------------------------------------ liquidación y alertas
    def _revisar_cuentas(self, simbolo: str, ts: int):
        for c in self.cuentas.values():
            if not any(p.simbolo == simbolo and abs(p.amt) > EPS for p in c.posiciones.values()):
                continue
            self.revisar_liquidacion(c, ts)

    def revisar_liquidacion(self, c: Cuenta, ts: int | None = None):
        ts = ts or self.reloj()
        abiertas = self.posiciones_abiertas(c)
        for p in abiertas:
            if p.tipo_margen != "ISOLATED":
                continue
            mk = self.marca(p.simbolo, p.entrada)
            mm = calc.maint_margin(self.m.brackets(p.simbolo), abs(p.amt) * mk)
            if p.margen_aislado + calc.unrealized_pnl(p.amt, p.entrada, mk) <= mm:
                self._liquidar(c, [p], ts, aislada=True)
        cruz = [p for p in self.posiciones_abiertas(c) if p.tipo_margen == "CROSSED"]
        if cruz:
            met = self.metricas(c, con_detalle=False)
            if met["mm_cruzado"] > 0 and met["billetera_cruzada"] + met["upnl_cruzado"] <= met["mm_cruzado"]:
                self._liquidar(c, cruz, ts, aislada=False)
                return
            # alertas
            if met["ratio_margen"] >= self.cfg.alerta_margen_ratio:
                self._alerta(c, f"ratio:{c.id}", "margen_alto", ratio=met["ratio_margen"])
        for p in self.posiciones_abiertas(c):
            d = self.detalle_pos(c, p)
            if d["distancia_liq"] is not None and d["distancia_liq"] < self.cfg.alerta_distancia_liq * 100:
                self._alerta(c, f"liq:{c.id}:{p.simbolo}:{p.lado}", "cerca_liquidacion", posicion=d)
            if p.tipo_margen == "ISOLATED" and d["ratio_margen"] >= self.cfg.alerta_margen_ratio:
                self._alerta(c, f"ratioiso:{c.id}:{p.simbolo}:{p.lado}", "margen_alto", ratio=d["ratio_margen"], posicion=d)

    def _alerta(self, c: Cuenta, clave: str, tipo: str, **datos):
        t = self.reloj()
        if t - self._enfriar.get(clave, 0) < 3600_000:
            return
        self._enfriar[clave] = t
        self.emitir(tipo, c.id, **datos)

    def _liquidar(self, c: Cuenta, posiciones: list[Posicion], ts: int, aislada: bool):
        simbolos = {p.simbolo for p in posiciones}
        for oid in list(self.ordenes):
            o = self.ordenes[oid]
            if o.cuenta == c.id and (not aislada or o.simbolo in simbolos):
                self._cerrar_orden(o, "CANCELED", "Cancelada por liquidación", ts)
        for p in list(posiciones):
            if abs(p.amt) <= EPS:
                continue
            mk = self.marca(p.simbolo, p.entrada)
            notional = abs(p.amt) * mk
            margen_antes = p.margen_aislado
            saldo_antes = c.saldo
            o = Orden(cuenta=c.id, simbolo=p.simbolo, lado="SELL" if p.amt > 0 else "BUY", lado_pos=p.lado,
                      tipo="MARKET", cantidad=abs(p.amt), solo_reducir=not c.cobertura, origen="liquidacion",
                      creada=ts, actualizada=ts)
            cur = self.db.ex("INSERT INTO ordenes(cuenta,simbolo,estado,creada,actualizada,datos) VALUES(?,?,?,?,?,?)",
                             (c.id, p.simbolo, "NEW", ts, ts, "{}"))
            o.id = cur.lastrowid
            self.ordenes[o.id] = o
            self.por_simbolo[o.simbolo].add(o.id)
            self._llenar(o, o.cantidad, mk, maker=False, ts=ts)
            if aislada:
                # en aislado se pierde todo el margen de la posición
                perdido = margen_antes - (saldo_antes - c.saldo)
                cargo = max(0.0, perdido)
            else:
                met = self.metricas(c, con_detalle=False)
                cargo = min(notional * self.cfg.comision_liquidacion, max(0.0, met["billetera_cruzada"] + met["upnl_cruzado"]))
            if cargo > 0:
                c.saldo -= cargo
                self._tx(c.id, "INSURANCE_CLEAR", -cargo, p.simbolo, "Comisión de liquidación", ts)
            self.emitir("liquidacion", c.id, simbolo=p.simbolo, lado=p.lado, precio=mk, cargo=cargo, ts=ts)
        if c.saldo < 0:   # el fondo de seguro cubre el saldo negativo
            self._tx(c.id, "INSURANCE_CLEAR", -c.saldo, "", "Fondo de seguro cubre saldo negativo", ts)
            c.saldo = 0.0
        self._guardar_cuenta(c)

    # ------------------------------------------------------------------ funding
    def aplicar_funding(self, simbolo: str, tasa: float, tiempo: int, marca: float | None = None):
        mk = marca or self.marca(simbolo)
        if not mk:
            return
        for c in self.cuentas.values():
            for p in list(c.posiciones.values()):
                if p.simbolo != simbolo or abs(p.amt) <= EPS:
                    continue
                if p.abierta > tiempo or p.ultimo_funding >= tiempo:
                    continue
                monto = calc.funding_payment(p.amt, mk, tasa)
                c.saldo += monto
                if p.tipo_margen == "ISOLATED":
                    p.margen_aislado += monto
                p.funding += monto
                p.ultimo_funding = tiempo
                self._tx(c.id, "FUNDING_FEE", monto, simbolo, f"tasa {tasa * 100:.4f}%", tiempo)
                self._guardar_pos(c.id, p)
                self._guardar_cuenta(c)
                self.emitir("funding", c.id, simbolo=simbolo, monto=monto, tasa=tasa, ts=tiempo,
                            importante=abs(monto) >= self.cfg.alerta_funding_minimo)

    # ------------------------------------------------------------------ deslistado
    def deslistar(self, simbolo: str, ts: int | None = None):
        ts = ts or self.reloj()
        for oid in list(self.por_simbolo.get(simbolo, ())):
            o = self.ordenes.get(oid)
            if o:
                self._cerrar_orden(o, "CANCELED", "Moneda deslistada", ts)
        mk = self.marca(simbolo)
        for c in self.cuentas.values():
            for p in list(c.posiciones.values()):
                if p.simbolo == simbolo and abs(p.amt) > EPS:
                    precio = mk or p.entrada
                    o = Orden(cuenta=c.id, simbolo=simbolo, lado="SELL" if p.amt > 0 else "BUY", lado_pos=p.lado,
                              tipo="MARKET", cantidad=abs(p.amt), solo_reducir=not c.cobertura, origen="deslistado",
                              creada=ts, actualizada=ts)
                    cur = self.db.ex("INSERT INTO ordenes(cuenta,simbolo,estado,creada,actualizada,datos) VALUES(?,?,?,?,?,?)",
                                     (c.id, simbolo, "NEW", ts, ts, "{}"))
                    o.id = cur.lastrowid
                    self.ordenes[o.id] = o
                    self.por_simbolo[simbolo].add(o.id)
                    self._llenar(o, o.cantidad, precio, maker=False, ts=ts)
                    self.emitir("deslistado", c.id, simbolo=simbolo, precio=precio)

    # ------------------------------------------------------------------ acciones de usuario
    def cancelar(self, cuenta, orden_id: int) -> Orden:
        c = self.cuenta(cuenta)
        o = self.ordenes.get(int(orden_id))
        if not o or o.cuenta != c.id:
            raise BinanceError(-2011, "Unknown order sent.", f"No hay una orden abierta #{orden_id} en esta cuenta.")
        self._cerrar_orden(o, "CANCELED", "Cancelada por el usuario")
        return o

    def cancelar_todas(self, cuenta, simbolo: str | None = None) -> int:
        c = self.cuenta(cuenta)
        n = 0
        for o in list(self.ordenes.values()):
            if o.cuenta == c.id and (not simbolo or o.simbolo == simbolo.upper()):
                self._cerrar_orden(o, "CANCELED", "Cancelada por el usuario")
                n += 1
        return n

    def ordenes_abiertas(self, cuenta, simbolo: str | None = None) -> list[Orden]:
        c = self.cuenta(cuenta)
        return sorted([o for o in self.ordenes.values() if o.cuenta == c.id and (not simbolo or o.simbolo == simbolo)],
                      key=lambda o: o.id)

    def cerrar_posicion(self, cuenta, simbolo: str, porcentaje: float = 100, lado_pos: str | None = None,
                        origen: str = "manual") -> Orden:
        c = self.cuenta(cuenta)
        simbolo = simbolo.upper()
        if not simbolo.endswith("USDT"):
            simbolo += "USDT"
        cands = [p for p in self.posiciones_abiertas(c) if p.simbolo == simbolo and (not lado_pos or p.lado == lado_pos.upper())]
        if not cands:
            raise BinanceError(-2022, "ReduceOnly Order is rejected.", f"No hay posición abierta en {simbolo}.")
        if len(cands) > 1:
            raise BinanceError(-4061, "Order's position side does not match user's setting.",
                               "Tienes LONG y SHORT en esa moneda: indica cuál cerrar.")
        p = cands[0]
        si = self.info(simbolo)
        qty = abs(p.amt) if porcentaje >= 100 else calc.round_step(abs(p.amt) * porcentaje / 100, si.paso_mkt)
        if qty <= 0:
            raise BinanceError(-4003, "Quantity less than or equal to zero.", "Ese porcentaje da una cantidad menor al mínimo.")
        return self.colocar(c.id, simbolo, "SELL" if p.amt > 0 else "BUY", "MARKET", cantidad=qty,
                            lado_pos=p.lado, solo_reducir=not c.cobertura, origen=origen)

    def poner_tpsl(self, cuenta, simbolo: str, tp: float | None = None, sl: float | None = None,
                   lado_pos: str | None = None, disparo: str = "MARK_PRICE", quitar_tp=False, quitar_sl=False) -> list[Orden]:
        """TP/SL de toda la posición (closePosition). Reemplaza los anteriores."""
        c = self.cuenta(cuenta)
        simbolo = simbolo.upper()
        if not simbolo.endswith("USDT"):
            simbolo += "USDT"
        cands = [p for p in self.posiciones_abiertas(c) if p.simbolo == simbolo and (not lado_pos or p.lado == lado_pos.upper())]
        if len(cands) != 1:
            raise BinanceError(-2022, "ReduceOnly Order is rejected.",
                               "No hay posición abierta" if not cands else "Indica si es la posición LONG o SHORT.")
        p = cands[0]
        lado_c = "SELL" if p.amt > 0 else "BUY"
        hechas = []
        for valor, tipo, quitar in ((tp, "TAKE_PROFIT_MARKET", quitar_tp), (sl, "STOP_MARKET", quitar_sl)):
            if valor is None and not quitar:
                continue
            for o in list(self.ordenes.values()):
                if (o.cuenta == c.id and o.simbolo == simbolo and o.lado_pos == p.lado and o.tipo == tipo
                        and self._es_reductora(o)):
                    self._cerrar_orden(o, "CANCELED", "Reemplazada")
            if valor:
                hechas.append(self.colocar(c.id, simbolo, lado_c, tipo, precio_stop=valor, lado_pos=p.lado,
                                           cerrar_posicion=True, disparo=disparo,
                                           origen="tp" if tipo.startswith("TAKE") else "sl"))
        return hechas

    def cambiar_apalancamiento(self, cuenta, simbolo: str, lev: int):
        c = self.cuenta(cuenta)
        simbolo = simbolo.upper()
        self.info(simbolo)
        br = self.m.brackets(simbolo)
        mx = calc.max_leverage(br)
        if not (1 <= int(lev) <= mx):
            raise BinanceError(-4028, f"Leverage {lev} is not valid", f"Apalancamiento inválido. En {simbolo} va de 1x a {mx}x.")
        for p in self.posiciones_abiertas(c):
            if p.simbolo == simbolo and abs(p.amt) * self.marca(simbolo, p.entrada) > calc.max_notional_at_leverage(br, int(lev)):
                raise BinanceError(-2027, "Exceeded the maximum allowable position at current leverage.",
                                   "Tu posición es demasiado grande para ese apalancamiento.")
        self.ajuste(c, simbolo)[0] = int(lev)
        self._guardar_ajuste(c, simbolo)

    def cambiar_tipo_margen(self, cuenta, simbolo: str, tipo: str):
        c = self.cuenta(cuenta)
        simbolo, tipo = simbolo.upper(), tipo.upper()
        tipo = {"CRUZADO": "CROSSED", "AISLADO": "ISOLATED", "CROSS": "CROSSED"}.get(tipo, tipo)
        if tipo not in ("CROSSED", "ISOLATED"):
            raise BinanceError(-4044, "The margin type is invalid.", "Tipo de margen inválido (cruzado o aislado).")
        self.info(simbolo)
        a = self.ajuste(c, simbolo)
        if a[1] == tipo:
            raise BinanceError(-4046, "No need to change margin type.", "Ya está en ese tipo de margen.")
        if any(p.simbolo == simbolo for p in self.posiciones_abiertas(c)):
            raise BinanceError(-4048, "Margin type cannot be changed if there exists position.", "No se puede cambiar con una posición abierta.")
        if any(o.simbolo == simbolo and o.cuenta == c.id for o in self.ordenes.values()):
            raise BinanceError(-4047, "Margin type cannot be changed if there exists open orders.", "No se puede cambiar con órdenes abiertas.")
        a[1] = tipo
        self._guardar_ajuste(c, simbolo)

    def cambiar_modo_posicion(self, cuenta, cobertura: bool):
        c = self.cuenta(cuenta)
        if c.cobertura == cobertura:
            raise BinanceError(-4059, "No need to change position side.", "Ya está en ese modo.")
        if self.posiciones_abiertas(c):
            raise BinanceError(-4068, "Position side cannot be changed if there exists position.", "Cierra tus posiciones primero.")
        if any(o.cuenta == c.id for o in self.ordenes.values()):
            raise BinanceError(-4067, "Position side cannot be changed if there exists open orders.", "Cancela tus órdenes primero.")
        c.cobertura = cobertura
        c.posiciones.clear()
        self._guardar_cuenta(c)

    def margen_aislado(self, cuenta, simbolo: str, monto: float, lado_pos: str | None = None):
        """monto > 0 agrega, < 0 quita."""
        c = self.cuenta(cuenta)
        simbolo = simbolo.upper()
        cands = [p for p in self.posiciones_abiertas(c) if p.simbolo == simbolo and p.tipo_margen == "ISOLATED"
                 and (not lado_pos or p.lado == lado_pos.upper())]
        if len(cands) != 1:
            raise BinanceError(-4051, "Isolated balance insufficient.", "No hay una posición aislada (única) en esa moneda.")
        p = cands[0]
        if monto > 0:
            if monto > self.metricas(c, con_detalle=False)["disponible"]:
                raise BinanceError(-2019, "Margin is insufficient.", "No tienes saldo disponible suficiente.")
        else:
            mk = self.marca(simbolo, p.entrada)
            lev, _ = self.ajuste(c, simbolo)
            maximo = p.margen_aislado + min(0.0, calc.unrealized_pnl(p.amt, p.entrada, mk)) - abs(p.amt) * mk / lev
            if -monto > maximo:
                raise BinanceError(-4050, "Cross balance insufficient.", f"Solo puedes quitar hasta {max(0, maximo):,.2f} USDT.")
        p.margen_aislado += monto
        self._guardar_pos(c.id, p)

    def transferir(self, cuenta, monto: float, info: str = ""):
        c = self.cuenta(cuenta)
        if monto == 0:
            raise BinanceError(-4003, "Quantity less than or equal to zero.", "El monto no puede ser cero.")
        if monto < 0 and -monto > self.metricas(c, con_detalle=False)["disponible"] + 1e-9:
            raise BinanceError(-2019, "Margin is insufficient.", "No puedes retirar más que tu saldo disponible.")
        c.saldo += monto
        self._tx(c.id, "TRANSFER", monto, "", info or ("Recarga" if monto > 0 else "Retiro"))
        self._guardar_cuenta(c)
        return c.saldo

    def reiniciar_cuenta(self, cuenta, saldo: float | None = None):
        c = self.cuenta(cuenta)
        for o in list(self.ordenes.values()):
            if o.cuenta == c.id:
                self._cerrar_orden(o, "CANCELED", "Cuenta reiniciada")
        for t in ("posiciones", "ordenes", "llenados", "transacciones", "posiciones_cerradas", "ajustes_simbolo"):
            self.db.ex(f"DELETE FROM {t} WHERE cuenta=?", (c.id,))
        c.posiciones.clear()
        c.ajustes.clear()
        c.saldo = 0.0
        c.cobertura = False
        self._guardar_cuenta(c)
        s = self.cfg.saldo_inicial if saldo is None else saldo
        if s > 0:
            self.transferir(c.id, s, "Saldo inicial (reinicio)")
