"""Estructuras de datos compartidas."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class SymbolInfo:
    simbolo: str
    estado: str = "TRADING"
    tick: float = 0.01
    min_precio: float = 0.0
    max_precio: float = 1e12
    paso: float = 0.001
    min_cant: float = 0.001
    max_cant: float = 1e12
    paso_mkt: float = 0.001
    min_cant_mkt: float = 0.001
    max_cant_mkt: float = 1e12
    min_notional: float = 5.0
    mult_arriba: float = 1.05
    mult_abajo: float = 0.95


class BinanceError(Exception):
    """Error con el mismo código y mensaje que daría Binance, más explicación en español."""

    def __init__(self, code: int, msg: str, es: str = ""):
        super().__init__(msg)
        self.code, self.msg, self.es = code, msg, es or msg

    def __str__(self):
        return f"{self.es}\n(Binance: {self.code} {self.msg})"


@dataclass
class Orden:
    id: int = 0
    cuenta: int = 0
    simbolo: str = ""
    lado: str = "BUY"                 # BUY / SELL
    lado_pos: str = "BOTH"            # BOTH / LONG / SHORT
    tipo: str = "MARKET"              # LIMIT, MARKET, STOP, STOP_MARKET, TAKE_PROFIT, TAKE_PROFIT_MARKET, TRAILING_STOP_MARKET
    cantidad: float = 0.0
    precio: float = 0.0
    precio_stop: float = 0.0
    disparo: str = "CONTRACT_PRICE"   # CONTRACT_PRICE (último) / MARK_PRICE (marca)
    tif: str = "GTC"                  # GTC / IOC / FOK / GTX (post only)
    solo_reducir: bool = False
    cerrar_posicion: bool = False
    activacion: Optional[float] = None
    retroceso: Optional[float] = None  # callbackRate en %
    activado: bool = False
    extremo: Optional[float] = None
    disparada: bool = False
    estado: str = "NEW"               # NEW, PARTIALLY_FILLED, FILLED, CANCELED, EXPIRED
    ejecutada: float = 0.0
    precio_prom: float = 0.0
    tp: Optional[float] = None        # TP/SL adjuntos al abrir
    sl: Optional[float] = None
    tpsl_disparo: str = "MARK_PRICE"
    tpsl_creados: bool = False
    cliente_id: str = ""
    origen: str = "manual"            # manual, tp, sl, liquidacion, deslistado, telegram, terminal
    motivo: str = ""
    creada: int = 0
    actualizada: int = 0

    def dict(self):
        return asdict(self)

    @property
    def es_condicional(self):
        return self.tipo in ("STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET")

    @property
    def restante(self):
        return max(0.0, self.cantidad - self.ejecutada)


@dataclass
class Posicion:
    simbolo: str
    lado: str = "BOTH"
    amt: float = 0.0                  # + long, - short
    entrada: float = 0.0
    margen_aislado: float = 0.0
    tipo_margen: str = "CROSSED"
    abierta: int = 0
    pnl: float = 0.0
    comisiones: float = 0.0
    funding: float = 0.0
    cant_max: float = 0.0
    valor_salida: float = 0.0
    cant_salida: float = 0.0
    ultimo_funding: int = 0

    def dict(self):
        return asdict(self)


@dataclass
class Cuenta:
    id: int
    nombre: str
    saldo: float
    maker: float
    taker: float
    cobertura: bool = False
    ajustes: dict = field(default_factory=dict)       # simbolo -> [apalancamiento, tipo_margen]
    posiciones: dict = field(default_factory=dict)    # (simbolo, lado) -> Posicion
