"""Textos en español para Telegram y la terminal (HTML sencillo: solo <b> y <code>)."""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .calc import fmt_num

TIPO_ES = {
    "LIMIT": "Límite", "MARKET": "Market", "STOP": "Stop Límite", "STOP_MARKET": "Stop Market",
    "TAKE_PROFIT": "Take Profit Límite", "TAKE_PROFIT_MARKET": "Take Profit Market",
    "TRAILING_STOP_MARKET": "Trailing Stop",
}
ESTADO_ES = {"NEW": "Abierta", "PARTIALLY_FILLED": "Parcial", "FILLED": "Llenada", "CANCELED": "Cancelada",
             "EXPIRED": "Expirada"}
TX_ES = {"REALIZED_PNL": "PnL realizado", "COMMISSION": "Comisión", "FUNDING_FEE": "Funding",
         "TRANSFER": "Transferencia", "INSURANCE_CLEAR": "Liquidación"}


def sin_html(t: str) -> str:
    return html.unescape(re.sub(r"</?(b|code|i)>", "", t))


def usd(x: float, signo: bool = False) -> str:
    s = f"{x:+,.2f}" if signo else f"{x:,.2f}"
    return s + " USDT"


def pct(x: float | None, signo: bool = True) -> str:
    if x is None:
        return "--"
    return f"{x:+.2f}%" if signo else f"{x:.2f}%"


def ico(x: float) -> str:
    return "🟢" if x > 0 else ("🔴" if x < 0 else "⚪")


class Vistas:
    def __init__(self, motor, mercado, cfg):
        self.motor, self.m, self.cfg = motor, mercado, cfg
        self.tz = ZoneInfo(cfg.zona_horaria)

    # ---------- tiempo
    def hora(self, ms: int, fmt: str = "%d/%m %H:%M") -> str:
        return datetime.fromtimestamp(ms / 1000, self.tz).strftime(fmt)

    def inicios(self) -> dict:
        ahora = datetime.now(self.tz)
        hoy = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
        semana = hoy - timedelta(days=hoy.weekday())
        mes = hoy.replace(day=1)
        return {k: int(v.timestamp() * 1000) for k, v in (("hoy", hoy), ("semana", semana), ("mes", mes))}

    def precio(self, simbolo: str, x: float | None) -> str:
        si = self.m.simbolo(simbolo)
        return fmt_num(x, si.tick if si else None) if x else "--"

    # ---------- resumen
    def resumen(self, cuenta) -> str:
        c = self.motor.cuenta(cuenta)
        met = self.motor.metricas(c)
        ini = self.inicios()
        h, s, mm = (self.motor.pnl_desde(c, ini[k]) for k in ("hoy", "semana", "mes"))
        modo = "Cobertura" if c.cobertura else "Unidireccional"
        return (
            f"💼 <b>{html.escape(c.nombre)}</b>  ·  {modo}\n"
            f"Saldo (billetera): <b>{usd(met['saldo'])}</b>\n"
            f"Balance de margen: {usd(met['balance_margen'])}\n"
            f"PnL no realizado: {ico(met['upnl'])} {usd(met['upnl'], True)}\n"
            f"Margen usado: {usd(met['margen_inicial'])}  ·  en órdenes: {usd(met['margen_ordenes'])}\n"
            f"Disponible: {usd(met['disponible'])}\n"
            f"Ratio de margen (cruzado): {pct(met['ratio_margen'] * 100, False)}\n"
            f"Posiciones: {len(met['posiciones'])}  ·  Órdenes: {len(self.motor.ordenes_abiertas(c.id))}\n\n"
            f"PnL hoy: {ico(h)} {usd(h, True)}\n"
            f"PnL semana: {ico(s)} {usd(s, True)}\n"
            f"PnL mes: {ico(mm)} {usd(mm, True)}"
        )

    # ---------- posiciones
    def posicion(self, d: dict) -> str:
        lado = "LONG" if d["amt"] > 0 else "SHORT"
        s = d["simbolo"]
        tm = "Cruzado" if d["tipo_margen"] == "CROSSED" else "Aislado"
        dist = f"{d['distancia_liq']:.2f}%" if d["distancia_liq"] is not None else "--"
        tpsl = self.tpsl_de(d)
        return (
            f"{'🟢' if lado == 'LONG' else '🔴'} <b>{s}</b> {lado} {d['apalancamiento']}x {tm}\n"
            f"Tamaño: {fmt_num(abs(d['amt']))} ({usd(d['notional'])})\n"
            f"Entrada: {self.precio(s, d['entrada'])}  ·  Marca: {self.precio(s, d['marca'])}\n"
            f"PnL: {ico(d['upnl'])} <b>{usd(d['upnl'], True)}</b>  ROE {pct(d['roe'])}\n"
            f"Margen: {usd(d['margen'])}  ·  Ratio: {pct(d['ratio_margen'] * 100, False)}\n"
            f"Liquidación: {self.precio(s, d['liquidacion'])}  (a {dist})"
            + (f"\n{tpsl}" if tpsl else "")
        )

    def tpsl_de(self, d: dict) -> str:
        partes = []
        for o in self.motor.ordenes_abiertas(self.motor_cuenta_de(d)):
            if o.simbolo != d["simbolo"] or o.lado_pos != d["lado"]:
                continue
            if o.tipo in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT"):
                partes.append(f"TP {self.precio(o.simbolo, o.precio_stop)}")
            elif o.tipo in ("STOP_MARKET", "STOP") and self.motor._es_reductora(o):
                partes.append(f"SL {self.precio(o.simbolo, o.precio_stop)}")
            elif o.tipo == "TRAILING_STOP_MARKET":
                partes.append(f"Trailing {o.retroceso}%")
        return "🎯 " + "  ·  ".join(partes) if partes else ""

    def motor_cuenta_de(self, d):
        return d.get("_cuenta", 0)

    def posiciones(self, cuenta) -> list[tuple[dict, str]]:
        c = self.motor.cuenta(cuenta)
        out = []
        for d in self.motor.metricas(c)["posiciones"]:
            d["_cuenta"] = c.id
            out.append((d, self.posicion(d)))
        return out

    # ---------- órdenes
    def orden(self, o) -> str:
        s = o.simbolo
        lado = "Compra" if o.lado == "BUY" else "Venta"
        partes = [f"#{o.id} <b>{s}</b> {TIPO_ES.get(o.tipo, o.tipo)} {lado}"]
        if o.lado_pos != "BOTH":
            partes.append(o.lado_pos)
        cant = "Toda la posición" if o.cerrar_posicion else fmt_num(o.cantidad)
        det = [f"Cant: {cant}"]
        if o.precio:
            det.append(f"Precio: {self.precio(s, o.precio)}")
        if o.precio_stop:
            det.append(f"Disparo: {self.precio(s, o.precio_stop)} ({'marca' if o.disparo == 'MARK_PRICE' else 'último'})")
        if o.tipo == "TRAILING_STOP_MARKET":
            det.append(f"Retroceso {o.retroceso}%" + (f", activa en {self.precio(s, o.activacion)}" if o.activacion else ""))
        if o.solo_reducir:
            det.append("Solo reducir")
        if o.tif != "GTC" and o.tipo == "LIMIT":
            det.append("Post only" if o.tif == "GTX" else o.tif)
        if o.ejecutada:
            det.append(f"Llenado {fmt_num(o.ejecutada)}")
        return " ".join(partes) + "\n" + "  ·  ".join(det)

    # ---------- historial
    def historial_hoy(self, cuenta) -> str:
        c = self.motor.cuenta(cuenta)
        ini = self.inicios()["hoy"]
        fills = self.motor.db.all("SELECT * FROM llenados WHERE cuenta=? AND tiempo>=? ORDER BY tiempo", (c.id, ini))
        txs = self.motor.db.all("SELECT tipo, SUM(monto) s FROM transacciones WHERE cuenta=? AND tiempo>=? GROUP BY tipo",
                                (c.id, ini))
        lineas = [f"📜 <b>Hoy en {html.escape(c.nombre)}</b>"]
        if not fills:
            lineas.append("Sin llenados hoy.")
        for f in fills[-30:]:
            lado = "Compra" if f["lado"] == "BUY" else "Venta"
            pnl = f"  PnL {f['pnl']:+,.2f}" if f["pnl"] else ""
            lineas.append(f"{self.hora(f['tiempo'], '%H:%M')} {f['simbolo']} {lado} {fmt_num(f['cantidad'])} @ "
                          f"{self.precio(f['simbolo'], f['precio'])}  com {f['comision']:.2f}{pnl}")
        if txs:
            lineas.append("")
            for t in txs:
                lineas.append(f"{TX_ES.get(t['tipo'], t['tipo'])}: {t['s']:+,.2f}")
        return "\n".join(lineas)

    # ---------- estado general
    def estado(self) -> str:
        lineas = ["📊 <b>Broker Papel</b>",
                  f"Binance WebSocket: {'conectado ✅' if self.m.ws_conectado else 'desconectado ❌'}",
                  f"Peso REST último minuto (este programa): {self.m.lim.usado_ultimo_minuto()}"
                  + (f"  ·  IP total: {self.m.lim.usado_ip}" if self.m.lim.usado_ip else ""),
                  f"Monedas siguiendo de cerca: {', '.join(sorted(self.m._suscritos)) or 'ninguna'}", ""]
        for c in sorted(self.motor.cuentas.values(), key=lambda x: x.id):
            met = self.motor.metricas(c, con_detalle=False)
            n = len(self.motor.posiciones_abiertas(c))
            lineas.append(f"• <b>{html.escape(c.nombre)}</b>: saldo {usd(c.saldo)}  ·  PnL abierto {usd(met['upnl'], True)}"
                          f"  ·  {n} posiciones  ·  {len(self.motor.ordenes_abiertas(c.id))} órdenes")
        return "\n".join(lineas)

    # ---------- alertas
    def evento(self, ev: dict) -> str | None:
        t, cn = ev["tipo"], html.escape(ev.get("nombre_cuenta", ""))
        if t == "llenado":
            o = ev["orden"]
            if o["origen"] == "liquidacion":
                return None
            if o["origen"] == "telegram" and o["tipo"] == "MARKET":
                return None   # ya se respondió en el chat
            que = {"tp": "🎯 Take Profit ejecutado", "sl": "🛑 Stop Loss ejecutado",
                   "deslistado": "⚠️ Cierre por deslistado"}.get(o["origen"], "✅ Orden llenada")
            if o["tipo"] == "TRAILING_STOP_MARKET":
                que = "🛑 Trailing stop ejecutado"
            elif o["tipo"] in ("STOP_MARKET", "STOP") and o["origen"] not in ("tp", "sl"):
                que = "🛑 Stop ejecutado"
            elif o["tipo"] in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT") and o["origen"] not in ("tp", "sl"):
                que = "🎯 Take Profit ejecutado"
            lado = "Compra" if o["lado"] == "BUY" else "Venta"
            pnl = f"\nPnL realizado: {ico(ev['pnl'])} {usd(ev['pnl'], True)}" if ev["pnl"] else ""
            return (f"{que} [{cn}]\n{o['simbolo']} {lado} {fmt_num(ev['cantidad'])} @ {self.precio(o['simbolo'], ev['precio'])}"
                    f"\nComisión: {ev['comision']:.4f} USDT ({'maker' if ev['maker'] else 'taker'}){pnl}")
        if t == "liquidacion":
            return (f"💥 <b>LIQUIDACIÓN</b> [{cn}]\n{ev['simbolo']} {ev['lado']} a {self.precio(ev['simbolo'], ev['precio'])}"
                    f"\nComisión de liquidación: {usd(ev['cargo'])}")
        if t == "margen_alto":
            return f"⚠️ [{cn}] Ratio de margen en {ev['ratio'] * 100:.1f}%. Riesgo de liquidación."
        if t == "cerca_liquidacion":
            d = ev["posicion"]
            return (f"⚠️ [{cn}] {d['simbolo']} a {d['distancia_liq']:.2f}% de su liquidación "
                    f"({self.precio(d['simbolo'], d['liquidacion'])}).")
        if t == "funding" and ev.get("importante"):
            v = "Cobraste" if ev["monto"] > 0 else "Pagaste"
            return f"💸 [{cn}] Funding {ev['simbolo']}: {v} {abs(ev['monto']):,.4f} USDT (tasa {ev['tasa'] * 100:.4f}%)."
        if t == "orden_rechazada":
            o = ev["orden"]
            return f"❌ [{cn}] Orden #{o['id']} {o['simbolo']} no se ejecutó:\n{html.escape(ev['error'])}"
        if t == "orden_disparada":
            o = ev["orden"]
            return f"⚡ [{cn}] Se disparó la orden #{o['id']} {o['simbolo']} ({TIPO_ES.get(o['tipo'], o['tipo'])})."
        if t == "deslistado":
            return f"⚠️ [{cn}] {ev['simbolo']} fue deslistada. Posición cerrada a {self.precio(ev['simbolo'], ev['precio'])}."
        return None
