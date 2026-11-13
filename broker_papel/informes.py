"""Informes: textos de estadísticas, reporte diario e imágenes (gráfica de posición, calendario, curva)."""
from __future__ import annotations

import asyncio
import html
import time
from datetime import datetime

from . import graficas
from .estadisticas import Estadisticas
from .graficas import AMARILLO, AZUL, ROJO, VERDE, GRIS, MESES
from .vistas import usd, ico, pct

RANGOS_ES = {"hoy": "hoy", "ayer": "ayer", "semana": "esta semana", "mes": "este mes", "todo": "todo el historial"}


class Informes:
    def __init__(self, motor, mercado, cfg, vistas):
        self.motor, self.m, self.cfg, self.v = motor, mercado, cfg, vistas
        self.est = Estadisticas(motor, cfg)

    # ------------------------------------------------------------ textos
    def texto_estadisticas(self, cuenta, rango: str = "todo") -> str:
        c = self.motor.cuenta(cuenta)
        desde, hasta = self.est.rango(rango)
        r = self.est.resumen(c.id, desde, hasta)

        def op(fila):
            if not fila:
                return "--"
            n = fila["pnl"] - fila["comisiones"] + fila["funding"]
            return f"{fila['simbolo']} {fila['lado']} {usd(n, True)}"
        return (f"📊 <b>Estadísticas de {html.escape(c.nombre)}</b> ({RANGOS_ES.get(rango, rango)})\n"
                f"Operaciones cerradas: {r['operaciones']}\n"
                f"Ganadoras: {r['ganadoras']} ({pct(r['pct_ganadoras'], False)})\n"
                f"Promedio ganadora: {usd(r['promedio_ganadora'], True)}  ·  perdedora: {usd(r['promedio_perdedora'], True)}\n"
                f"Mejor: {op(r['mejor'])}\nPeor: {op(r['peor'])}\n\n"
                f"PnL realizado: {usd(r['pnl_realizado'], True)}\n"
                f"Comisiones: {usd(r['comisiones'], True)}\n"
                f"Funding: {usd(r['funding'], True)}\n"
                + (f"Liquidaciones: {usd(r['liquidaciones'], True)}\n" if r["liquidaciones"] else "")
                + f"<b>Neto: {ico(r['neto'])} {usd(r['neto'], True)}</b>\n"
                f"Transferencias (no cuentan): {usd(r['transferencias'], True)}")

    def texto_calendario(self, cuenta, anio: int, mes: int) -> str:
        c = self.motor.cuenta(cuenta)
        d = self.est.pnl_por_dia(c.id, anio, mes)
        lineas = [f"🗓 <b>{html.escape(c.nombre)} · {MESES[mes]} {anio}</b>"]
        for dia in sorted(d):
            lineas.append(f"{dia:02d}: {ico(d[dia])} {d[dia]:+,.2f}")
        lineas.append(f"Total: {sum(d.values()):+,.2f} USDT")
        return "\n".join(lineas)

    def texto_anio(self, cuenta, anio: int) -> str:
        c = self.motor.cuenta(cuenta)
        d = self.est.pnl_por_mes(c.id, anio)
        lineas = [f"🗓 <b>{html.escape(c.nombre)} · {anio} por mes</b>"]
        for m in range(1, 13):
            if m in d:
                lineas.append(f"{MESES[m]}: {ico(d[m])} {d[m]:+,.2f}")
        lineas.append(f"Total: {sum(d.values()):+,.2f} USDT")
        return "\n".join(lineas)

    def texto_reporte_diario(self) -> str:
        ini = self.est.rango("ayer")
        lineas = [f"☀️ <b>Reporte diario</b> · {datetime.now(self.est.tz):%d/%m/%Y}"]
        for c in sorted(self.motor.cuentas.values(), key=lambda x: x.id):
            met = self.motor.metricas(c, con_detalle=False)
            r = self.est.resumen(c.id, *ini)
            sem = self.motor.pnl_desde(c, self.v.inicios()["semana"])
            mes = self.motor.pnl_desde(c, self.v.inicios()["mes"])
            lineas.append(
                f"\n💼 <b>{html.escape(c.nombre)}</b>: saldo {usd(c.saldo)}\n"
                f"Ayer: {ico(r['neto'])} {usd(r['neto'], True)}  ({r['operaciones']} cerradas, {r['ganadoras']} ganadoras)\n"
                f"Semana: {usd(sem, True)}  ·  Mes: {usd(mes, True)}\n"
                f"Abiertas: {len(self.motor.posiciones_abiertas(c))} posiciones, PnL {usd(met['upnl'], True)}  ·  "
                f"{len(self.motor.ordenes_abiertas(c.id))} órdenes")
        return "\n".join(lineas)

    # ------------------------------------------------------------ imágenes
    async def grafica_posicion(self, cuenta, simbolo: str, lado: str = "BOTH") -> tuple[bytes, str]:
        c = self.motor.cuenta(cuenta)
        p = c.posiciones.get((simbolo, lado))
        ahora = int(time.time() * 1000)
        if p and abs(p.amt) > 0:
            desde = p.abierta
            d = self.motor.detalle_pos(c, p)
        else:
            desde, d = ahora - 6 * 3600_000, None
        intervalo = graficas.intervalo_para(desde - 20 * 60000, ahora)
        paso = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}[intervalo] * 60000
        inicio = desde - 10 * paso
        velas = await self.m.velas(simbolo, intervalo, inicio, None, min(499, int((ahora - inicio) / paso) + 2))
        lineas = {}
        if d:
            lineas["Entrada"] = (d["entrada"], AZUL)
            lineas["Liq"] = (d["liquidacion"], AMARILLO)
        for o in self.motor.ordenes_abiertas(c.id, simbolo):
            if o.lado_pos != lado or not o.precio_stop:
                continue
            if o.tipo.startswith("TAKE"):
                lineas[f"TP#{o.id}"] = (o.precio_stop, VERDE)
            elif o.tipo.startswith("STOP"):
                lineas[f"SL#{o.id}"] = (o.precio_stop, ROJO)
        for o in self.motor.ordenes_abiertas(c.id, simbolo):
            if o.tipo == "LIMIT":
                lineas[f"Lím#{o.id}"] = (o.precio, GRIS)
        fills = self.motor.db.all("SELECT tiempo, precio, lado FROM llenados WHERE cuenta=? AND simbolo=? AND tiempo>=?",
                                  (c.id, simbolo, inicio))
        marcas = [(f["tiempo"], f["precio"], f["lado"]) for f in fills]
        if d:
            titulo = (f"{simbolo} {'LONG' if d['amt'] > 0 else 'SHORT'} {d['apalancamiento']}x · {intervalo} · "
                      f"PnL {d['upnl']:+,.2f} ({d['roe']:+.2f}%)")
        else:
            titulo = f"{simbolo} · {intervalo}"
        png = await asyncio.get_running_loop().run_in_executor(
            None, lambda: graficas.velas_png(simbolo, velas, self.cfg.zona_horaria, titulo, lineas, marcas))
        return png, titulo

    async def calendario_png(self, cuenta, anio: int, mes: int) -> bytes:
        c = self.motor.cuenta(cuenta)
        d = self.est.pnl_por_dia(c.id, anio, mes)
        return await asyncio.get_running_loop().run_in_executor(
            None, lambda: graficas.calendario_png(anio, mes, d, c.nombre))

    async def curva_png(self, cuenta) -> bytes:
        c = self.motor.cuenta(cuenta)
        pts = self.est.curva_saldo(c.id)
        return await asyncio.get_running_loop().run_in_executor(
            None, lambda: graficas.curva_png(pts, self.cfg.zona_horaria, f"Saldo de {c.nombre}"))
