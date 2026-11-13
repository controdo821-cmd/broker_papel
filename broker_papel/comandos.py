"""Comandos de texto (los usa la terminal y también Telegram con /c ...)."""
from __future__ import annotations

import shlex

from . import calc
from .modelos import BinanceError
from .vistas import sin_html

AYUDA = """Comandos (la cuenta puede ser su nombre o 1, 2, 3):

estado                                   resumen de todo
resumen <cuenta>                         saldo, margen y PnL
posiciones <cuenta>
ordenes <cuenta>
historial <cuenta>                       llenados y movimientos de hoy
precio <moneda>

abrir <cuenta> <moneda> <long|short> <monto> [x10] [limite <precio>] [tp <precio>] [sl <precio>] [aislado|cruzado]
    monto: 100 = 100 USDT de margen   ·   q0.01 = 0.01 monedas
    ej: abrir 1 BTC long 100 x10 tp 120000 sl 95000
cerrar <cuenta> <moneda> [porcentaje] [long|short]
tpsl <cuenta> <moneda> [tp <precio>|tp no] [sl <precio>|sl no]
orden <cuenta> <moneda> <compra|venta> <tipo> cant=.. precio=.. stop=.. retroceso=.. activacion=..
      [reducir] [postonly] [ioc|fok] [marca|ultimo] [long|short]
    tipos: market, limite, stop, stoplimite, tp, tplimite, trailing
cancelar <cuenta> <id|todas> [moneda]
apalancamiento <cuenta> <moneda> <n>
margen <cuenta> <moneda> <cruzado|aislado>
margen-aislado <cuenta> <moneda> <+monto|-monto>
modo <cuenta> <cobertura|unidireccional>
comisiones <cuenta> <maker%> <taker%>      ej: comisiones 1 0.02 0.05

recargar <cuenta> <monto>
retirar <cuenta> <monto>
reiniciar-cuenta <cuenta> [saldo]          (borra todo el historial de esa cuenta)
respaldo                                   copia de la base de datos ahora

estadisticas <cuenta> [hoy|ayer|semana|mes|todo]
calendario <cuenta> [AAAA-MM]              PnL por día
anio <cuenta> [AAAA]                       PnL por mes
exportar <cuenta> [excel|csv]              guarda el historial en data/exportes
reporte <HH:MM|no>                         hora del reporte diario en Telegram
"""

TIPOS_ALIAS = {
    "market": "MARKET", "mercado": "MARKET", "limite": "LIMIT", "limit": "LIMIT", "límite": "LIMIT",
    "stop": "STOP_MARKET", "stopmarket": "STOP_MARKET", "stoplimite": "STOP", "stoplimit": "STOP",
    "tp": "TAKE_PROFIT_MARKET", "takeprofit": "TAKE_PROFIT_MARKET", "tplimite": "TAKE_PROFIT", "tplimit": "TAKE_PROFIT",
    "trailing": "TRAILING_STOP_MARKET",
}


def num(x: str) -> float:
    return float(str(x).replace(",", "").replace("$", ""))


def simb(x: str) -> str:
    s = x.upper().replace("/", "").replace("-", "")
    return s if s.endswith("USDT") else s + "USDT"


class Comandos:
    def __init__(self, motor, mercado, vistas, respaldo_fn=None, informes=None):
        self.motor, self.m, self.v = motor, mercado, vistas
        self.respaldo_fn = respaldo_fn
        self.inf = informes

    async def ejecutar(self, linea: str | list, origen: str = "terminal") -> str:
        args = shlex.split(linea) if isinstance(linea, str) else list(linea)
        if not args:
            return AYUDA
        cmd, a = args[0].lower(), args[1:]
        try:
            f = getattr(self, "c_" + cmd.replace("-", "_"), None)
            if not f:
                return f"No conozco el comando '{cmd}'.\n\n" + AYUDA
            return await f(a, origen)
        except BinanceError as e:
            return "❌ " + str(e)
        except (IndexError, ValueError):
            return "❌ Faltan datos o hay un número mal escrito. Escribe 'ayuda'."

    # ---------------- consultas
    async def c_ayuda(self, a, o):
        return AYUDA

    async def c_estado(self, a, o):
        return self.v.estado()

    async def c_resumen(self, a, o):
        return self.v.resumen(a[0])

    async def c_posiciones(self, a, o):
        ps = self.v.posiciones(a[0])
        return "\n\n".join(t for _, t in ps) if ps else "Sin posiciones abiertas."

    async def c_ordenes(self, a, o):
        os_ = self.motor.ordenes_abiertas(a[0])
        return "\n\n".join(self.v.orden(x) for x in os_) if os_ else "Sin órdenes abiertas."

    async def c_historial(self, a, o):
        return self.v.historial_hoy(a[0])

    async def c_precio(self, a, o):
        s = simb(a[0])
        if not self.m.simbolo(s):
            raise BinanceError(-1121, "Invalid symbol.", f"{s} no existe en Binance Futures.")
        await self.m.asegurar(s)
        d = self.m.datos_marca(s) or {}
        lib = self.m.libro(s)
        import time
        falta = (d.get("T", 0) - time.time() * 1000) / 1000 if d.get("T") else None
        cuenta = f"{int(falta // 3600)}h {int(falta % 3600 // 60)}m" if falta and falta > 0 else "--"
        return (f"<b>{s}</b>\nÚltimo: {self.v.precio(s, self.m.ultimo(s))}\nMarca: {self.v.precio(s, d.get('p'))}"
                f"\nÍndice: {self.v.precio(s, d.get('i'))}"
                f"\nBid/Ask: {self.v.precio(s, lib[0]) if lib else '--'} / {self.v.precio(s, lib[1]) if lib else '--'}"
                f"\nFunding: {d.get('r', 0) * 100:.4f}%  ·  próximo en {cuenta}"
                f"\nApalancamiento máx: {calc.max_leverage(self.m.brackets(s))}x")

    # ---------------- operar
    def cantidad_desde_monto(self, cuenta, s: str, monto: str, lev: int, precio: float) -> float:
        si = self.motor.info(s)
        if monto.lower().startswith("q"):
            q = num(monto[1:])
        else:
            q = num(monto.lower().replace("usdt", "")) * lev / precio
        q = calc.round_step(q, si.paso, "down")
        if q <= 0:
            raise BinanceError(-4003, "Quantity less than or equal to zero.", "El monto es muy chico para esta moneda.")
        return q

    async def c_abrir(self, a, origen):
        cuenta, s, lado_txt, monto = a[0], simb(a[1]), a[2].lower(), a[3]
        c = self.motor.cuenta(cuenta)
        if lado_txt not in ("long", "short", "compra", "venta", "buy", "sell"):
            return "❌ El lado debe ser long o short."
        largo = lado_txt in ("long", "compra", "buy")
        self.motor.info(s)
        await self.m.asegurar(s)
        resto = [x.lower() for x in a[4:]]
        precio = tp = sl = None
        i = 0
        while i < len(resto):
            x = resto[i]
            if x.startswith("x") and x[1:].isdigit():
                self.motor.cambiar_apalancamiento(c.id, s, int(x[1:]))
            elif x in ("aislado", "cruzado"):
                try:
                    self.motor.cambiar_tipo_margen(c.id, s, x)
                except BinanceError as e:
                    if e.code != -4046:
                        raise
            elif x in ("limite", "límite", "limit"):
                precio = num(resto[i + 1]); i += 1
            elif x == "tp":
                tp = num(resto[i + 1]); i += 1
            elif x == "sl":
                sl = num(resto[i + 1]); i += 1
            i += 1
        lev = self.motor.ajuste(c, s)[0]
        ref = precio or self.motor.ultimo(s)
        q = self.cantidad_desde_monto(c.id, s, monto, lev, ref)
        lado_pos = ("LONG" if largo else "SHORT") if c.cobertura else None
        si = self.motor.info(s)
        if tp:
            tp = calc.round_step(tp, si.tick, "near")
        if sl:
            sl = calc.round_step(sl, si.tick, "near")
        o = self.motor.colocar(c.id, s, "BUY" if largo else "SELL", "LIMIT" if precio else "MARKET", cantidad=q,
                               precio=calc.round_step(precio, si.tick, "near") if precio else None,
                               lado_pos=lado_pos, tp=tp, sl=sl, origen=origen)
        return self._resultado(o)

    def _resultado(self, o) -> str:
        est = {"FILLED": "✅ Llenada", "NEW": "🕒 Puesta", "PARTIALLY_FILLED": "🕒 Parcial", "EXPIRED": "⌛ Expirada",
               "CANCELED": "Cancelada"}.get(o.estado, o.estado)
        t = f"{est}: " + self.v.orden(o)
        if o.ejecutada:
            t += f"\nPrecio promedio: {self.v.precio(o.simbolo, o.precio_prom)}"
        if o.motivo:
            t += f"\n{o.motivo}"
        return t

    async def c_cerrar(self, a, origen):
        cuenta, s = a[0], simb(a[1])
        pct_, lado = 100.0, None
        for x in a[2:]:
            if x.lower() in ("long", "short"):
                lado = x.upper()
            else:
                pct_ = num(x.replace("%", ""))
        await self.m.asegurar(s)
        o = self.motor.cerrar_posicion(cuenta, s, pct_, lado, origen=origen)
        return self._resultado(o)

    async def c_tpsl(self, a, origen):
        cuenta, s = a[0], simb(a[1])
        tp = sl = None
        qtp = qsl = False
        lado = None
        r = [x.lower() for x in a[2:]]
        i = 0
        while i < len(r):
            if r[i] in ("long", "short"):
                lado = r[i].upper()
            elif r[i] == "tp":
                if r[i + 1] == "no":
                    qtp = True
                else:
                    tp = num(r[i + 1])
                i += 1
            elif r[i] == "sl":
                if r[i + 1] == "no":
                    qsl = True
                else:
                    sl = num(r[i + 1])
                i += 1
            i += 1
        si = self.motor.info(s)
        tp = calc.round_step(tp, si.tick, "near") if tp else None
        sl = calc.round_step(sl, si.tick, "near") if sl else None
        hechas = self.motor.poner_tpsl(cuenta, s, tp=tp, sl=sl, lado_pos=lado, quitar_tp=qtp, quitar_sl=qsl)
        return "✅ TP/SL actualizado.\n" + "\n".join(self.v.orden(x) for x in hechas)

    async def c_orden(self, a, origen):
        cuenta, s, lado_txt, tipo_txt = a[0], simb(a[1]), a[2].lower(), a[3].lower()
        c = self.motor.cuenta(cuenta)
        lado = "BUY" if lado_txt in ("compra", "buy", "long") else "SELL" if lado_txt in ("venta", "sell", "short") else None
        if not lado:
            return "❌ Lado: compra o venta."
        tipo = TIPOS_ALIAS.get(tipo_txt)
        if not tipo:
            return "❌ Tipo de orden: market, limite, stop, stoplimite, tp, tplimite o trailing."
        kw = dict(tif="GTC", solo_reducir=False, disparo="CONTRACT_PRICE")
        lado_pos = None
        for x in a[4:]:
            xl = x.lower()
            if "=" in xl:
                k, v = xl.split("=", 1)
                k = {"cant": "cantidad", "qty": "cantidad", "stop": "precio_stop", "act": "activacion"}.get(k, k)
                kw[k] = num(v)
            elif xl in ("reducir", "solo-reducir", "reduceonly"):
                kw["solo_reducir"] = True
            elif xl in ("postonly", "post-only"):
                kw["tif"] = "GTX"
            elif xl in ("ioc", "fok", "gtc"):
                kw["tif"] = xl.upper()
            elif xl == "marca":
                kw["disparo"] = "MARK_PRICE"
            elif xl in ("ultimo", "último"):
                kw["disparo"] = "CONTRACT_PRICE"
            elif xl in ("long", "short"):
                lado_pos = xl.upper()
            elif xl == "cerrar":
                kw["cerrar_posicion"] = True
        if c.cobertura and not lado_pos:
            lado_pos = "LONG" if (lado == "BUY") != kw["solo_reducir"] else "SHORT"
            kw["solo_reducir"] = False
        await self.m.asegurar(s)
        o = self.motor.colocar(c.id, s, lado, tipo, lado_pos=lado_pos, origen=origen, **kw)
        return self._resultado(o)

    async def c_cancelar(self, a, origen):
        if a[1].lower() in ("todas", "todo", "all"):
            n = self.motor.cancelar_todas(a[0], simb(a[2]) if len(a) > 2 else None)
            return f"✅ {n} órdenes canceladas."
        o = self.motor.cancelar(a[0], int(a[1].lstrip("#")))
        return f"✅ Orden #{o.id} cancelada."

    async def c_apalancamiento(self, a, o):
        self.motor.cambiar_apalancamiento(a[0], simb(a[1]), int(a[2].lower().lstrip("x")))
        return f"✅ Apalancamiento de {simb(a[1])}: {int(a[2].lower().lstrip('x'))}x"

    async def c_margen(self, a, o):
        self.motor.cambiar_tipo_margen(a[0], simb(a[1]), a[2])
        return f"✅ {simb(a[1])} ahora en margen {a[2].lower()}."

    async def c_margen_aislado(self, a, o):
        self.motor.margen_aislado(a[0], simb(a[1]), num(a[2]))
        return "✅ Margen aislado ajustado."

    async def c_modo(self, a, o):
        cob = a[1].lower().startswith("cob") or a[1].lower() == "hedge"
        self.motor.cambiar_modo_posicion(a[0], cob)
        return f"✅ Modo {'cobertura' if cob else 'unidireccional'}."

    async def c_comisiones(self, a, o):
        c = self.motor.cuenta(a[0])
        c.maker, c.taker = num(a[1]) / 100, num(a[2]) / 100
        self.motor._guardar_cuenta(c)
        return f"✅ Comisiones de {c.nombre}: maker {c.maker * 100:.4f}%  taker {c.taker * 100:.4f}%"

    async def c_recargar(self, a, o):
        s = self.motor.transferir(a[0], abs(num(a[1])), f"Recarga ({o})")
        return f"✅ Recarga hecha. Saldo: {s:,.2f} USDT"

    async def c_retirar(self, a, o):
        s = self.motor.transferir(a[0], -abs(num(a[1])), f"Retiro ({o})")
        return f"✅ Retiro hecho. Saldo: {s:,.2f} USDT"

    async def c_reiniciar_cuenta(self, a, o):
        saldo = num(a[1]) if len(a) > 1 else None
        self.motor.reiniciar_cuenta(a[0], saldo)
        c = self.motor.cuenta(a[0])
        return f"✅ Cuenta {c.nombre} reiniciada. Saldo: {c.saldo:,.2f} USDT"

    async def c_respaldo(self, a, o):
        if not self.respaldo_fn:
            return "Respaldo no disponible."
        return f"✅ Respaldo guardado en {self.respaldo_fn()}"

    # ---------------- informes
    async def c_estadisticas(self, a, o):
        return self.inf.texto_estadisticas(a[0], a[1].lower() if len(a) > 1 else "todo")

    async def c_calendario(self, a, o):
        from datetime import datetime
        if len(a) > 1:
            anio, mes = (int(x) for x in a[1].split("-"))
        else:
            hoy = datetime.now(self.inf.est.tz)
            anio, mes = hoy.year, hoy.month
        return self.inf.texto_calendario(a[0], anio, mes)

    async def c_anio(self, a, o):
        from datetime import datetime
        anio = int(a[1]) if len(a) > 1 else datetime.now(self.inf.est.tz).year
        return self.inf.texto_anio(a[0], anio)

    async def c_exportar(self, a, o):
        formato = a[1].lower() if len(a) > 1 else "excel"
        nombre, datos = (self.inf.est.exportar_csv_zip(a[0]) if formato == "csv" else self.inf.est.exportar_excel(a[0]))
        carpeta = self.motor.cfg.data_dir / "exportes"
        carpeta.mkdir(parents=True, exist_ok=True)
        (carpeta / nombre).write_bytes(datos)
        return f"✅ Guardado en {carpeta / nombre}"

    async def c_reporte(self, a, o):
        v = a[0].lower()
        if v in ("no", "apagar", "off"):
            self.motor.db.set("reporte_hora", None)
            return "✅ Reporte diario apagado."
        h, m = (int(x) for x in (v.split(":") + ["0"])[:2])
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError
        self.motor.db.set("reporte_hora", f"{h:02d}:{m:02d}")
        return f"✅ Reporte diario a las {h:02d}:{m:02d} ({self.motor.cfg.zona_horaria})."


def texto_plano(t: str) -> str:
    return sin_html(t)
