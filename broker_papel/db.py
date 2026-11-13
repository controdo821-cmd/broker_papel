"""Base de datos SQLite: todo lo que el broker necesita para no perder nada."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS cuentas (
  id INTEGER PRIMARY KEY, nombre TEXT UNIQUE, saldo REAL, maker REAL, taker REAL,
  cobertura INTEGER DEFAULT 0, creada INTEGER
);
CREATE TABLE IF NOT EXISTS ajustes_simbolo (
  cuenta INTEGER, simbolo TEXT, apalancamiento INTEGER, tipo_margen TEXT,
  PRIMARY KEY (cuenta, simbolo)
);
CREATE TABLE IF NOT EXISTS posiciones (
  cuenta INTEGER, simbolo TEXT, lado TEXT, datos TEXT,
  PRIMARY KEY (cuenta, simbolo, lado)
);
CREATE TABLE IF NOT EXISTS ordenes (
  id INTEGER PRIMARY KEY, cuenta INTEGER, simbolo TEXT, estado TEXT, creada INTEGER,
  actualizada INTEGER, datos TEXT
);
CREATE INDEX IF NOT EXISTS ix_ord_estado ON ordenes(estado);
CREATE INDEX IF NOT EXISTS ix_ord_cuenta ON ordenes(cuenta, creada);
CREATE TABLE IF NOT EXISTS llenados (
  id INTEGER PRIMARY KEY AUTOINCREMENT, orden INTEGER, cuenta INTEGER, simbolo TEXT,
  lado TEXT, lado_pos TEXT, cantidad REAL, precio REAL, comision REAL, pnl REAL,
  maker INTEGER, tiempo INTEGER
);
CREATE INDEX IF NOT EXISTS ix_fill ON llenados(cuenta, tiempo);
CREATE TABLE IF NOT EXISTS transacciones (
  id INTEGER PRIMARY KEY AUTOINCREMENT, cuenta INTEGER, tipo TEXT, monto REAL,
  simbolo TEXT, info TEXT, tiempo INTEGER
);
CREATE INDEX IF NOT EXISTS ix_tx ON transacciones(cuenta, tiempo);
CREATE TABLE IF NOT EXISTS posiciones_cerradas (
  id INTEGER PRIMARY KEY AUTOINCREMENT, cuenta INTEGER, simbolo TEXT, lado TEXT,
  entrada REAL, salida REAL, cantidad_max REAL, pnl REAL, comisiones REAL, funding REAL,
  abierta INTEGER, cerrada INTEGER, motivo TEXT
);
CREATE INDEX IF NOT EXISTS ix_pc ON posiciones_cerradas(cuenta, cerrada);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS usuarios (
  usuario TEXT PRIMARY KEY, hash TEXT, cuenta INTEGER, totp TEXT, fallos INTEGER DEFAULT 0,
  bloqueado_hasta INTEGER DEFAULT 0, creado INTEGER
);
CREATE TABLE IF NOT EXISTS sesiones (token TEXT PRIMARY KEY, usuario TEXT, creada INTEGER, ultimo INTEGER, ip TEXT);
CREATE TABLE IF NOT EXISTS llaves_api (llave TEXT PRIMARY KEY, secreto TEXT, cuenta INTEGER, creada INTEGER);
"""


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.con = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        self.con.execute("PRAGMA synchronous=NORMAL")

    def ex(self, sql, args=()):
        return self.con.execute(sql, args)

    def all(self, sql, args=()):
        return [dict(r) for r in self.con.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        r = self.con.execute(sql, args).fetchone()
        return dict(r) if r else None

    # ---- kv ----
    def get(self, k, default=None):
        r = self.one("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r["v"]) if r else default

    def set(self, k, v):
        self.ex("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, json.dumps(v)))

    # ---- respaldo ----
    def respaldo(self, carpeta: Path, dias: int = 14) -> Path:
        carpeta.mkdir(parents=True, exist_ok=True)
        destino = carpeta / time.strftime("broker-%Y%m%d-%H%M%S.db")
        dst = sqlite3.connect(str(destino))
        with dst:
            self.con.backup(dst)
        dst.close()
        limite = time.time() - dias * 86400
        for f in carpeta.glob("broker-*.db"):
            if f.stat().st_mtime < limite:
                f.unlink(missing_ok=True)
        return destino
