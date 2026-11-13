"""Estadísticas, calendario de PnL, curva de saldo y exportar historial."""
from __future__ import annotations

import csv
import io
import zipfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .motor import INGRESOS_PNL


class Estadisticas:
    def __init__(self, motor, cfg):
        self.motor, self.db, self.cfg = motor, motor.db, cfg
        self.tz = ZoneInfo(cfg.zona_horaria)

    def _ms(self, d: datetime) -> int:
        return int(d.timestamp() * 1000)

    def resumen(self, cuenta, desde: int = 0, hasta: int | None = None) -> dict:
        c = self.motor.cuenta(cuenta)
        hasta = hasta or 2 ** 62
        cerradas = self.db.all("SELECT * FROM posiciones_cerradas WHERE cuenta=? AND cerrada>=? AND cerrada<? ORDER BY cerrada",
                               (c.id, desde, hasta))
        netos = [r["pnl"] - r["comisiones"] + r["funding"] for r in cerradas]
        tx = {r["tipo"]: r["s"] for r in self.db.all(
            "SELECT tipo, SUM(monto) s FROM transacciones WHERE cuenta=? AND tiempo>=? AND tiempo<? GROUP BY tipo",
            (c.id, desde, hasta))}
        ganadoras = [x for x in netos if x > 0]
        mejor = max(cerradas, key=lambda r: r["pnl"] - r["comisiones"] + r["funding"]) if cerradas else None
        peor = min(cerradas, key=lambda r: r["pnl"] - r["comisiones"] + r["funding"]) if cerradas else None
        neto_total = sum(tx.get(t, 0) for t in INGRESOS_PNL)
        return {
            "operaciones": len(cerradas),
            "ganadoras": len(ganadoras),
            "pct_ganadoras": len(ganadoras) / len(cerradas) * 100 if cerradas else 0.0,
            "mejor": mejor, "peor": peor,
            "mejor_neto": max(netos) if netos else 0.0, "peor_neto": min(netos) if netos else 0.0,
            "promedio_ganadora": sum(ganadoras) / len(ganadoras) if ganadoras else 0.0,
            "promedio_perdedora": (sum(x for x in netos if x <= 0) / max(1, len(netos) - len(ganadoras))) if netos else 0.0,
            "pnl_realizado": tx.get("REALIZED_PNL", 0.0),
            "comisiones": tx.get("COMMISSION", 0.0),
            "funding": tx.get("FUNDING_FEE", 0.0),
            "liquidaciones": tx.get("INSURANCE_CLEAR", 0.0),
            "transferencias": tx.get("TRANSFER", 0.0),
            "neto": neto_total,
        }

    def pnl_por_dia(self, cuenta, anio: int, mes: int) -> dict[int, float]:
        c = self.motor.cuenta(cuenta)
        ini = datetime(anio, mes, 1, tzinfo=self.tz)
        fin = datetime(anio + (mes == 12), mes % 12 + 1, 1, tzinfo=self.tz)
        filas = self.db.all(
            f"SELECT tiempo, monto FROM transacciones WHERE cuenta=? AND tiempo>=? AND tiempo<? AND tipo IN ({','.join('?' * len(INGRESOS_PNL))})",
            (c.id, self._ms(ini), self._ms(fin), *INGRESOS_PNL))
        out: dict[int, float] = {}
        for f in filas:
            d = datetime.fromtimestamp(f["tiempo"] / 1000, self.tz).day
            out[d] = out.get(d, 0.0) + f["monto"]
        return out

    def pnl_por_mes(self, cuenta, anio: int) -> dict[int, float]:
        c = self.motor.cuenta(cuenta)
        ini, fin = datetime(anio, 1, 1, tzinfo=self.tz), datetime(anio + 1, 1, 1, tzinfo=self.tz)
        filas = self.db.all(
            f"SELECT tiempo, monto FROM transacciones WHERE cuenta=? AND tiempo>=? AND tiempo<? AND tipo IN ({','.join('?' * len(INGRESOS_PNL))})",
            (c.id, self._ms(ini), self._ms(fin), *INGRESOS_PNL))
        out: dict[int, float] = {}
        for f in filas:
            m = datetime.fromtimestamp(f["tiempo"] / 1000, self.tz).month
            out[m] = out.get(m, 0.0) + f["monto"]
        return out

    def curva_saldo(self, cuenta) -> list[tuple[int, float]]:
        c = self.motor.cuenta(cuenta)
        saldo, pts = 0.0, []
        for f in self.db.all("SELECT tiempo, monto FROM transacciones WHERE cuenta=? ORDER BY tiempo, id", (c.id,)):
            saldo += f["monto"]
            pts.append((f["tiempo"], saldo))
        return pts

    def rango(self, que: str) -> tuple[int, int | None]:
        ahora = datetime.now(self.tz)
        hoy = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
        if que == "hoy":
            return self._ms(hoy), None
        if que == "ayer":
            return self._ms(hoy - timedelta(days=1)), self._ms(hoy)
        if que == "semana":
            return self._ms(hoy - timedelta(days=hoy.weekday())), None
        if que == "mes":
            return self._ms(hoy.replace(day=1)), None
        return 0, None

    # ------------------------------------------------------------ exportar
    TABLAS = {
        "ordenes": "SELECT id, simbolo, estado, creada, actualizada, datos FROM ordenes WHERE cuenta=? ORDER BY id",
        "llenados": "SELECT id, orden, simbolo, lado, lado_pos, cantidad, precio, comision, pnl, maker, tiempo FROM llenados WHERE cuenta=? ORDER BY tiempo",
        "transacciones": "SELECT id, tipo, monto, simbolo, info, tiempo FROM transacciones WHERE cuenta=? ORDER BY tiempo",
        "posiciones_cerradas": "SELECT id, simbolo, lado, entrada, salida, cantidad_max, pnl, comisiones, funding, abierta, cerrada, motivo FROM posiciones_cerradas WHERE cuenta=? ORDER BY cerrada",
    }

    def _filas(self, cid: int, tabla: str) -> list[dict]:
        import json
        filas = self.db.all(self.TABLAS[tabla], (cid,))
        for f in filas:
            for k in ("creada", "actualizada", "tiempo", "abierta", "cerrada"):
                if k in f and f[k]:
                    f[k] = datetime.fromtimestamp(f[k] / 1000, self.tz).strftime("%Y-%m-%d %H:%M:%S")
            if tabla == "ordenes":
                d = json.loads(f.pop("datos") or "{}")
                for k in ("lado", "lado_pos", "tipo", "cantidad", "precio", "precio_stop", "disparo", "tif",
                          "solo_reducir", "cerrar_posicion", "ejecutada", "precio_prom", "origen", "motivo"):
                    f[k] = d.get(k)
        return filas

    def exportar_csv_zip(self, cuenta) -> tuple[str, bytes]:
        c = self.motor.cuenta(cuenta)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for t in self.TABLAS:
                filas = self._filas(c.id, t)
                s = io.StringIO()
                if filas:
                    w = csv.DictWriter(s, fieldnames=list(filas[0].keys()))
                    w.writeheader()
                    w.writerows(filas)
                z.writestr(f"{t}.csv", "﻿" + s.getvalue())
        return f"{c.nombre}-historial-{datetime.now(self.tz):%Y%m%d}.zip", buf.getvalue()

    def exportar_excel(self, cuenta) -> tuple[str, bytes]:
        from openpyxl import Workbook
        from openpyxl.styles import Font
        c = self.motor.cuenta(cuenta)
        wb = Workbook()
        wb.remove(wb.active)
        nombres = {"ordenes": "Órdenes", "llenados": "Llenados", "transacciones": "Transacciones",
                   "posiciones_cerradas": "Posiciones cerradas"}
        for t, titulo in nombres.items():
            ws = wb.create_sheet(titulo)
            filas = self._filas(c.id, t)
            if not filas:
                ws.append(["Sin datos"])
                continue
            ws.append(list(filas[0].keys()))
            for celda in ws[1]:
                celda.font = Font(bold=True)
            for f in filas:
                ws.append([v if not isinstance(v, bool) else ("sí" if v else "no") for v in f.values()])
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = min(28, max(10, max(len(str(x.value or "")) for x in col) + 2))
        r = self.resumen(c.id)
        ws = wb.create_sheet("Resumen", 0)
        for k, v in (("Cuenta", c.nombre), ("Saldo", c.saldo), ("Operaciones cerradas", r["operaciones"]),
                     ("% ganadoras", round(r["pct_ganadoras"], 2)), ("Mejor operación", r["mejor_neto"]),
                     ("Peor operación", r["peor_neto"]), ("PnL realizado", r["pnl_realizado"]),
                     ("Comisiones", r["comisiones"]), ("Funding", r["funding"]), ("Liquidaciones", r["liquidaciones"]),
                     ("Neto (sin transferencias)", r["neto"]), ("Transferencias", r["transferencias"])):
            ws.append([k, v])
        ws.column_dimensions["A"].width = 28
        out = io.BytesIO()
        wb.save(out)
        return f"{c.nombre}-historial-{datetime.now(self.tz):%Y%m%d}.xlsx", out.getvalue()
