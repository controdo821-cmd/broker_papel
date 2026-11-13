'use strict';
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const E = {cuenta: null, s: 'BTCUSDT', i: '15m', tipo: 'LIMIT', tab: 'pos', mk: null, cta: null, zona: 'America/Mexico_City'};
let chart, serie, lineas = [], ultimaVela = null;

async function api(ruta, datos) {
  const op = datos ? {method: 'POST', headers: {'Content-Type': 'application/json', 'X-BP': '1'}, body: JSON.stringify(datos)} : {};
  const r = await fetch(ruta, op);
  if (r.status === 401) { location.href = '/login'; throw new Error('sesión'); }
  const j = await r.json();
  if (!r.ok) throw new Error((j.error || 'Error') + (j.code ? `\n(Binance: ${j.code} ${j.msg})` : ''));
  return j;
}
function aviso(t, error) {
  const a = $('aviso'); a.textContent = t; a.className = 'aviso' + (error ? ' error' : '');
  clearTimeout(aviso.t); aviso.t = setTimeout(() => a.classList.add('oculto'), error ? 8000 : 4000);
}
function confirmar(html) {
  return new Promise((res) => {
    $('dlg-cuerpo').innerHTML = html; const d = $('dlg');
    d.onclose = () => res(d.returnValue === 'si' ? d : null); d.showModal();
  });
}
const dec = () => (E.mk ? E.mk.decimales : 2);
const px = (x) => (x == null ? '--' : Number(x).toFixed(dec()));
const n2 = (x, s) => (x == null ? '--' : (s && x > 0 ? '+' : '') + Number(x).toLocaleString('es-MX', {minimumFractionDigits: 2, maximumFractionDigits: 2}));
const cls = (x) => (x > 0 ? 'v' : x < 0 ? 'r' : '');
const hora = (ms) => new Date(ms).toLocaleString('es-MX', {timeZone: E.zona, day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit'});
const TIPO = {LIMIT: 'Límite', MARKET: 'Market', STOP: 'Stop Límite', STOP_MARKET: 'Stop Market', TAKE_PROFIT: 'TP Límite', TAKE_PROFIT_MARKET: 'TP Market', TRAILING_STOP_MARKET: 'Trailing'};
const EST = {NEW: 'Abierta', PARTIALLY_FILLED: 'Parcial', FILLED: 'Llenada', CANCELED: 'Cancelada', EXPIRED: 'Expirada'};
const TX = {REALIZED_PNL: 'PnL realizado', COMMISSION: 'Comisión', FUNDING_FEE: 'Funding', TRANSFER: 'Transferencia', INSURANCE_CLEAR: 'Liquidación'};

// ---------------------------------------------------------------- gráfica
function crearGrafica() {
  chart = LightweightCharts.createChart($('chart'), {
    layout: {background: {color: '#161a1e'}, textColor: '#848e9c'},
    grid: {vertLines: {color: '#2b3139'}, horzLines: {color: '#2b3139'}},
    timeScale: {timeVisible: true, borderColor: '#2b3139'}, rightPriceScale: {borderColor: '#2b3139'},
    localization: {timeFormatter: (t) => hora(t * 1000)}, autoSize: true,
  });
  serie = chart.addCandlestickSeries({upColor: '#0ecb81', downColor: '#f6465d', borderVisible: false, wickUpColor: '#0ecb81', wickDownColor: '#f6465d'});
}
async function cargarVelas() {
  try {
    const v = await api(`/api/velas?s=${E.s}&i=${E.i}`);
    serie.setData(v.map((x) => ({time: x[0], open: x[1], high: x[2], low: x[3], close: x[4]})));
    ultimaVela = v.length ? {time: v[v.length - 1][0], open: v[v.length - 1][1], high: v[v.length - 1][2], low: v[v.length - 1][3], close: v[v.length - 1][4]} : null;
    serie.applyOptions({priceFormat: {type: 'price', precision: dec(), minMove: E.mk ? E.mk.tick : 0.01}});
    marcarLlenados();
  } catch (e) { aviso(e.message, true); }
}
async function marcarLlenados() {
  try {
    const f = await api(`/api/historial?c=${E.cuenta}&tipo=llenados&limite=300`);
    const m = f.filter((x) => x.simbolo === E.s).map((x) => ({time: Math.floor(x.tiempo / 1000), position: x.lado === 'BUY' ? 'belowBar' : 'aboveBar',
      color: x.lado === 'BUY' ? '#0ecb81' : '#f6465d', shape: x.lado === 'BUY' ? 'arrowUp' : 'arrowDown', text: (x.lado === 'BUY' ? 'C ' : 'V ') + px(x.precio)}));
    const seg = {'1m': 60, '5m': 300, '15m': 900, '1h': 3600, '4h': 14400, '1d': 86400}[E.i];
    m.forEach((x) => { x.time = x.time - (x.time % seg); });
    m.sort((a, b) => a.time - b.time);
    serie.setMarkers(m);
  } catch (e) { /* sin marcas */ }
}
function dibujarLineas() {
  lineas.forEach((l) => serie.removePriceLine(l)); lineas = [];
  if (!E.cta) return;
  const add = (p, color, t) => p && lineas.push(serie.createPriceLine({price: p, color, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: t}));
  for (const p of E.cta.posiciones.filter((x) => x.simbolo === E.s)) {
    add(p.entrada, '#3d8bfd', 'Entrada'); add(p.liquidacion, '#f0b90b', 'Liq');
  }
  for (const o of E.cta.ordenes.filter((x) => x.simbolo === E.s)) {
    if (o.tipo.startsWith('TAKE')) add(o.precio_stop, '#0ecb81', 'TP');
    else if (o.tipo.startsWith('STOP')) add(o.precio_stop, '#f6465d', 'SL/Stop');
    if (o.tipo === 'LIMIT') add(o.precio, '#848e9c', 'Lím #' + o.id);
  }
}

// ---------------------------------------------------------------- mercado y cuenta
async function refrescarMercado() {
  try {
    E.mk = await api(`/api/mercado?s=${E.s}&c=${E.cuenta}`);
    $('m-ultimo').textContent = px(E.mk.ultimo); $('m-marca').textContent = px(E.mk.marca); $('m-indice').textContent = px(E.mk.indice);
    $('m-funding').textContent = E.mk.funding != null ? (E.mk.funding * 100).toFixed(4) + '%' : '--';
    $('m-libro').textContent = `${px(E.mk.bid)} / ${px(E.mk.ask)}`;
    $('b-lev').textContent = E.mk.lev + 'x'; $('b-margen').textContent = E.mk.tipo_margen === 'CROSSED' ? 'Cruzado' : 'Aislado';
    if (ultimaVela && E.mk.ultimo) {
      const c = E.mk.ultimo; ultimaVela.close = c; ultimaVela.high = Math.max(ultimaVela.high, c); ultimaVela.low = Math.min(ultimaVela.low, c);
      serie.update(ultimaVela);
    }
    infoOrden();
  } catch (e) { aviso(e.message, true); }
}
function cuentaRegresiva() {
  if (!E.mk || !E.mk.proximo_funding) return;
  const s = Math.max(0, Math.floor((E.mk.proximo_funding - Date.now()) / 1000));
  $('m-cuenta').textContent = [Math.floor(s / 3600), Math.floor(s % 3600 / 60), s % 60].map((x) => String(x).padStart(2, '0')).join(':');
}
async function refrescarCuenta() {
  try {
    E.cta = await api(`/api/cuenta?c=${E.cuenta}`);
    const c = E.cta;
    $('disp').textContent = n2(c.disponible);
    $('b-modo').textContent = c.cobertura ? 'Cobertura' : 'Unidireccional';
    $('resumen').innerHTML = [['Saldo', n2(c.saldo)], ['Balance de margen', n2(c.balance_margen)], ['PnL no realizado', `<b class="${cls(c.upnl)}">${n2(c.upnl, 1)}</b>`],
      ['Margen usado', n2(c.margen_inicial)], ['Ratio de margen', (c.ratio_margen * 100).toFixed(2) + '%'],
      ['PnL hoy', `<b class="${cls(c.pnl_hoy)}">${n2(c.pnl_hoy, 1)}</b>`], ['PnL semana', `<b class="${cls(c.pnl_semana)}">${n2(c.pnl_semana, 1)}</b>`],
      ['PnL mes', `<b class="${cls(c.pnl_mes)}">${n2(c.pnl_mes, 1)}</b>`]].map(([a, b]) => `<div><span>${a}</span><b>${b}</b></div>`).join('');
    dibujarLineas();
    if (E.tab === 'pos' || E.tab === 'ord') pintarTab();
  } catch (e) { aviso(e.message, true); }
}

// ---------------------------------------------------------------- formulario de orden
function mostrarCampos() {
  const t = E.tipo;
  const tienePrecio = ['LIMIT', 'STOP', 'TAKE_PROFIT'].includes(t);
  const tieneStop = ['STOP', 'STOP_MARKET', 'TAKE_PROFIT', 'TAKE_PROFIT_MARKET'].includes(t);
  document.querySelectorAll('.c-precio').forEach((x) => x.classList.toggle('oculto', !tienePrecio));
  document.querySelectorAll('.c-stop').forEach((x) => x.classList.toggle('oculto', !tieneStop));
  document.querySelectorAll('.c-disparo').forEach((x) => x.classList.toggle('oculto', !(tieneStop || t === 'TRAILING_STOP_MARKET')));
  document.querySelectorAll('.c-trail').forEach((x) => x.classList.toggle('oculto', t !== 'TRAILING_STOP_MARKET'));
  document.querySelectorAll('.c-limite').forEach((x) => x.classList.toggle('oculto', t !== 'LIMIT'));
  if (tienePrecio && !$('o-precio').value && E.mk) $('o-precio').value = px(E.mk.ultimo);
  infoOrden();
}
function cantidadOrden() {
  const v = parseFloat($('o-cant').value);
  if (!v || !E.mk) return null;
  if ($('o-unidad').value === 'moneda') return v;
  const ref = parseFloat($('o-precio').value) || parseFloat($('o-stop').value) || E.mk.ultimo;
  const q = v * E.mk.lev / ref;
  return Math.floor(q / E.mk.paso + 1e-9) * E.mk.paso;
}
function infoOrden() {
  const q = cantidadOrden();
  if (!q || !E.mk) { $('o-info').textContent = ''; return; }
  const ref = parseFloat($('o-precio').value) || E.mk.ultimo;
  const pasoDec = Math.max(0, Math.round(-Math.log10(E.mk.paso)));
  $('o-info').textContent = `Cantidad: ${q.toFixed(pasoDec)} ${E.s.replace('USDT', '')} · Valor: ${n2(q * ref)} USDT · Margen: ${n2(q * ref / E.mk.lev)} USDT`;
}
async function enviarOrden(lado) {
  const q = cantidadOrden();
  const d = {c: E.cuenta, s: E.s, lado, tipo: E.tipo, cantidad: q, precio: $('o-precio').value, stop: $('o-stop').value,
    disparo: $('o-disparo').value, retroceso: $('o-ret').value, activacion: $('o-act').value, reducir: $('o-reducir').checked,
    tif: $('o-post').checked ? 'GTX' : $('o-tif').value};
  if (!['LIMIT', 'STOP', 'TAKE_PROFIT'].includes(E.tipo)) d.precio = null;
  if ($('o-tpsl').checked) { d.tp = $('o-tp').value; d.sl = $('o-sl').value; }
  if (!q) return aviso('Escribe el tamaño.', true);
  const larg = lado === 'BUY';
  const ok = await confirmar(`<b>${larg ? '🟢 Comprar / Long' : '🔴 Vender / Short'} ${esc(E.s)}</b>\n${TIPO[E.tipo]} · ${q} · ${E.mk.lev}x` +
    (d.precio ? `\nPrecio ${esc(d.precio)}` : '') + (d.stop ? `\nDisparo ${esc(d.stop)}` : '') + (d.tp ? `\nTP ${esc(d.tp)}` : '') + (d.sl ? `\nSL ${esc(d.sl)}` : ''));
  if (!ok) return;
  try { const r = await api('/api/orden', d); aviso(r.texto.replace(/<[^>]+>/g, '')); refrescarCuenta(); marcarLlenados(); } catch (e) { aviso(e.message, true); }
}

// ---------------------------------------------------------------- pestañas de abajo
function tabla(cab, filas) {
  if (!filas.length) return '<p class="g">Sin datos.</p>';
  return `<table><thead><tr>${cab.map((c) => `<th>${c}</th>`).join('')}</tr></thead><tbody>${filas.map((f) => `<tr>${f.map((c) => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}
async function pintarTab() {
  const el = $('contenido'); const c = E.cta; const t = E.tab;
  try {
    if (t === 'pos') {
      el.innerHTML = tabla(['Moneda', 'Tamaño', 'Entrada', 'Marca', 'Liq.', 'Ratio', 'Margen', 'PnL (ROE)', 'Acciones'], (c?.posiciones || []).map((p) => [
        `<b class="${p.amt > 0 ? 'v' : 'r'}">${esc(p.simbolo)}</b> ${p.amt > 0 ? 'Long' : 'Short'} ${p.apalancamiento}x ${p.tipo_margen === 'CROSSED' ? 'Cruzado' : 'Aislado'}`,
        Math.abs(p.amt), px(p.entrada), px(p.marca), px(p.liquidacion) + (p.distancia_liq != null ? ` <span class="g">(${p.distancia_liq.toFixed(1)}%)</span>` : ''),
        (p.ratio_margen * 100).toFixed(2) + '%', n2(p.margen), `<b class="${cls(p.upnl)}">${n2(p.upnl, 1)} (${p.roe.toFixed(2)}%)</b>`,
        `<button data-acc="cerrar" data-s="${esc(p.simbolo)}" data-l="${p.lado}" data-p="100">Cerrar</button><button data-acc="cerrar" data-s="${esc(p.simbolo)}" data-l="${p.lado}" data-p="50">50%</button><button data-acc="tpsl" data-s="${esc(p.simbolo)}" data-l="${p.lado}">TP/SL</button>` +
        (p.tipo_margen === 'ISOLATED' ? `<button data-acc="margen" data-s="${esc(p.simbolo)}" data-l="${p.lado}">Margen</button>` : '')]));
    } else if (t === 'ord') {
      el.innerHTML = (c?.ordenes?.length ? '<button data-acc="cancelar" data-id="todas">Cancelar todas</button>' : '') +
        tabla(['Hora', 'Moneda', 'Tipo', 'Lado', 'Precio', 'Disparo', 'Cantidad', 'Llenado', 'Solo reducir', ''], (c?.ordenes || []).map((o) => [
          hora(o.creada), esc(o.simbolo), TIPO[o.tipo] + (o.disparada ? ' ⚡' : ''), `<span class="${o.lado === 'BUY' ? 'v' : 'r'}">${o.lado === 'BUY' ? 'Compra' : 'Venta'}</span> ${o.lado_pos !== 'BOTH' ? o.lado_pos : ''}`,
          o.precio ? px(o.precio) : 'Market', o.precio_stop ? px(o.precio_stop) + (o.disparo === 'MARK_PRICE' ? ' (marca)' : '') : (o.retroceso ? o.retroceso + '%' : '--'),
          o.cerrar_posicion ? 'Toda' : o.cantidad, o.ejecutada, o.solo_reducir || o.cerrar_posicion ? 'Sí' : 'No', `<button data-acc="cancelar" data-id="${o.id}">Cancelar</button>`]));
    } else if (t === 'hord') {
      const f = await api(`/api/historial?c=${E.cuenta}&tipo=ordenes`);
      el.innerHTML = tabla(['Hora', 'Moneda', 'Tipo', 'Lado', 'Precio prom.', 'Llenado / Cantidad', 'Estado', 'Origen', 'Nota'], f.map((o) => [
        hora(o.actualizada), esc(o.simbolo), TIPO[o.tipo], o.lado === 'BUY' ? 'Compra' : 'Venta', o.precio_prom ? px(o.precio_prom) : '--',
        `${o.ejecutada} / ${o.cerrar_posicion ? 'Toda' : o.cantidad}`, EST[o.estado] || o.estado, esc(o.origen), esc(o.motivo)]));
    } else if (t === 'fills') {
      const f = await api(`/api/historial?c=${E.cuenta}&tipo=llenados`);
      el.innerHTML = tabla(['Hora', 'Moneda', 'Lado', 'Precio', 'Cantidad', 'Comisión', 'Rol', 'PnL realizado'], f.map((x) => [
        hora(x.tiempo), esc(x.simbolo), x.lado === 'BUY' ? 'Compra' : 'Venta', x.precio, x.cantidad, x.comision.toFixed(4), x.maker ? 'Maker' : 'Taker',
        `<span class="${cls(x.pnl)}">${n2(x.pnl, 1)}</span>`]));
    } else if (t === 'tx') {
      const f = await api(`/api/historial?c=${E.cuenta}&tipo=transacciones`);
      el.innerHTML = tabla(['Hora', 'Tipo', 'Monto', 'Moneda', 'Detalle'], f.map((x) => [hora(x.tiempo), TX[x.tipo] || x.tipo,
        `<span class="${cls(x.monto)}">${n2(x.monto, 1)}</span>`, esc(x.simbolo), esc(x.info)]));
    } else if (t === 'cerr') {
      const f = await api(`/api/historial?c=${E.cuenta}&tipo=cerradas`);
      el.innerHTML = tabla(['Cerrada', 'Moneda', 'Lado', 'Entrada', 'Salida', 'Cant. máx', 'PnL', 'Comisiones', 'Funding', 'Neto', 'Motivo'], f.map((x) => {
        const neto = x.pnl - x.comisiones + x.funding;
        return [hora(x.cerrada), esc(x.simbolo), x.lado, x.entrada, x.salida, x.cantidad_max, n2(x.pnl, 1), n2(-x.comisiones), n2(x.funding, 1),
          `<b class="${cls(neto)}">${n2(neto, 1)}</b>`, esc(x.motivo)];
      }));
    } else if (t === 'est') {
      const rango = pintarTab.rango || 'todo';
      const r = await api(`/api/estadisticas?c=${E.cuenta}&rango=${rango}`);
      const tar = [['Operaciones cerradas', r.operaciones], ['% ganadoras', r.pct_ganadoras.toFixed(1) + '%'], ['Mejor operación', n2(r.mejor_neto, 1)],
        ['Peor operación', n2(r.peor_neto, 1)], ['PnL realizado', n2(r.pnl_realizado, 1)], ['Comisiones', n2(r.comisiones, 1)], ['Funding', n2(r.funding, 1)],
        ['Neto', `<b class="${cls(r.neto)}">${n2(r.neto, 1)}</b>`], ['Transferencias', n2(r.transferencias, 1)]];
      el.innerHTML = `<div class="fila" style="max-width:420px;margin-bottom:8px">${['hoy', 'semana', 'mes', 'todo'].map((x) => `<button data-acc="rango" data-r="${x}" class="${x === rango ? 'act' : 'sec'}">${x[0].toUpperCase() + x.slice(1)}</button>`).join('')}</div>` +
        `<div class="tarjetas">${tar.map(([a, b]) => `<div><small>${a}</small><b>${b}</b></div>`).join('')}</div><div id="curva" style="height:240px"></div>`;
      const cc = LightweightCharts.createChart($('curva'), {layout: {background: {color: '#161a1e'}, textColor: '#848e9c'}, grid: {vertLines: {color: '#2b3139'}, horzLines: {color: '#2b3139'}}, autoSize: true, timeScale: {timeVisible: true}});
      const s = cc.addAreaSeries({lineColor: '#f0b90b', topColor: '#f0b90b44', bottomColor: '#f0b90b00'});
      const pts = []; for (const [tt, v] of r.curva) { if (pts.length && pts[pts.length - 1].time >= tt) pts[pts.length - 1].value = v; else pts.push({time: tt, value: v}); }
      s.setData(pts); cc.timeScale().fitContent();
    } else if (t === 'cal') {
      const hoy = new Date(); const a = pintarTab.anio || hoy.getFullYear(); const m = pintarTab.mes || hoy.getMonth() + 1;
      const r = await api(`/api/calendario?c=${E.cuenta}&anio=${a}&mes=${m}`);
      const primero = (new Date(a, m - 1, 1).getDay() + 6) % 7; const dias = new Date(a, m, 0).getDate();
      let h = ['Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb', 'Dom'].map((d) => `<div class="cab">${d}</div>`).join('') + '<div class="cab"></div>'.repeat(0);
      h += '<div style="background:none"></div>'.repeat(primero);
      for (let d = 1; d <= dias; d++) { const v = r.dias[d]; h += `<div class="${v > 0 ? 'pos' : v < 0 ? 'neg' : ''}">${d}${v ? `<b class="${cls(v)}">${n2(v, 1)}</b>` : ''}</div>`; }
      const total = Object.values(r.dias).reduce((x, y) => x + y, 0);
      const meses = ['Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun', 'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic'];
      el.innerHTML = `<div class="fila" style="max-width:640px;margin-bottom:8px"><button data-acc="mes" data-d="-1" class="sec">◀</button><b style="flex:2;text-align:center">${meses[m - 1]} ${a} · <span class="${cls(total)}">${n2(total, 1)} USDT</span></b><button data-acc="mes" data-d="1" class="sec">▶</button></div><div class="cal">${h}</div>` +
        `<p class="g">Por mes en ${a}: ${Object.entries(r.meses).map(([k, v]) => `${meses[k - 1]} <span class="${cls(v)}">${n2(v, 1)}</span>`).join(' · ') || 'sin datos'}</p>`;
    } else if (t === 'cta') {
      el.innerHTML = `<div class="cuenta-acc">
        <label>Monto USDT<input id="t-monto" type="number" step="any"></label>
        <button data-acc="transf" data-sg="1">Recargar</button><button data-acc="transf" data-sg="-1">Retirar</button>
        <button data-acc="modo">Cambiar a ${c?.cobertura ? 'unidireccional' : 'cobertura'}</button>
        <button data-acc="exportar" data-f="xlsx">Exportar Excel</button><button data-acc="exportar" data-f="csv">Exportar CSV</button>
        <button data-acc="reiniciar" class="sec">Reiniciar cuenta…</button></div>
        <p class="g">Comisiones: maker ${(c.maker * 100).toFixed(4)}% · taker ${(c.taker * 100).toFixed(4)}%. Las recargas y retiros quedan como transferencia y no cuentan como ganancia.</p>`;
    }
  } catch (e) { el.textContent = e.message; }
}

// ---------------------------------------------------------------- acciones
document.addEventListener('click', async (ev) => {
  const b = ev.target.closest('button[data-acc]'); if (!b) return;
  const a = b.dataset.acc;
  try {
    if (a === 'cerrar') {
      if (!await confirmar(`¿Cerrar ${b.dataset.p}% de ${esc(b.dataset.s)} a mercado?`)) return;
      const r = await api('/api/cerrar', {c: E.cuenta, s: b.dataset.s, lado_pos: b.dataset.l, pct: b.dataset.p}); aviso(r.texto.replace(/<[^>]+>/g, ''));
    } else if (a === 'tpsl') {
      const d = await confirmar(`<b>TP/SL de toda la posición ${esc(b.dataset.s)}</b>\nDeja vacío para no cambiar, o escribe "no" para quitar.<label>Take Profit<input id="x-tp"></label><label>Stop Loss<input id="x-sl"></label><label>Disparar con<select id="x-d"><option value="MARK_PRICE">Precio de marca</option><option value="CONTRACT_PRICE">Último precio</option></select></label>`);
      if (!d) return;
      await api('/api/tpsl', {c: E.cuenta, s: b.dataset.s, lado_pos: b.dataset.l, tp: $('x-tp').value || null, sl: $('x-sl').value || null, disparo: $('x-d').value}); aviso('TP/SL actualizado.');
    } else if (a === 'margen') {
      const d = await confirmar(`<b>Margen aislado ${esc(b.dataset.s)}</b>\nPositivo agrega, negativo quita.<label>Monto USDT<input id="x-m" type="number" step="any"></label>`);
      if (!d) return;
      await api('/api/ajuste', {c: E.cuenta, que: 'margen_aislado', s: b.dataset.s, lado_pos: b.dataset.l, valor: $('x-m').value}); aviso('Margen ajustado.');
    } else if (a === 'cancelar') {
      if (b.dataset.id === 'todas' && !await confirmar('¿Cancelar todas las órdenes abiertas?')) return;
      await api('/api/cancelar', {c: E.cuenta, id: b.dataset.id}); aviso('Cancelada.');
    } else if (a === 'rango') { pintarTab.rango = b.dataset.r; return pintarTab(); }
    else if (a === 'mes') {
      const hoy = new Date(); let m = (pintarTab.mes || hoy.getMonth() + 1) + Number(b.dataset.d); let y = pintarTab.anio || hoy.getFullYear();
      if (m < 1) { m = 12; y--; } if (m > 12) { m = 1; y++; } pintarTab.mes = m; pintarTab.anio = y; return pintarTab();
    } else if (a === 'transf') {
      const m = parseFloat($('t-monto').value); if (!m) return aviso('Escribe el monto.', true);
      const monto = m * Number(b.dataset.sg);
      if (!await confirmar(`¿${monto > 0 ? 'Recargar' : 'Retirar'} ${n2(Math.abs(monto))} USDT?`)) return;
      const r = await api('/api/transferir', {c: E.cuenta, monto}); aviso('Saldo: ' + n2(r.saldo) + ' USDT');
    } else if (a === 'modo') {
      if (!await confirmar('¿Cambiar el modo de posición?')) return;
      await api('/api/ajuste', {c: E.cuenta, que: 'modo', valor: !E.cta.cobertura}); aviso('Modo cambiado.');
    } else if (a === 'exportar') { location.href = `/api/exportar?c=${E.cuenta}&f=${b.dataset.f}`; return; }
    else if (a === 'reiniciar') {
      const d = await confirmar('<b>Reiniciar cuenta</b>\nSe borran posiciones, órdenes e historial.<label>Saldo nuevo<input id="x-s" type="number" value="10000"></label><label>Escribe REINICIAR<input id="x-c"></label>');
      if (!d) return;
      await api('/api/reiniciar', {c: E.cuenta, saldo: $('x-s').value, confirmar: $('x-c').value}); aviso('Cuenta reiniciada.');
    }
    await refrescarCuenta(); pintarTab();
  } catch (e) { aviso(e.message, true); }
});

async function cambiarSimbolo(s) {
  s = s.toUpperCase().trim(); if (!s.endsWith('USDT')) s += 'USDT';
  E.s = s; $('simbolo').value = s; $('o-precio').value = ''; $('o-stop').value = '';
  try { localStorage.setItem('bp_s', s); } catch (e) { /* nada */ }
  await refrescarMercado(); await cargarVelas(); dibujarLineas();
}

async function iniciar() {
  crearGrafica();
  const yo = await api('/api/yo'); E.zona = yo.zona;
  $('cuenta').innerHTML = yo.cuentas.map((c) => `<option value="${c.id}">${esc(c.nombre)}</option>`).join('');
  try { E.cuenta = Number(localStorage.getItem('bp_c')) || yo.cuentas[0].id; E.s = localStorage.getItem('bp_s') || E.s; } catch (e) { E.cuenta = yo.cuentas[0].id; }
  if (!yo.cuentas.some((c) => c.id === E.cuenta)) E.cuenta = yo.cuentas[0].id;
  $('cuenta').value = E.cuenta;
  api('/api/simbolos').then((l) => { $('lista-simbolos').innerHTML = l.map((s) => `<option value="${s}">`).join(''); });
  await cambiarSimbolo(E.s); await refrescarCuenta(); pintarTab(); mostrarCampos();
  setInterval(refrescarMercado, 2000); setInterval(refrescarCuenta, 3000); setInterval(cuentaRegresiva, 1000); setInterval(cargarVelas, 30000);
}

$('cuenta').onchange = (e) => { E.cuenta = Number(e.target.value); try { localStorage.setItem('bp_c', E.cuenta); } catch (x) { /* nada */ } refrescarCuenta().then(pintarTab); marcarLlenados(); };
$('simbolo').onchange = (e) => cambiarSimbolo(e.target.value);
$('intervalos').onclick = (e) => { const b = e.target.closest('button'); if (!b) return; E.i = b.dataset.i; document.querySelectorAll('#intervalos button').forEach((x) => x.classList.toggle('act', x === b)); cargarVelas(); };
$('tipos').onclick = (e) => { const b = e.target.closest('button'); if (!b) return; E.tipo = b.dataset.t; document.querySelectorAll('#tipos button').forEach((x) => x.classList.toggle('act', x === b)); mostrarCampos(); };
$('tabs').onclick = (e) => { const b = e.target.closest('button'); if (!b) return; E.tab = b.dataset.tab; document.querySelectorAll('#tabs button').forEach((x) => x.classList.toggle('act', x === b)); pintarTab(); };
document.querySelector('.pcts').onclick = (e) => { const b = e.target.closest('button'); if (!b || !E.cta) return; $('o-unidad').value = 'usdt'; $('o-cant').value = (Math.max(0, E.cta.disponible) * b.dataset.p / 100 * 0.995).toFixed(2); infoOrden(); };
['o-cant', 'o-precio', 'o-unidad', 'o-stop'].forEach((id) => $(id).addEventListener('input', infoOrden));
$('o-tpsl').onchange = (e) => $('caja-tpsl').classList.toggle('oculto', !e.target.checked);
$('b-comprar').onclick = () => enviarOrden('BUY');
$('b-vender').onclick = () => enviarOrden('SELL');
$('salir').onclick = async () => { await api('/api/logout', {}); location.href = '/login'; };
$('b-lev').onclick = async () => {
  const d = await confirmar(`<b>Apalancamiento ${esc(E.s)}</b><label>De 1 a ${E.mk.max_lev}x<input id="x-l" type="number" min="1" max="${E.mk.max_lev}" value="${E.mk.lev}"></label>`);
  if (!d) return;
  try { await api('/api/ajuste', {c: E.cuenta, que: 'lev', s: E.s, valor: $('x-l').value}); refrescarMercado(); } catch (e) { aviso(e.message, true); }
};
$('b-margen').onclick = async () => {
  const nuevo = E.mk.tipo_margen === 'CROSSED' ? 'ISOLATED' : 'CROSSED';
  if (!await confirmar(`¿Cambiar ${esc(E.s)} a margen ${nuevo === 'CROSSED' ? 'cruzado' : 'aislado'}?`)) return;
  try { await api('/api/ajuste', {c: E.cuenta, que: 'margen', s: E.s, valor: nuevo}); refrescarMercado(); } catch (e) { aviso(e.message, true); }
};
$('b-modo').onclick = async () => {
  if (!await confirmar(`¿Cambiar a modo ${E.cta.cobertura ? 'unidireccional' : 'cobertura'}?`)) return;
  try { await api('/api/ajuste', {c: E.cuenta, que: 'modo', valor: !E.cta.cobertura}); refrescarCuenta(); } catch (e) { aviso(e.message, true); }
};
iniciar().catch((e) => aviso(e.message, true));
