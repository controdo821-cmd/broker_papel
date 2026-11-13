"""Página web estilo Binance Futures (solo escucha en el VPS; se abre con un túnel SSH)."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from aiohttp import web

from . import calc
from .auth import Auth
from .comandos import Comandos, simb
from .modelos import BinanceError

log = logging.getLogger("web")
WEB = Path(__file__).resolve().parent / "web"
COOKIE = "bp_sesion"

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


@web.middleware
async def cabeceras(request, handler):
    try:
        resp = await handler(request)
    except web.HTTPException as e:
        resp = e
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    if isinstance(resp, web.HTTPException) and resp.status >= 400:
        raise resp
    return resp


class Web:
    def __init__(self, cfg, motor, mercado, vistas, informes, auth: Auth, comandos: Comandos):
        self.cfg, self.motor, self.m, self.v, self.inf, self.auth, self.cmd = cfg, motor, mercado, vistas, informes, auth, comandos
        self._velas_cache: dict = {}

    # ------------------------------------------------------------ utilidades
    def _usuario(self, req) -> dict | None:
        return self.auth.sesion(req.cookies.get(COOKIE))

    def _cuenta(self, req, u: dict, ref) -> int:
        c = self.motor.cuenta(ref if ref not in (None, "") else (u["cuenta"] or 1))
        if u["cuenta"] and c.id != u["cuenta"]:
            raise web.HTTPForbidden(text="Esa cuenta no es tuya.")
        return c.id

    @staticmethod
    def _json(data, status=200):
        return web.json_response(data, status=status, dumps=lambda x: json.dumps(x, default=str))

    def protegido(self, f, post=False):
        async def h(req):
            u = self._usuario(req)
            if not u:
                return self._json({"error": "Sesión vencida. Entra de nuevo."}, 401)
            if post and req.headers.get("X-BP") != "1":
                return self._json({"error": "Petición inválida."}, 400)
            try:
                return await f(req, u)
            except BinanceError as e:
                return self._json({"code": e.code, "msg": e.msg, "error": e.es}, 400)
            except (ValueError, KeyError, TypeError) as e:
                return self._json({"error": f"Datos inválidos: {e}"}, 400)
        return h

    # ------------------------------------------------------------ páginas
    async def p_inicio(self, req):
        if not self._usuario(req):
            raise web.HTTPFound("/login")
        return web.FileResponse(WEB / "index.html")

    async def p_login(self, req):
        return web.FileResponse(WEB / "login.html")

    async def a_login(self, req):
        d = await req.json()
        if req.headers.get("X-BP") != "1":
            return self._json({"error": "Petición inválida."}, 400)
        ip = req.headers.get("X-Forwarded-For", req.remote or "")
        token, msg = self.auth.login(d.get("usuario", ""), d.get("contrasena", ""), d.get("codigo", ""), ip)
        if not token:
            log.warning("Login fallido de '%s' desde %s", d.get("usuario", ""), ip)
            return self._json({"error": msg}, 401)
        r = self._json({"ok": True})
        r.set_cookie(COOKIE, token, httponly=True, samesite="Strict", secure=self.cfg.web_cookie_segura,
                     max_age=24 * 3600, path="/")
        log.info("Login de '%s' desde %s", d.get("usuario"), ip)
        return r

    async def a_logout(self, req):
        self.auth.logout(req.cookies.get(COOKIE))
        r = self._json({"ok": True})
        r.del_cookie(COOKIE, path="/")
        return r

    # ------------------------------------------------------------ lectura
    async def a_yo(self, req, u):
        cuentas = [{"id": c.id, "nombre": c.nombre} for c in sorted(self.motor.cuentas.values(), key=lambda x: x.id)
                   if not u["cuenta"] or c.id == u["cuenta"]]
        return self._json({"usuario": u["usuario"], "cuentas": cuentas, "zona": self.cfg.zona_horaria})

    async def a_simbolos(self, req, u):
        return self._json(sorted(s for s, i in self.m.simbolos.items() if i.estado == "TRADING"))

    async def a_mercado(self, req, u):
        s = simb(req.query["s"])
        si = self.motor.info(s)
        await self.m.asegurar(s)
        cid = self._cuenta(req, u, req.query.get("c"))
        lev, tm = self.motor.ajuste(self.motor.cuentas[cid], s)
        d = self.m.datos_marca(s) or {}
        lib = self.m.libro(s)
        return self._json({
            "s": s, "ultimo": self.m.ultimo(s), "marca": d.get("p"), "indice": d.get("i"), "funding": d.get("r"),
            "proximo_funding": d.get("T"), "bid": lib[0] if lib else None, "ask": lib[1] if lib else None,
            "tick": si.tick, "paso": si.paso, "min_cant": si.min_cant, "min_notional": si.min_notional,
            "max_lev": calc.max_leverage(self.m.brackets(s)), "lev": lev, "tipo_margen": tm,
            "decimales": calc.decimals_of(si.tick),
        })

    async def a_velas(self, req, u):
        s, i = simb(req.query["s"]), req.query.get("i", "15m")
        if i not in ("1m", "5m", "15m", "1h", "4h", "1d"):
            raise ValueError("intervalo")
        self.motor.info(s)
        k = (s, i)
        cache = self._velas_cache.get(k)
        if cache and time.time() - cache[0] < 20:
            filas = cache[1]
        else:
            filas = await self.m.velas(s, i, limite=499)
            self._velas_cache[k] = (time.time(), filas)
            if len(self._velas_cache) > 50:
                self._velas_cache.pop(next(iter(self._velas_cache)))
        return self._json([[int(f[0]) // 1000, float(f[1]), float(f[2]), float(f[3]), float(f[4])] for f in filas])

    async def a_cuenta(self, req, u):
        cid = self._cuenta(req, u, req.query.get("c"))
        c = self.motor.cuentas[cid]
        met = self.motor.metricas(c)
        ini = self.v.inicios()
        met["pnl_hoy"] = self.motor.pnl_desde(c, ini["hoy"])
        met["pnl_semana"] = self.motor.pnl_desde(c, ini["semana"])
        met["pnl_mes"] = self.motor.pnl_desde(c, ini["mes"])
        met["cobertura"] = c.cobertura
        met["maker"], met["taker"] = c.maker, c.taker
        met["ordenes"] = [o.dict() for o in self.motor.ordenes_abiertas(cid)]
        return self._json(met)

    async def a_historial(self, req, u):
        cid = self._cuenta(req, u, req.query.get("c"))
        tipo = req.query.get("tipo", "ordenes")
        lim = min(1000, int(req.query.get("limite", 200)))
        if tipo == "ordenes":
            filas = [json.loads(r["datos"]) for r in self.motor.db.all(
                "SELECT datos FROM ordenes WHERE cuenta=? AND estado NOT IN ('NEW','PARTIALLY_FILLED') ORDER BY actualizada DESC LIMIT ?", (cid, lim))]
        elif tipo == "llenados":
            filas = self.motor.db.all("SELECT * FROM llenados WHERE cuenta=? ORDER BY tiempo DESC LIMIT ?", (cid, lim))
        elif tipo == "transacciones":
            filas = self.motor.db.all("SELECT * FROM transacciones WHERE cuenta=? ORDER BY tiempo DESC, id DESC LIMIT ?", (cid, lim))
        elif tipo == "cerradas":
            filas = self.motor.db.all("SELECT * FROM posiciones_cerradas WHERE cuenta=? ORDER BY cerrada DESC LIMIT ?", (cid, lim))
        else:
            raise ValueError("tipo")
        return self._json(filas)

    async def a_estadisticas(self, req, u):
        cid = self._cuenta(req, u, req.query.get("c"))
        desde, hasta = self.inf.est.rango(req.query.get("rango", "todo"))
        r = self.inf.est.resumen(cid, desde, hasta)
        r["curva"] = [[t // 1000, s] for t, s in self.inf.est.curva_saldo(cid)]
        return self._json(r)

    async def a_calendario(self, req, u):
        cid = self._cuenta(req, u, req.query.get("c"))
        anio, mes = int(req.query["anio"]), int(req.query["mes"])
        return self._json({"dias": self.inf.est.pnl_por_dia(cid, anio, mes),
                           "meses": self.inf.est.pnl_por_mes(cid, anio)})

    async def a_exportar(self, req, u):
        cid = self._cuenta(req, u, req.query.get("c"))
        if req.query.get("f") == "csv":
            nombre, datos = self.inf.est.exportar_csv_zip(cid)
            ct = "application/zip"
        else:
            nombre, datos = self.inf.est.exportar_excel(cid)
            ct = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        return web.Response(body=datos, content_type=ct,
                            headers={"Content-Disposition": f'attachment; filename="{nombre}"'})

    # ------------------------------------------------------------ acciones
    async def a_orden(self, req, u):
        d = await req.json()
        cid = self._cuenta(req, u, d.get("c"))
        c = self.motor.cuentas[cid]
        s = simb(d["s"])
        si = self.motor.info(s)
        await self.m.asegurar(s)
        tipo = d["tipo"]
        f = lambda k: float(d[k]) if d.get(k) not in (None, "") else None
        precio, stop = f("precio"), f("stop")
        cantidad = f("cantidad")
        if cantidad is None and f("monto"):
            ref = precio or stop or self.motor.ultimo(s)
            cantidad = calc.round_step(f("monto") * self.motor.ajuste(c, s)[0] / ref, si.paso, "down")
        lado_pos = d.get("lado_pos") or None
        if c.cobertura and not lado_pos:
            lado_pos = "LONG" if (d["lado"] == "BUY") != bool(d.get("reducir")) else "SHORT"
        o = self.motor.colocar(
            cid, s, d["lado"], tipo, cantidad=None if d.get("cerrar") else cantidad, precio=precio, precio_stop=stop,
            lado_pos=lado_pos, tif=d.get("tif", "GTC"), solo_reducir=bool(d.get("reducir")) and not c.cobertura,
            cerrar_posicion=bool(d.get("cerrar")), disparo=d.get("disparo", "CONTRACT_PRICE"),
            activacion=f("activacion"), retroceso=f("retroceso"), tp=f("tp"), sl=f("sl"),
            tpsl_disparo=d.get("tpsl_disparo", "MARK_PRICE"), origen="web")
        return self._json({"ok": True, "orden": o.dict(), "texto": self.cmd._resultado(o)})

    async def a_cancelar(self, req, u):
        d = await req.json()
        cid = self._cuenta(req, u, d.get("c"))
        if d.get("id") == "todas":
            return self._json({"ok": True, "n": self.motor.cancelar_todas(cid, d.get("s") or None)})
        self.motor.cancelar(cid, int(d["id"]))
        return self._json({"ok": True})

    async def a_cerrar(self, req, u):
        d = await req.json()
        cid = self._cuenta(req, u, d.get("c"))
        await self.m.asegurar(simb(d["s"]))
        lp = d.get("lado_pos")
        o = self.motor.cerrar_posicion(cid, d["s"], float(d.get("pct", 100)), None if lp in (None, "BOTH") else lp, origen="web")
        return self._json({"ok": True, "texto": self.cmd._resultado(o)})

    async def a_tpsl(self, req, u):
        d = await req.json()
        cid = self._cuenta(req, u, d.get("c"))
        lp = d.get("lado_pos")
        f = lambda k: float(d[k]) if d.get(k) not in (None, "", "no") else None
        self.motor.poner_tpsl(cid, d["s"], tp=f("tp"), sl=f("sl"), lado_pos=None if lp in (None, "BOTH") else lp,
                              disparo=d.get("disparo", "MARK_PRICE"), quitar_tp=d.get("tp") == "no", quitar_sl=d.get("sl") == "no")
        return self._json({"ok": True})

    async def a_ajuste(self, req, u):
        d = await req.json()
        cid = self._cuenta(req, u, d.get("c"))
        que = d["que"]
        if que == "lev":
            self.motor.cambiar_apalancamiento(cid, simb(d["s"]), int(d["valor"]))
        elif que == "margen":
            self.motor.cambiar_tipo_margen(cid, simb(d["s"]), d["valor"])
        elif que == "modo":
            self.motor.cambiar_modo_posicion(cid, bool(d["valor"]))
        elif que == "margen_aislado":
            lp = d.get("lado_pos")
            self.motor.margen_aislado(cid, simb(d["s"]), float(d["valor"]), None if lp in (None, "BOTH") else lp)
        else:
            raise ValueError("que")
        return self._json({"ok": True})

    async def a_transferir(self, req, u):
        d = await req.json()
        cid = self._cuenta(req, u, d.get("c"))
        monto = float(d["monto"])
        saldo = self.motor.transferir(cid, monto, ("Recarga" if monto > 0 else "Retiro") + " (web)")
        return self._json({"ok": True, "saldo": saldo})

    async def a_reiniciar(self, req, u):
        d = await req.json()
        cid = self._cuenta(req, u, d.get("c"))
        if d.get("confirmar") != "REINICIAR":
            raise ValueError("Escribe REINICIAR para confirmar")
        self.motor.reiniciar_cuenta(cid, float(d["saldo"]) if d.get("saldo") not in (None, "") else None)
        return self._json({"ok": True})

    # ------------------------------------------------------------ app
    def app(self) -> web.Application:
        a = web.Application(middlewares=[cabeceras], client_max_size=64 * 1024)
        p = self.protegido
        a.router.add_get("/", self.p_inicio)
        a.router.add_get("/login", self.p_login)
        a.router.add_post("/api/login", self.a_login)
        a.router.add_post("/api/logout", self.a_logout)
        a.router.add_get("/api/yo", p(self.a_yo))
        a.router.add_get("/api/simbolos", p(self.a_simbolos))
        a.router.add_get("/api/mercado", p(self.a_mercado))
        a.router.add_get("/api/velas", p(self.a_velas))
        a.router.add_get("/api/cuenta", p(self.a_cuenta))
        a.router.add_get("/api/historial", p(self.a_historial))
        a.router.add_get("/api/estadisticas", p(self.a_estadisticas))
        a.router.add_get("/api/calendario", p(self.a_calendario))
        a.router.add_get("/api/exportar", p(self.a_exportar))
        for ruta, f in (("orden", self.a_orden), ("cancelar", self.a_cancelar), ("cerrar", self.a_cerrar),
                        ("tpsl", self.a_tpsl), ("ajuste", self.a_ajuste), ("transferir", self.a_transferir),
                        ("reiniciar", self.a_reiniciar)):
            a.router.add_post(f"/api/{ruta}", p(f, post=True))
        a.router.add_static("/static/", WEB, show_index=False)
        return a

    async def iniciar(self):
        runner = web.AppRunner(self.app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, self.cfg.web_host, self.cfg.web_puerto).start()
        log.info("Web en http://%s:%s", self.cfg.web_host, self.cfg.web_puerto)
        return runner
