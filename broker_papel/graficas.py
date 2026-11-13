"""Imágenes PNG para Telegram: velas de una posición, calendario de PnL y curva de saldo.

matplotlib se carga solo cuando hace falta, para no gastar memoria.
"""
from __future__ import annotations

import calendar
import io
from datetime import datetime
from zoneinfo import ZoneInfo

FONDO, TEXTO, REJILLA = "#0b0e11", "#eaecef", "#2b3139"
VERDE, ROJO, AMARILLO, AZUL, GRIS = "#0ecb81", "#f6465d", "#f0b90b", "#3d8bfd", "#848e9c"
MESES = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre",
         "Octubre", "Noviembre", "Diciembre"]


def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, facecolor=fig.get_facecolor(), bbox_inches="tight")
    _plt().close(fig)
    return buf.getvalue()


def intervalo_para(desde_ms: int, hasta_ms: int, max_velas: int = 300) -> str:
    dur_min = max(1, (hasta_ms - desde_ms) / 60000)
    for nombre, minutos in (("1m", 1), ("5m", 5), ("15m", 15), ("1h", 60), ("4h", 240), ("1d", 1440)):
        if dur_min / minutos <= max_velas:
            return nombre
    return "1d"


def velas_png(simbolo: str, velas: list, tz: str, titulo: str = "", lineas: dict | None = None,
              marcas: list | None = None) -> bytes:
    """velas: filas de Binance [t, o, h, l, c, ...]. lineas: {'Entrada': (precio, color)}.
    marcas: [(t_ms, precio, 'BUY'|'SELL')] para entradas y salidas."""
    plt = _plt()
    zona = ZoneInfo(tz)
    fig, ax = plt.subplots(figsize=(9, 5))
    fig.patch.set_facecolor(FONDO)
    ax.set_facecolor(FONDO)
    n = len(velas)
    if n:
        t0 = int(velas[0][0])
        paso = (int(velas[1][0]) - t0) if n > 1 else 60000
        ancho = 0.7
        for i, v in enumerate(velas):
            o, h, l, c = (float(x) for x in v[1:5])
            col = VERDE if c >= o else ROJO
            ax.vlines(i, l, h, color=col, linewidth=0.8)
            ax.bar(i, abs(c - o) or (h - l) * 0.01 or 1e-9, bottom=min(o, c), width=ancho, color=col, linewidth=0)
        etiquetas = list(range(0, n, max(1, n // 6)))
        ax.set_xticks(etiquetas)
        fmt = "%d/%m %H:%M" if paso < 86400000 else "%d/%m"
        ax.set_xticklabels([datetime.fromtimestamp(int(velas[i][0]) / 1000, zona).strftime(fmt) for i in etiquetas],
                           fontsize=8)
        for t, p, lado in marcas or []:
            i = (t - t0) / paso
            if -1 <= i <= n:
                ax.scatter([i], [p], marker="^" if lado == "BUY" else "v", s=90,
                           color=VERDE if lado == "BUY" else ROJO, edgecolors="white", zorder=5)
        ax.set_xlim(-1, n + max(3, n // 12))
    for nombre, (precio, color) in (lineas or {}).items():
        if precio:
            ax.axhline(precio, color=color, linestyle="--", linewidth=1)
            ax.text(ax.get_xlim()[1], precio, f" {nombre} {precio:g}", color=color, fontsize=8, va="center", ha="right",
                    bbox=dict(facecolor=FONDO, edgecolor="none", pad=1))
    ax.tick_params(colors=TEXTO, labelsize=8)
    ax.yaxis.tick_right()
    for s in ax.spines.values():
        s.set_color(REJILLA)
    ax.grid(color=REJILLA, linewidth=0.5)
    ax.set_title(titulo or simbolo, color=TEXTO, fontsize=11, loc="left")
    return _png(fig)


def calendario_png(anio: int, mes: int, pnl: dict[int, float], titulo: str = "") -> bytes:
    plt = _plt()
    fig, ax = plt.subplots(figsize=(7, 6))
    fig.patch.set_facecolor(FONDO)
    ax.set_facecolor(FONDO)
    ax.set_xlim(0, 7)
    semanas = calendar.Calendar(firstweekday=0).monthdayscalendar(anio, mes)
    ax.set_ylim(len(semanas) + 0.6, 0)
    ax.axis("off")
    for i, d in enumerate(["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]):
        ax.text(i + 0.5, 0.35, d, color=GRIS, ha="center", fontsize=9)
    maximo = max([abs(v) for v in pnl.values()] + [1e-9])
    for fila, semana in enumerate(semanas):
        for col, dia in enumerate(semana):
            if not dia:
                continue
            v = pnl.get(dia)
            y = fila + 0.6
            color = REJILLA
            if v:
                alfa = 0.35 + 0.65 * min(1, abs(v) / maximo)
                color = (VERDE if v > 0 else ROJO)
                ax.add_patch(plt.Rectangle((col + 0.04, y + 0.04), 0.92, 0.92, color=color, alpha=alfa))
            else:
                ax.add_patch(plt.Rectangle((col + 0.04, y + 0.04), 0.92, 0.92, color=REJILLA, alpha=0.6))
            ax.text(col + 0.12, y + 0.25, str(dia), color=TEXTO, fontsize=8)
            if v:
                ax.text(col + 0.5, y + 0.65, f"{v:+,.2f}", color="white", ha="center", fontsize=8, weight="bold")
    total = sum(pnl.values())
    ganadores = sum(1 for v in pnl.values() if v > 0)
    perdedores = sum(1 for v in pnl.values() if v < 0)
    ax.set_title(f"{titulo}  {MESES[mes]} {anio}\nPnL del mes: {total:+,.2f} USDT   ·   días ganadores {ganadores}, perdedores {perdedores}",
                 color=VERDE if total >= 0 else ROJO, fontsize=11)
    return _png(fig)


def curva_png(puntos: list[tuple[int, float]], tz: str, titulo: str = "Saldo") -> bytes:
    plt = _plt()
    zona = ZoneInfo(tz)
    fig, ax = plt.subplots(figsize=(9, 4))
    fig.patch.set_facecolor(FONDO)
    ax.set_facecolor(FONDO)
    if puntos:
        xs = [datetime.fromtimestamp(t / 1000, zona) for t, _ in puntos]
        ys = [s for _, s in puntos]
        ax.step(xs, ys, where="post", color=AMARILLO, linewidth=1.5)
        ax.fill_between(xs, ys, min(ys), step="post", color=AMARILLO, alpha=0.1)
        fig.autofmt_xdate()
    ax.tick_params(colors=TEXTO, labelsize=8)
    for s in ax.spines.values():
        s.set_color(REJILLA)
    ax.grid(color=REJILLA, linewidth=0.5)
    ax.set_title(titulo, color=TEXTO, loc="left")
    return _png(fig)
