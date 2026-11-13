"""Panel de Telegram: menú con botones, operar paso a paso y alertas."""
from __future__ import annotations

import asyncio
import html
import logging
import secrets

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as K, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from . import calc
from .comandos import Comandos, simb, num
from .modelos import BinanceError

log = logging.getLogger("telegram")


def menu_kb():
    return K([
        [B("💼 Resumen", callback_data="m:resumen"), B("📈 Posiciones", callback_data="m:pos")],
        [B("📋 Órdenes", callback_data="m:ord"), B("➕ Abrir operación", callback_data="m:abrir")],
        [B("💲 Precio", callback_data="m:precio"), B("📜 Hoy", callback_data="m:hist")],
        [B("📊 Estadísticas", callback_data="st:todo"), B("🗓 Calendario", callback_data="cal:0:0")],
        [B("💰 Recargar", callback_data="m:recargar"), B("🏧 Retirar", callback_data="m:retirar")],
        [B("📤 Exportar", callback_data="m:exportar"), B("📉 Curva de saldo", callback_data="m:curva")],
        [B("👤 Cambiar cuenta", callback_data="m:cuenta"), B("⚙️ Ajustes", callback_data="m:ajustes")],
    ])


VOLVER = [B("🏠 Menú", callback_data="m:menu")]


class BotTelegram:
    def __init__(self, cfg, motor, mercado, vistas, comandos: Comandos, informes=None):
        self.cfg, self.motor, self.m, self.v, self.cmd = cfg, motor, mercado, vistas, comandos
        self.inf = informes or comandos.inf
        self.app: Application | None = None
        self.chats = set(cfg.telegram_chats)
        self.flujo: dict[int, dict] = {}
        self.pendientes: dict[str, tuple] = {}
        motor.oyentes.append(self._evento)

    # ------------------------------------------------------------ arranque
    async def iniciar(self):
        self.app = Application.builder().token(self.cfg.telegram_token).build()
        self.app.add_handler(CommandHandler(["start", "menu"], self.cmd_start))
        self.app.add_handler(CommandHandler("c", self.cmd_texto))
        self.app.add_handler(CommandHandler("id", self.cmd_id))
        self.app.add_handler(CallbackQueryHandler(self.boton))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.texto))
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("Bot de Telegram iniciado. Chats autorizados: %d", len(self.chats))
        await self.enviar_a_todos("🟢 Broker Papel en línea.", menu=True)

    async def detener(self):
        if self.app:
            try:
                await self.app.updater.stop()
                await self.app.stop()
                await self.app.shutdown()
            except Exception:
                pass

    # ------------------------------------------------------------ utilidades
    def _ok(self, update: Update) -> bool:
        return update.effective_chat and update.effective_chat.id in self.chats

    def cuenta_de(self, chat: int):
        cid = self.motor.db.get(f"tg_cuenta_{chat}")
        if cid in self.motor.cuentas:
            return self.motor.cuentas[cid]
        return sorted(self.motor.cuentas.values(), key=lambda c: c.id)[0]

    async def enviar(self, chat: int, texto: str, kb=None, menu=False):
        if menu and kb is None:
            kb = K([VOLVER])
        for i in range(0, max(len(texto), 1), 3900):
            parte = texto[i:i + 3900]
            ult = i + 3900 >= len(texto)
            try:
                await self.app.bot.send_message(chat, parte, parse_mode=ParseMode.HTML,
                                                reply_markup=kb if ult else None, disable_web_page_preview=True)
            except TelegramError as e:
                log.warning("No se pudo enviar a %s: %s", chat, e)

    async def enviar_foto(self, chat: int, png: bytes, pie: str = "", kb=None):
        try:
            await self.app.bot.send_photo(chat, png, caption=pie[:1000], parse_mode=ParseMode.HTML, reply_markup=kb)
        except TelegramError as e:
            log.warning("No se pudo enviar foto a %s: %s", chat, e)

    async def enviar_foto_a_todos(self, png: bytes, pie: str = ""):
        for ch in self.chats:
            await self.enviar_foto(ch, png, pie)

    async def enviar_doc(self, chat: int, nombre: str, datos: bytes, pie: str = ""):
        try:
            await self.app.bot.send_document(chat, datos, filename=nombre, caption=pie[:1000])
        except TelegramError as e:
            log.warning("No se pudo enviar archivo a %s: %s", chat, e)

    async def enviar_a_todos(self, texto: str, menu=False):
        for ch in self.chats:
            await self.enviar(ch, texto, kb=menu_kb() if menu else None)

    def _evento(self, ev: dict):
        if not self.app:
            return
        if ev.get("silenciosa"):
            return
        t = self.v.evento(ev)
        if t:
            try:
                asyncio.get_running_loop().create_task(self.enviar_a_todos(t))
            except RuntimeError:
                pass

    def confirmar(self, accion: tuple) -> str:
        tok = secrets.token_hex(4)
        self.pendientes[tok] = accion
        if len(self.pendientes) > 200:
            self.pendientes.pop(next(iter(self.pendientes)))
        return tok

    def kb_confirmar(self, tok: str):
        return K([[B("✅ Confirmar", callback_data=f"ok:{tok}"), B("✖️ Cancelar", callback_data=f"no:{tok}")]])

    # ------------------------------------------------------------ comandos
    async def cmd_id(self, update: Update, ctx):
        await update.message.reply_text(f"Tu chat id es: {update.effective_chat.id}")

    async def cmd_start(self, update: Update, ctx):
        if not self._ok(update):
            await update.message.reply_text(
                f"⛔ No autorizado. Tu chat id es {update.effective_chat.id}.\n"
                "Agrégalo en config/secretos.toml y reinicia el broker.")
            return
        c = self.cuenta_de(update.effective_chat.id)
        self.flujo.pop(update.effective_chat.id, None)
        await self.enviar(update.effective_chat.id, f"🏦 <b>Broker Papel</b>\nCuenta activa: <b>{html.escape(c.nombre)}</b>",
                          kb=menu_kb())

    async def cmd_texto(self, update: Update, ctx):
        if not self._ok(update):
            return
        linea = update.message.text.partition(" ")[2]
        r = await self.cmd.ejecutar(linea, origen="telegram")
        await self.enviar(update.effective_chat.id, r, menu=True)

    # ------------------------------------------------------------ botones
    async def boton(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not self._ok(update):
            await q.answer("No autorizado")
            return
        await q.answer()
        chat = update.effective_chat.id
        d = q.data or ""
        c = self.cuenta_de(chat)
        try:
            await self._boton(chat, c, d)
        except BinanceError as e:
            await self.enviar(chat, "❌ " + html.escape(str(e)), menu=True)
        except Exception as e:
            log.exception("botón %s", d)
            await self.enviar(chat, f"❌ Error: {html.escape(str(e))}", menu=True)

    async def _boton(self, chat: int, c, d: str):
        partes = d.split(":")
        k = partes[0]
        if k == "m":
            await self._menu(chat, c, partes[1])
        elif k == "cu":
            self.motor.db.set(f"tg_cuenta_{chat}", int(partes[1]))
            c = self.cuenta_de(chat)
            await self.enviar(chat, f"✅ Cuenta activa: <b>{html.escape(c.nombre)}</b>", kb=menu_kb())
        elif k == "pc":   # cerrar posición: pc:SIMB:LADO:PCT
            s, lado, p = partes[1], partes[2], float(partes[3])
            tok = self.confirmar(("cerrar", c.id, s, lado, p))
            await self.enviar(chat, f"¿Cerrar {p:g}% de {s} {lado if lado != 'BOTH' else ''} en <b>{html.escape(c.nombre)}</b> a mercado?",
                              kb=self.kb_confirmar(tok))
        elif k == "pt":   # TP/SL de posición
            s, lado = partes[1], partes[2]
            self.flujo[chat] = {"tipo": "tpsl", "paso": "tp", "s": s, "lado": lado, "cuenta": c.id}
            pos = c.posiciones.get((s, lado))
            await self.enviar(chat, f"🎯 TP/SL de {s}. Entrada {self.v.precio(s, pos.entrada if pos else None)}, "
                                    f"marca {self.v.precio(s, self.m.marca(s))}.\n\n"
                                    "Escribe el precio del <b>Take Profit</b>, o un % (ej. <code>5%</code>), "
                                    "<code>no</code> para quitarlo o <code>-</code> para dejarlo igual.")
        elif k == "pg":   # gráfica de la posición
            await self.enviar(chat, "⏳ Armando la gráfica…")
            png, titulo = await self.inf.grafica_posicion(c.id, partes[1], partes[2])
            await self.enviar_foto(chat, png, html.escape(titulo), kb=K([VOLVER]))
        elif k == "st":   # estadísticas
            r = partes[1]
            filas = [[B(("• " if x == r else "") + n, callback_data=f"st:{x}") for x, n in
                      (("hoy", "Hoy"), ("semana", "Semana"), ("mes", "Mes"), ("todo", "Todo"))], VOLVER]
            await self.enviar(chat, self.inf.texto_estadisticas(c.id, r), kb=K(filas))
        elif k == "cal":  # calendario: cal:AAAA:MM (0:0 = mes actual)
            from datetime import datetime
            anio, mes = int(partes[1]), int(partes[2])
            if not anio:
                hoy = datetime.now(self.inf.est.tz)
                anio, mes = hoy.year, hoy.month
            ant = (anio, mes - 1) if mes > 1 else (anio - 1, 12)
            sig = (anio, mes + 1) if mes < 12 else (anio + 1, 1)
            kb = K([[B("◀️", callback_data=f"cal:{ant[0]}:{ant[1]}"), B("Año", callback_data=f"ano:{anio}"),
                     B("▶️", callback_data=f"cal:{sig[0]}:{sig[1]}")], VOLVER])
            png = await self.inf.calendario_png(c.id, anio, mes)
            await self.enviar_foto(chat, png, f"Calendario de PnL · {html.escape(c.nombre)}", kb=kb)
        elif k == "ano":
            await self.enviar(chat, self.inf.texto_anio(c.id, int(partes[1])), menu=True)
        elif k == "ex":
            if partes[1] == "csv":
                nombre, datos = self.inf.est.exportar_csv_zip(c.id)
            else:
                nombre, datos = self.inf.est.exportar_excel(c.id)
            await self.enviar_doc(chat, nombre, datos, f"Historial de {c.nombre}")
        elif k == "oc":   # cancelar orden
            tok = self.confirmar(("cancelar", c.id, int(partes[1])))
            await self.enviar(chat, f"¿Cancelar la orden #{partes[1]}?", kb=self.kb_confirmar(tok))
        elif k == "oa":
            tok = self.confirmar(("cancelar_todas", c.id))
            await self.enviar(chat, f"¿Cancelar TODAS las órdenes de {html.escape(c.nombre)}?", kb=self.kb_confirmar(tok))
        elif k == "a":    # pasos del asistente de abrir
            await self._abrir_boton(chat, c, partes[1], partes[2])
        elif k == "aj":
            if partes[1] == "modo":
                cob = not c.cobertura
                tok = self.confirmar(("modo", c.id, cob))
                await self.enviar(chat, f"¿Cambiar {html.escape(c.nombre)} a modo <b>{'cobertura' if cob else 'unidireccional'}</b>?",
                                  kb=self.kb_confirmar(tok))
        elif k == "ok":
            acc = self.pendientes.pop(partes[1], None)
            if not acc:
                await self.enviar(chat, "Esa confirmación ya no es válida.", menu=True)
                return
            await self._ejecutar(chat, acc)
        elif k == "no":
            self.pendientes.pop(partes[1], None)
            self.flujo.pop(chat, None)
            await self.enviar(chat, "Cancelado.", kb=menu_kb())

    async def _menu(self, chat: int, c, que: str):
        self.flujo.pop(chat, None)
        if que == "menu":
            await self.enviar(chat, f"🏦 Cuenta activa: <b>{html.escape(c.nombre)}</b>", kb=menu_kb())
        elif que == "cuenta":
            filas = [[B(("✅ " if x.id == c.id else "") + x.nombre, callback_data=f"cu:{x.id}")]
                     for x in sorted(self.motor.cuentas.values(), key=lambda x: x.id)]
            await self.enviar(chat, "Elige la cuenta:", kb=K(filas))
        elif que == "resumen":
            await self.enviar(chat, self.v.resumen(c.id), kb=K([[B("🔄 Actualizar", callback_data="m:resumen")], VOLVER]))
        elif que == "pos":
            ps = self.v.posiciones(c.id)
            if not ps:
                await self.enviar(chat, f"Sin posiciones abiertas en {html.escape(c.nombre)}.", menu=True)
                return
            for d, t in ps:
                s, lado = d["simbolo"], d["lado"]
                kb = K([
                    [B("Cerrar 25%", callback_data=f"pc:{s}:{lado}:25"), B("50%", callback_data=f"pc:{s}:{lado}:50"),
                     B("100%", callback_data=f"pc:{s}:{lado}:100")],
                    [B("🎯 Poner / mover TP-SL", callback_data=f"pt:{s}:{lado}"),
                     B("📉 Gráfica", callback_data=f"pg:{s}:{lado}")],
                ])
                await self.enviar(chat, t, kb=kb)
            await self.enviar(chat, "—", kb=K([[B("🔄 Actualizar", callback_data="m:pos")], VOLVER]))
        elif que == "ord":
            os_ = self.motor.ordenes_abiertas(c.id)
            if not os_:
                await self.enviar(chat, "Sin órdenes abiertas.", menu=True)
                return
            filas = [[B(f"❌ #{o.id} {o.simbolo}", callback_data=f"oc:{o.id}")] for o in os_[:40]]
            filas.append([B("❌ Cancelar todas", callback_data="oa:0")])
            filas.append(VOLVER)
            await self.enviar(chat, "\n\n".join(self.v.orden(o) for o in os_), kb=K(filas))
        elif que == "hist":
            await self.enviar(chat, self.v.historial_hoy(c.id), menu=True)
        elif que == "precio":
            self.flujo[chat] = {"tipo": "precio"}
            await self.enviar(chat, "Escribe la moneda (ej. <code>BTC</code> o <code>SOLUSDT</code>):")
        elif que in ("recargar", "retirar"):
            self.flujo[chat] = {"tipo": que, "cuenta": c.id}
            await self.enviar(chat, f"Escribe el monto a {'recargar' if que == 'recargar' else 'retirar'} en USDT "
                                    f"(cuenta {html.escape(c.nombre)}):")
        elif que == "abrir":
            self.flujo[chat] = {"tipo": "abrir", "paso": "moneda", "cuenta": c.id}
            await self.enviar(chat, f"➕ Nueva operación en <b>{html.escape(c.nombre)}</b>\n\nEscribe la moneda (ej. <code>BTC</code>):",
                              kb=K([[B("✖️ Cancelar", callback_data="m:menu")]]))
        elif que == "exportar":
            await self.enviar(chat, f"📤 Exportar historial de {html.escape(c.nombre)}:",
                              kb=K([[B("Excel", callback_data="ex:xlsx"), B("CSV (zip)", callback_data="ex:csv")], VOLVER]))
        elif que == "curva":
            png = await self.inf.curva_png(c.id)
            await self.enviar_foto(chat, png, f"Saldo de {html.escape(c.nombre)}", kb=K([VOLVER]))
        elif que == "ajustes":
            t = (f"⚙️ <b>{html.escape(c.nombre)}</b>\nModo: {'cobertura' if c.cobertura else 'unidireccional'}\n"
                 f"Comisiones: maker {c.maker * 100:.4f}%  ·  taker {c.taker * 100:.4f}%\n\n"
                 f"Reporte diario: {self.motor.db.get('reporte_hora') or 'apagado'} (cámbialo con <code>/c reporte 08:00</code>)\n\n"
                 "Más opciones escribiendo <code>/c ayuda</code>")
            await self.enviar(chat, t, kb=K([[B("🔁 Cambiar modo de posición", callback_data="aj:modo")], VOLVER]))

    # ------------------------------------------------------------ texto libre (pasos de flujos)
    async def texto(self, update: Update, ctx):
        if not self._ok(update):
            await update.message.reply_text(f"⛔ No autorizado. Tu chat id es {update.effective_chat.id}.")
            return
        chat = update.effective_chat.id
        t = (update.message.text or "").strip()
        f = self.flujo.get(chat)
        if not f:
            await self.enviar(chat, "Usa el menú 👇", kb=menu_kb())
            return
        try:
            await self._texto_flujo(chat, f, t)
        except BinanceError as e:
            await self.enviar(chat, "❌ " + html.escape(str(e)) + "\n\nIntenta de nuevo o pulsa Menú.", menu=True)
        except ValueError:
            await self.enviar(chat, "❌ No entendí el número. Intenta de nuevo.", menu=True)

    async def _texto_flujo(self, chat: int, f: dict, t: str):
        tipo = f["tipo"]
        if tipo == "precio":
            self.flujo.pop(chat, None)
            await self.enviar(chat, await self.cmd.ejecutar(["precio", t]), menu=True)
        elif tipo in ("recargar", "retirar"):
            monto = abs(num(t))
            tok = self.confirmar((tipo, f["cuenta"], monto))
            self.flujo.pop(chat, None)
            await self.enviar(chat, f"¿{'Recargar' if tipo == 'recargar' else 'Retirar'} {monto:,.2f} USDT?",
                              kb=self.kb_confirmar(tok))
        elif tipo == "tpsl":
            await self._tpsl_texto(chat, f, t)
        elif tipo == "abrir":
            await self._abrir_texto(chat, f, t)

    def _precio_rel(self, s: str, t: str, base: float, largo: bool, es_tp: bool) -> float:
        si = self.motor.info(s)
        if t.endswith("%"):
            p = num(t[:-1]) / 100
            sube = largo == es_tp
            val = base * (1 + p) if sube else base * (1 - p)
        else:
            val = num(t)
        return calc.round_step(val, si.tick, "near")

    async def _tpsl_texto(self, chat, f, t):
        c = self.motor.cuentas[f["cuenta"]]
        pos = c.posiciones.get((f["s"], f["lado"]))
        if not pos or abs(pos.amt) <= 0:
            self.flujo.pop(chat, None)
            await self.enviar(chat, "Esa posición ya no está abierta.", menu=True)
            return
        largo = pos.amt > 0
        tl = t.lower()
        val = None if tl == "-" else ("no" if tl == "no" else self._precio_rel(f["s"], t, pos.entrada, largo, f["paso"] == "tp"))
        f[f["paso"]] = val
        if f["paso"] == "tp":
            f["paso"] = "sl"
            await self.enviar(chat, "Ahora el precio del <b>Stop Loss</b> (o %, <code>no</code> o <code>-</code>):")
            return
        self.flujo.pop(chat, None)
        tp, sl = f.get("tp"), f.get("sl")
        txt = lambda v: "sin cambio" if v is None else ("quitar" if v == "no" else self.v.precio(f["s"], v))
        tok = self.confirmar(("tpsl", c.id, f["s"], f["lado"], tp, sl))
        await self.enviar(chat, f"🎯 {f['s']}\nTP: {txt(tp)}\nSL: {txt(sl)}\n(se disparan con precio de marca)\n\n¿Confirmas?",
                          kb=self.kb_confirmar(tok))

    # ------------------------------------------------------------ asistente "abrir operación"
    async def _abrir_texto(self, chat, f, t):
        paso = f["paso"]
        c = self.motor.cuentas[f["cuenta"]]
        if paso == "moneda":
            s = simb(t)
            self.motor.info(s)
            await self.m.asegurar(s)
            f["s"] = s
            lev, tm = self.motor.ajuste(c, s)
            f["lev"], f["tm"] = lev, tm
            f["paso"] = "lado"
            mx = calc.max_leverage(self.m.brackets(s))
            await self.enviar(chat, f"<b>{s}</b>  último {self.v.precio(s, self.motor.ultimo(s))}  ·  marca "
                                    f"{self.v.precio(s, self.m.marca(s))}\nApalancamiento máx {mx}x\n\n¿Long o Short?",
                              kb=K([[B("🟢 Long", callback_data="a:lado:long"), B("🔴 Short", callback_data="a:lado:short")],
                                    [B("✖️ Cancelar", callback_data="m:menu")]]))
        elif paso == "precio":
            si = self.motor.info(f["s"])
            f["precio"] = calc.round_step(num(t), si.tick, "near")
            await self._pedir_lev(chat, f)
        elif paso == "lev":
            await self._abrir_boton(chat, c, "lev", t.lower().lstrip("x"))
        elif paso == "monto":
            ref = f.get("precio") or self.motor.ultimo(f["s"])
            f["cant"] = self.cmd.cantidad_desde_monto(c.id, f["s"], t, f["lev"], ref)
            f["paso"] = "tp"
            await self.enviar(chat, "Precio del <b>Take Profit</b> (o % como <code>5%</code>, o <code>no</code>):")
        elif paso == "tp":
            base = f.get("precio") or self.motor.ultimo(f["s"])
            f["tp"] = None if t.lower() == "no" else self._precio_rel(f["s"], t, base, f["largo"], True)
            f["paso"] = "sl"
            await self.enviar(chat, "Precio del <b>Stop Loss</b> (o %, o <code>no</code>):")
        elif paso == "sl":
            base = f.get("precio") or self.motor.ultimo(f["s"])
            f["sl"] = None if t.lower() == "no" else self._precio_rel(f["s"], t, base, f["largo"], False)
            await self._resumen_abrir(chat, f)

    async def _pedir_lev(self, chat, f):
        f["paso"] = "lev"
        mx = calc.max_leverage(self.m.brackets(f["s"]))
        ops = [x for x in (3, 5, 10, 20, 50, 75, 100, 125) if x <= mx]
        filas = [[B(f"{x}x" + (" ✅" if x == f["lev"] else ""), callback_data=f"a:lev:{x}") for x in ops[i:i + 4]]
                 for i in range(0, len(ops), 4)]
        filas.append([B(f"Dejar {f['lev']}x", callback_data=f"a:lev:{f['lev']}")])
        await self.enviar(chat, f"Apalancamiento (1 a {mx}x). Pulsa uno o escribe el número:", kb=K(filas))

    async def _abrir_boton(self, chat, c, paso, val):
        f = self.flujo.get(chat)
        if not f or f.get("tipo") != "abrir":
            await self.enviar(chat, "Ese asistente ya terminó. Empieza de nuevo.", kb=menu_kb())
            return
        if paso == "lado":
            f["largo"] = val == "long"
            f["paso"] = "tipo"
            await self.enviar(chat, "Tipo de orden:", kb=K([[B("⚡ Market", callback_data="a:tipo:market"),
                                                             B("📌 Límite", callback_data="a:tipo:limit")]]))
        elif paso == "tipo":
            if val == "limit":
                f["paso"] = "precio"
                await self.enviar(chat, "Escribe el precio límite:")
            else:
                f["precio"] = None
                await self._pedir_lev(chat, f)
        elif paso == "lev":
            mx = calc.max_leverage(self.m.brackets(f["s"]))
            lev = int(float(val))
            if not 1 <= lev <= mx:
                await self.enviar(chat, f"❌ Debe ser de 1 a {mx}. Escribe otro:")
                return
            f["lev"] = lev
            f["paso"] = "margen"
            tm = "cruzado" if f["tm"] == "CROSSED" else "aislado"
            await self.enviar(chat, f"Modo de margen (ahora {tm}):",
                              kb=K([[B("Cruzado" + (" ✅" if f["tm"] == "CROSSED" else ""), callback_data="a:mg:CROSSED"),
                                     B("Aislado" + (" ✅" if f["tm"] == "ISOLATED" else ""), callback_data="a:mg:ISOLATED")]]))
        elif paso == "mg":
            f["tm"] = val
            f["paso"] = "monto"
            disp = self.motor.metricas(c, con_detalle=False)["disponible"]
            await self.enviar(chat, f"Disponible: {disp:,.2f} USDT\n\nEscribe el <b>margen en USDT</b> (ej. <code>100</code>) "
                                    f"o la cantidad en monedas con q (ej. <code>q0.01</code>):")

    async def _resumen_abrir(self, chat, f):
        c = self.motor.cuentas[f["cuenta"]]
        s = f["s"]
        ref = f.get("precio") or self.motor.ultimo(s)
        notional = f["cant"] * ref
        margen = notional / f["lev"]
        com = notional * (c.maker if f.get("precio") else c.taker)
        liq = ""
        if f["tm"] == "ISOLATED":
            lp = calc.liquidation_price(margen, 0, 0, [calc.Leg(1 if f["largo"] else -1, f["cant"], ref)], self.m.brackets(s))
            liq = f"\nLiquidación estimada: {self.v.precio(s, lp)}"
        txt = (f"📝 <b>Confirmar operación</b> ({html.escape(c.nombre)})\n"
               f"{s} {'🟢 LONG' if f['largo'] else '🔴 SHORT'} {f['lev']}x {'cruzado' if f['tm'] == 'CROSSED' else 'aislado'}\n"
               f"Tipo: {'Límite a ' + self.v.precio(s, f['precio']) if f.get('precio') else 'Market'}\n"
               f"Cantidad: {calc.fmt_num(f['cant'])}  ({notional:,.2f} USDT)\n"
               f"Margen: {margen:,.2f} USDT  ·  Comisión aprox: {com:,.4f} USDT\n"
               f"TP: {self.v.precio(s, f.get('tp')) if f.get('tp') else 'no'}  ·  SL: {self.v.precio(s, f.get('sl')) if f.get('sl') else 'no'}"
               f"{liq}")
        tok = self.confirmar(("abrir", dict(f)))
        self.flujo.pop(chat, None)
        await self.enviar(chat, txt, kb=self.kb_confirmar(tok))

    # ------------------------------------------------------------ ejecutar lo confirmado
    async def _ejecutar(self, chat: int, acc: tuple):
        k = acc[0]
        if k == "cerrar":
            _, cid, s, lado, p = acc
            await self.m.asegurar(s)
            o = self.motor.cerrar_posicion(cid, s, p, None if lado == "BOTH" else lado, origen="telegram")
            await self.enviar(chat, self.cmd._resultado(o), menu=True)
        elif k == "cancelar":
            o = self.motor.cancelar(acc[1], acc[2])
            await self.enviar(chat, f"✅ Orden #{o.id} cancelada.", menu=True)
        elif k == "cancelar_todas":
            n = self.motor.cancelar_todas(acc[1])
            await self.enviar(chat, f"✅ {n} órdenes canceladas.", menu=True)
        elif k in ("recargar", "retirar"):
            _, cid, monto = acc
            saldo = self.motor.transferir(cid, monto if k == "recargar" else -monto, f"{k.capitalize()} (telegram)")
            await self.enviar(chat, f"✅ Hecho. Saldo: {saldo:,.2f} USDT", menu=True)
        elif k == "modo":
            self.motor.cambiar_modo_posicion(acc[1], acc[2])
            await self.enviar(chat, "✅ Modo cambiado.", menu=True)
        elif k == "tpsl":
            _, cid, s, lado, tp, sl = acc
            hechas = self.motor.poner_tpsl(cid, s, tp=tp if tp not in (None, "no") else None,
                                           sl=sl if sl not in (None, "no") else None,
                                           lado_pos=None if lado == "BOTH" else lado,
                                           quitar_tp=tp == "no", quitar_sl=sl == "no")
            await self.enviar(chat, "✅ TP/SL listo.\n" + "\n".join(self.v.orden(o) for o in hechas), menu=True)
        elif k == "abrir":
            f = acc[1]
            c = self.motor.cuentas[f["cuenta"]]
            s = f["s"]
            await self.m.asegurar(s)
            if self.motor.ajuste(c, s)[1] != f["tm"]:
                self.motor.cambiar_tipo_margen(c.id, s, f["tm"])
            if self.motor.ajuste(c, s)[0] != f["lev"]:
                self.motor.cambiar_apalancamiento(c.id, s, f["lev"])
            lado_pos = ("LONG" if f["largo"] else "SHORT") if c.cobertura else None
            o = self.motor.colocar(c.id, s, "BUY" if f["largo"] else "SELL", "LIMIT" if f.get("precio") else "MARKET",
                                   cantidad=f["cant"], precio=f.get("precio"), lado_pos=lado_pos,
                                   tp=f.get("tp"), sl=f.get("sl"), origen="telegram")
            await self.enviar(chat, self.cmd._resultado(o), menu=True)
