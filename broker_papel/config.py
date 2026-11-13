"""Lee config/config.toml y config/secretos.toml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

BASE = Path(os.environ.get("BROKER_HOME", Path(__file__).resolve().parent.parent))


@dataclass
class CuentaCfg:
    nombre: str
    maker: float | None = None
    taker: float | None = None


@dataclass
class Config:
    zona_horaria: str = "America/Mexico_City"
    saldo_inicial: float = 10_000.0
    apalancamiento_inicial: int = 20
    cuentas: list[CuentaCfg] = field(default_factory=list)
    maker: float = 0.0002
    taker: float = 0.0005
    llenado_limite: str = "cruza"           # "cruza" o "toca"
    deslizamiento_base: float = 0.0001      # 0.01 %
    deslizamiento_por_50k: float = 0.0001   # +0.01 % por cada 50,000 USDT
    comision_liquidacion: float = 0.0125
    rest: str = "https://fapi.binance.com"
    ws: str = "wss://fstream.binance.com"
    brackets_url: str = "https://www.binance.com/bapi/futures/v1/friendly/future/common/brackets"
    peso_max_por_minuto: int = 200
    pausa_si_peso_ip_supera: int = 1800
    alerta_margen_ratio: float = 0.80
    alerta_distancia_liq: float = 0.05
    alerta_funding_minimo: float = 1.0
    puerto_control: int = 8765
    hora_respaldo: int = 3
    dias_respaldo: int = 14
    telegram_token: str = ""
    telegram_chats: list[int] = field(default_factory=list)
    data_dir: Path = BASE / "data"


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def cargar(base: Path = BASE) -> Config:
    c = Config()
    raw = _load(base / "config" / "config.toml")
    sec = _load(base / "config" / "secretos.toml")
    g = raw.get("general", {})
    c.zona_horaria = g.get("zona_horaria", c.zona_horaria)
    c.saldo_inicial = float(g.get("saldo_inicial", c.saldo_inicial))
    c.apalancamiento_inicial = int(g.get("apalancamiento_inicial", c.apalancamiento_inicial))
    com = raw.get("comisiones", {})
    c.maker = float(com.get("maker", c.maker))
    c.taker = float(com.get("taker", c.taker))
    for cu in raw.get("cuentas", []) or [{"nombre": "cuenta1"}, {"nombre": "cuenta2"}, {"nombre": "cuenta3"}]:
        c.cuentas.append(CuentaCfg(str(cu["nombre"]), cu.get("maker"), cu.get("taker")))
    s = raw.get("simulacion", {})
    c.llenado_limite = s.get("llenado_limite", c.llenado_limite)
    c.deslizamiento_base = float(s.get("deslizamiento_base", c.deslizamiento_base))
    c.deslizamiento_por_50k = float(s.get("deslizamiento_por_50k", c.deslizamiento_por_50k))
    c.comision_liquidacion = float(s.get("comision_liquidacion", c.comision_liquidacion))
    b = raw.get("binance", {})
    c.rest = b.get("rest", c.rest)
    c.ws = b.get("ws", c.ws)
    c.brackets_url = b.get("brackets_url", c.brackets_url)
    c.peso_max_por_minuto = int(b.get("peso_max_por_minuto", c.peso_max_por_minuto))
    c.pausa_si_peso_ip_supera = int(b.get("pausa_si_peso_ip_supera", c.pausa_si_peso_ip_supera))
    a = raw.get("alertas", {})
    c.alerta_margen_ratio = float(a.get("margen_ratio", c.alerta_margen_ratio))
    c.alerta_distancia_liq = float(a.get("distancia_liquidacion", c.alerta_distancia_liq))
    c.alerta_funding_minimo = float(a.get("funding_minimo", c.alerta_funding_minimo))
    ctl = raw.get("control", {})
    c.puerto_control = int(ctl.get("puerto", c.puerto_control))
    r = raw.get("respaldo", {})
    c.hora_respaldo = int(r.get("hora", c.hora_respaldo))
    c.dias_respaldo = int(r.get("dias_a_guardar", c.dias_respaldo))
    t = sec.get("telegram", {})
    c.telegram_token = str(t.get("token", "")).strip()
    c.telegram_chats = [int(x) for x in t.get("chats_autorizados", [])]
    c.data_dir = base / "data"
    c.data_dir.mkdir(parents=True, exist_ok=True)
    return c
