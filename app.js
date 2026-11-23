/* Chokepoint Desk — reads only data/*.json and draws four panels.
   No dependencies: charts are hand-drawn SVG so the page stays light. */
(function () {
  'use strict';

  // ---- configuration -------------------------------------------------------
  var STRAITS = {
    hormuz: {
      title: 'Strait of Hormuz',
      // Reference lines from the brief (Plan 1, section 2).
      refs: [
        { value: 92.3, label: '2023 avg 92.3', pos: 'above' },
        { value: 85.5, label: '2025 avg 85.5', pos: 'below', compare: '2025 average' },
        { value: 60, label: '60 line', pos: 'above' }
      ]
    },
    bab_el_mandeb: { title: 'Bab el-Mandeb Strait', refs: [] }
  };
  // Stack order = validated colour slot order (bottom to top).
  var TYPES = [
    { key: 'tanker', label: 'Tanker', color: 'var(--s1)' },
    { key: 'dry_bulk', label: 'Dry bulk', color: 'var(--s2)' },
    { key: 'container', label: 'Container', color: 'var(--s3)' },
    { key: 'general_cargo', label: 'General cargo', color: 'var(--s4)' },
    { key: 'roro', label: 'Ro-ro', color: 'var(--s5)' }
  ];
  var STATUS = {
    claimed_only: 'Claimed only',
    third_party_report: 'Third-party report',
    confirmed: 'Confirmed'
  };
  var REGIME = {
    open: { label: 'Open', icon: '<circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="2"/><path d="M5 8.2l2 2 4-4.2" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>' },
    contested: { label: 'Contested', icon: '<path d="M8 1.8l6.6 11.7H1.4z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M8 6.3v3.4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><circle cx="8" cy="11.6" r="1" fill="currentColor"/>' },
    closed: { label: 'Closed', icon: '<circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="2"/><path d="M4 8h8" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>' }
  };

  // ---- small helpers -------------------------------------------------------
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var SVGNS = 'http://www.w3.org/2000/svg';
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function safeUrl(u) { return /^https?:\/\//i.test(String(u || '')) ? String(u) : null; }
  function parseDate(s) { // 'YYYY-MM-DD' or ISO datetime -> Date (UTC)
    if (!s) return null;
    var d = new Date(/^\d{4}-\d{2}-\d{2}$/.test(s) ? s + 'T00:00:00Z' : s);
    return isNaN(d) ? null : d;
  }
  var fmtDay = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
  var fmtDayShort = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' });
  var fmtWkDay = new Intl.DateTimeFormat('en-GB', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' });
  var fmtTimeUTC = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZone: 'UTC' });
  function day(s) { var d = parseDate(s); return d ? fmtDay.format(d) : '—'; }
  function dayShort(s) { var d = parseDate(s); return d ? fmtDayShort.format(d) : '—'; }
  function stamp(s) { var d = parseDate(s); return d ? fmtDay.format(d) + ', ' + fmtTimeUTC.format(d) + ' UTC' : '—'; }
  function num(v, dp) {
    if (v == null || isNaN(v)) return '—';
    return Number(v).toLocaleString('en-GB', { minimumFractionDigits: dp || 0, maximumFractionDigits: dp || 0 });
  }
  function addDays(iso, n) { var d = parseDate(iso); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); }
  function svg(tag, attrs, parent) {
    var el = document.createElementNS(SVGNS, tag);
    for (var k in attrs) if (attrs[k] != null) el.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(el);
    return el;
  }
  function niceMax(v) { // 4 grid steps of a round size
    var steps = [1, 2, 2.5, 5, 10, 15, 20, 25, 30, 40, 50, 75, 100, 150, 200, 250, 500];
    for (var i = 0; i < steps.length; i++) if (steps[i] * 4 >= v) return steps[i] * 4;
    return Math.ceil(v / 500) * 500;
  }
  function isMock() {
    for (var i = 0; i < arguments.length; i++) {
      var a = arguments[i];
      if (a && /mock/i.test(String(a))) return true;
    }
    return false;
  }

  // ---- data loading --------------------------------------------------------
  var cache = {};
  function getJSON(path) {
    if (!cache[path]) {
      var pre = window.__desk && window.__desk.req && window.__desk.req[path];
      if (pre) delete window.__desk.req[path]; // a Response body can be read once
      cache[path] = (pre || fetch(path, { cache: 'no-cache' })).then(function (r) {
        if (!r.ok) throw new Error(r.status + ' for ' + path);
        return r.json();
      }).then(function (j) { return { ok: true, data: j }; }, function (e) { return { ok: false, error: e, path: path }; });
    }
    return cache[path];
  }

  function normTransits(j) {
    var rows = Array.isArray(j) ? j : (j.rows || j.daily || j.data || []);
    rows = rows.filter(function (r) { return r && r.date; }).slice().sort(function (a, b) { return a.date < b.date ? -1 : 1; });
    rows.forEach(function (r, i) {
      r.by_type = r.by_type || {};
      if (r.total == null) r.total = TYPES.reduce(function (s, t) { return s + (+r.by_type[t.key] || 0); }, 0);
      if (r.ma7 === undefined) {
        r.ma7 = i < 6 ? null : rows.slice(i - 6, i + 1).reduce(function (s, x) { return s + x.total; }, 0) / 7;
      }
    });
    return {
      rows: rows,
      source: j.source || 'IMF PortWatch',
      fetched_at: j.fetched_at,
      last_data_date: j.last_data_date || (rows.length ? rows[rows.length - 1].date : null)
    };
  }

  // ---- state ---------------------------------------------------------------
  var state = { strait: 'hormuz', range: '90', scale: 'refs', transits: null, forecast: null, refs: [] };
  function refsFor(strait, fc) {
    var st = STRAITS[strait].refs;
    if (st.length) return st;
    var b = fc && fc.ok && fc.data.baseline;
    if (b && b.mean_per_day != null) {
      var lab = b.label ? b.label.replace(/\s*\(.*\)$/, '') : 'baseline';
      return [{ value: +b.mean_per_day, label: lab + ' ' + num(b.mean_per_day, 1), pos: 'above', compare: lab }];
    }
    return [];
  }
  function prettyDates(text) { // ISO dates inside free text -> '15 Nov 2026'
    return esc(text).replace(/\b(\d{4}-\d{2}-\d{2})\b/g, function (m) { return day(m); });
  }

  // Long ranges are drawn as 7-day bins (daily average per bin) so the SVG stays light.
  function binWeekly(rows) {
    var out = [];
    for (var end = rows.length; end > 0; end -= 7) {
      var chunk = rows.slice(Math.max(0, end - 7), end), k = chunk.length;
      var by = {};
      TYPES.forEach(function (t) { by[t.key] = chunk.reduce(function (s, r) { return s + (+r.by_type[t.key] || 0); }, 0) / k; });
      var tot = chunk.reduce(function (s, r) { return s + r.total; }, 0) / k;
      out.unshift({ date: chunk[k - 1].date, start: chunk[0].date, by_type: by, total: tot, ma7: tot, binned: true });
    }
    return out;
  }

  // ---- tooltip (shared) ----------------------------------------------------
  var tip = $('#tooltip');
  function showTip(html, clientX, clientY) {
    tip.innerHTML = html;
    tip.hidden = false;
    var w = tip.offsetWidth, h = tip.offsetHeight, vw = document.documentElement.clientWidth;
    var x = clientX + 14, y = clientY - h - 12;
    if (x + w > vw - 8) x = clientX - w - 14;
    if (x < 8) x = 8;
    if (y < 8) y = clientY + 16;
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  }
  function hideTip() { tip.hidden = true; }
  function tipRow(color, label, value, cls) {
    return '<div class="tooltip__row ' + (cls || '') + '"><span>' +
      (color ? '<i class="swatch" style="background:' + color + '"></i>' : '') + esc(label) +
      '</span><span>' + value + '</span></div>';
  }

  /* Interactive layer: one invisible hit column per day, plus keyboard arrows.
     `points` = [{x, html}] in svg coordinates. */
  function attachHover(container, svgEl, points, plotTop, plotBottom) {
    var cursor = svg('line', { class: 'cursor', y1: plotTop, y2: plotBottom, visibility: 'hidden' }, svgEl);
    var idx = -1;
    function place(i, clientX, clientY) {
      idx = i;
      var p = points[i];
      cursor.setAttribute('x1', p.x); cursor.setAttribute('x2', p.x);
      cursor.setAttribute('visibility', 'visible');
      if (clientX == null) {
        var r = svgEl.getBoundingClientRect();
        var scale = r.width / svgEl.viewBox.baseVal.width;
        clientX = r.left + p.x * scale; clientY = r.top + plotTop * scale + 40;
      }
      showTip(p.html, clientX, clientY);
    }
    function nearest(clientX) {
      var r = svgEl.getBoundingClientRect();
      var x = (clientX - r.left) * (svgEl.viewBox.baseVal.width / r.width);
      var best = 0, bd = Infinity;
      for (var i = 0; i < points.length; i++) { var d = Math.abs(points[i].x - x); if (d < bd) { bd = d; best = i; } }
      return best;
    }
    function clear() { idx = -1; cursor.setAttribute('visibility', 'hidden'); hideTip(); }
    svgEl.addEventListener('pointermove', function (e) { place(nearest(e.clientX), e.clientX, e.clientY); });
    svgEl.addEventListener('pointerdown', function (e) { place(nearest(e.clientX), e.clientX, e.clientY); });
    svgEl.addEventListener('pointerleave', function (e) { if (e.pointerType === 'mouse') clear(); });
    container.onkeydown = function (e) {
      if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
        e.preventDefault();
        var n = idx < 0 ? points.length - 1 : Math.max(0, Math.min(points.length - 1, idx + (e.key === 'ArrowRight' ? 1 : -1)));
        place(n);
      } else if (e.key === 'Escape') clear();
    };
    container.onblur = clear;
  }

  // ---- shared axis drawing -------------------------------------------------
  function drawFrame(g, o) {
    // y grid + labels
    var ticks = 4;
    for (var i = 0; i <= ticks; i++) {
      var v = o.yMax * i / ticks, y = o.y(v);
      svg('line', { class: i === 0 ? 'axis-line' : 'grid-line', x1: o.left, x2: o.right, y1: y, y2: y }, g);
      var t = svg('text', { x: o.left - 6, y: y + 3.5, 'text-anchor': 'end' }, g);
      t.textContent = num(v, v % 1 ? 1 : 0);
    }
    // x labels
    var dates = o.dates, n = dates.length;
    var maxLabels = Math.max(2, Math.floor((o.right - o.left) / 64));
    var step = Math.max(1, Math.ceil(n / maxLabels));
    for (var j = n - 1; j >= 0; j -= step) {
      var tx = svg('text', { x: o.x(j), y: o.bottom + 16, 'text-anchor': j === n - 1 ? 'end' : 'middle' }, g);
      tx.textContent = dayShort(dates[j]);
    }
  }
  function drawRefs(g, refs, o) {
    refs.forEach(function (r) {
      if (r.value > o.yMax) return;
      var y = o.y(r.value);
      svg('line', { class: 'ref-line', x1: o.left, x2: o.right, y1: y, y2: y }, g);
      var t = svg('text', { class: 'ref-label', x: o.right - 2, y: r.pos === 'below' ? y + 12 : y - 4, 'text-anchor': 'end' }, g);
      t.textContent = r.label;
    });
  }

  // ---- Panel A: transit counter -------------------------------------------
  function renderTiles(T) {
    var rows = T.rows, el = $('#a-tiles');
    el.hidden = !rows.length; // an empty, visible #a-tiles reserves room while loading
    if (!rows.length) { el.innerHTML = ''; return; }
    var last = rows[rows.length - 1];
    var lastMa = null;
    for (var i = rows.length - 1; i >= 0; i--) if (rows[i].ma7 != null) { lastMa = rows[i]; break; }
    var w30 = rows.slice(-30), avg30 = w30.reduce(function (s, r) { return s + r.total; }, 0) / w30.length;
    var ctx = '';
    var cmp = state.refs.filter(function (r) { return r.compare; })[0];
    if (lastMa && cmp) {
      var diff = (lastMa.ma7 / cmp.value - 1) * 100;
      ctx = '<p class="tile__ctx">' + (diff < 0 ? num(Math.abs(diff)) + '% below' : num(diff) + '% above') + ' the ' + esc(cmp.compare) + ' (' + num(cmp.value, 1) + '/day)</p>';
    }
    el.innerHTML =
      tile('Latest daily count', num(last.total), 'ships', 'on ' + day(last.date)) +
      tile('7-day average', lastMa ? num(lastMa.ma7, 1) : '—', 'per day', lastMa ? day(addDays(lastMa.date, -6)) + ' – ' + day(lastMa.date) : '', ctx) +
      tile('30-day average', num(avg30, 1), 'per day', day(w30[0].date) + ' – ' + day(last.date));
  }
  function tile(label, value, unit, date, extra) {
    return '<div class="tile"><p class="tile__label">' + esc(label) + '</p><p class="tile__value">' + value +
      '<span class="tile__unit">' + esc(unit) + '</span></p><p class="tile__date">' + esc(date) + '</p>' + (extra || '') + '</div>';
  }

  function renderLegendA() {
    var refs = state.refs;
    $('#a-legend').innerHTML = TYPES.map(function (t) {
      return '<span class="legend__item"><i class="swatch" style="background:' + t.color + '"></i>' + t.label + '</span>';
    }).join('') +
      '<span class="legend__item"><i class="swatch swatch--line" style="background:var(--actual)"></i>7-day average</span>' +
      (refs.length ? '<span class="legend__item"><i class="swatch swatch--dash"></i>Reference lines</span>' : '');
  }

  // viewBox for a chart host. The height comes from the host's CSS --chart-h (styles.css owns
  // the heights and the width threshold), scaled so the drawn SVG is exactly that tall even
  // when the viewBox is wider than the host (narrow phones, W floor of 300).
  function chartBox(host) {
    var cw = host.clientWidth || 600, W = Math.max(300, cw);
    var h = parseFloat(getComputedStyle(host).getPropertyValue('--chart-h')) || 240;
    return { W: W, H: Math.round(h * W / cw * 10) / 10 };
  }

  function renderChartA(T) {
    var host = $('#a-chart');
    host.innerHTML = '';
    var all = T.rows;
    if (!all.length) { host.innerHTML = '<p class="empty">No transit rows in the data file yet.</p>'; return; }
    var rows = state.range === 'all' ? all : all.slice(-(+state.range));
    var binned = rows.length > 200;
    if (binned) rows = binWeekly(rows);
    var box = chartBox(host), W = box.W, H = box.H;
    var o = { left: 34, right: W - 4, top: 10, bottom: H - 24 };
    var allRefs = state.refs;
    $('#scale-seg').hidden = !allRefs.length;
    var useRefs = state.scale === 'refs' ? allRefs : [];
    var dataMax = Math.max.apply(null, rows.map(function (r) { return r.total; }));
    var maxV = Math.max.apply(null, [dataMax, 5].concat(useRefs.map(function (r) { return r.value; })));
    o.yMax = niceMax(maxV * 1.05);
    var refs = allRefs.filter(function (r) { return r.value <= o.yMax; });
    var hiddenRefs = allRefs.length - refs.length;
    o.y = function (v) { return o.bottom - (v / o.yMax) * (o.bottom - o.top); };
    var n = rows.length, band = (o.right - o.left) / n;
    o.x = function (i) { return o.left + band * (i + 0.5); };
    o.dates = rows.map(function (r) { return r.date; });

    var s = svg('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img',
      'aria-label': 'Daily transits through the ' + STRAITS[state.strait].title + ' from ' + day(rows[0].date) + ' to ' + day(rows[n - 1].date) +
        ', stacked by ship type, with the 7-day average. Latest day ' + day(rows[n - 1].date) + ': ' + rows[n - 1].total + ' ships. Use the table below for exact values.' }, host);
    var g = svg('g', {}, s);
    drawFrame(g, o);

    var bw = Math.max(1, band * (band > 6 ? 0.72 : 0.84));
    var gap = bw >= 4 ? 1 : 0;
    var bars = svg('g', {}, s);
    rows.forEach(function (r, i) {
      var y0 = o.bottom, x = o.x(i) - bw / 2;
      TYPES.forEach(function (t) {
        var v = +r.by_type[t.key] || 0;
        if (!v) return;
        var h = (v / o.yMax) * (o.bottom - o.top);
        svg('rect', { x: x, y: y0 - h, width: bw, height: Math.max(0, h - gap), fill: t.color }, bars);
        y0 -= h;
      });
    });
    drawRefs(g, refs, o);
    // 7-day average line with a surface halo so it reads over the bars
    var d = '', started = false;
    rows.forEach(function (r, i) {
      if (r.ma7 == null) { started = false; return; }
      d += (started ? 'L' : 'M') + o.x(i).toFixed(1) + ' ' + o.y(r.ma7).toFixed(1);
      started = true;
    });
    if (d) { svg('path', { class: 'ma-halo', d: d }, s); svg('path', { class: 'ma-line', d: d }, s); }

    attachHover(host, s, rows.map(function (r, i) {
      var html = r.binned
        ? '<div class="tooltip__date">' + esc(day(r.start)) + ' – ' + esc(day(r.date)) + '</div><div class="tooltip__row"><span>Daily average for the week</span></div>' +
          TYPES.slice().reverse().map(function (t) { return tipRow(t.color, t.label, num(r.by_type[t.key], 1)); }).join('') +
          tipRow(null, 'Total per day', num(r.total, 1), 'tooltip__row--total')
        : '<div class="tooltip__date">' + esc(day(r.date)) + '</div>' +
          TYPES.slice().reverse().map(function (t) { return tipRow(t.color, t.label, num(r.by_type[t.key])); }).join('') +
          tipRow(null, 'Total', num(r.total), 'tooltip__row--total') +
          tipRow(null, '7-day average', r.ma7 == null ? '—' : num(r.ma7, 1));
      return { x: o.x(i), html: html };
    }), o.top, o.bottom);

    var span = day(binned ? rows[0].start : rows[0].date) + ' – ' + day(rows[n - 1].date);
    var note = 'Showing ' + span + (binned ? ', as weekly bars (daily average per week).' : '.');
    if (allRefs.length) {
      note += refs.length ? ' Dashed reference lines (ships/day): ' + refs.map(function (r) { return r.label; }).join(', ') + '.' : '';
      if (hiddenRefs) note += ' Scale fitted to the data: ' + (hiddenRefs === allRefs.length ? 'all' : hiddenRefs) + ' reference line' + (hiddenRefs > 1 ? 's sit' : ' sits') + ' above the top of the chart.';
    }
    $('#a-note').textContent = note;
  }

  function renderTableA(T) {
    var rows = T.rows.slice().reverse();
    $('#a-table').innerHTML = '<table><caption class="sr-only">Daily transits by ship type, newest first</caption><thead><tr><th scope="col">Date</th>' +
      TYPES.map(function (t) { return '<th scope="col">' + t.label + '</th>'; }).join('') +
      '<th scope="col">Total</th><th scope="col">7-day avg</th></tr></thead><tbody>' +
      rows.map(function (r) {
        return '<tr><th scope="row" style="font-weight:500">' + esc(day(r.date)) + '</th>' +
          TYPES.map(function (t) { return '<td>' + num(r.by_type[t.key]) + '</td>'; }).join('') +
          '<td><strong>' + num(r.total) + '</strong></td><td>' + (r.ma7 == null ? '—' : num(r.ma7, 1)) + '</td></tr>';
      }).join('') + '</tbody></table>';
  }

  // ---- Forecast panel ------------------------------------------------------
  function renderForecast(res, T) {
    var host = $('#f-body');
    if (!res.ok) {
      host.innerHTML = '<p class="empty">No forecast file yet (<code>data/forecast/' + esc(state.strait) + '.json</code>). It appears here once the model publishes.</p>';
      return;
    }
    var F = res.data, rg = REGIME[F.regime] || null;
    var reasons = (F.regime_reasons || []).map(function (r) { return '<li>' + prettyDates(r) + '</li>'; }).join('');
    var wt = F.weekly_total || {};
    var daily = (F.daily || []).slice().sort(function (a, b) { return a.date < b.date ? -1 : 1; });
    var first = daily[0], lastD = daily[daily.length - 1];
    var bt = F.backtest || null;

    host.innerHTML =
      '<div class="f-top"><div class="regime-block">' +
        (rg ? '<span class="regime regime--' + esc(F.regime) + '"><svg viewBox="0 0 16 16" aria-hidden="true">' + rg.icon + '</svg>Regime: ' + rg.label + '</span>'
            : '<span class="regime">Regime: ' + esc(F.regime || 'unknown') + '</span>') +
        (reasons ? '<ul class="regime-reasons" aria-label="Why">' + reasons + '</ul>' : '') +
      '</div><p class="stamp">Forecast issued ' + esc(stamp(F.issued_at)) + '<br>' + methodLine(F) + '</p></div>' +
      '<div class="legend"><span class="legend__item"><i class="swatch swatch--line" style="background:var(--actual)"></i>Measured daily count</span>' +
        '<span class="legend__item"><i class="swatch swatch--band"></i>Likely range (80%)</span>' +
        '<span class="legend__item"><i class="swatch swatch--line" style="background:var(--s1)"></i>Central estimate</span></div>' +
      '<div class="chart" id="f-chart" tabindex="0" role="group" aria-label="Forecast chart: measured counts, then the next 7 days" aria-describedby="f-hint"></div>' +
      '<p class="chart-hint" id="f-hint">Hover or tap the chart to read a day. With a keyboard, focus the chart and use the left and right arrow keys.</p>' +
      '<div class="f-grid">' +
        '<div class="f-card"><p class="tile__label">Total transits, ' + esc(first ? day(first.date) : '') + ' – ' + esc(lastD ? day(lastD.date) : '') + '</p>' +
          '<p class="range">' + num(wt.p50) + ' <small>likely ' + num(wt.p10) + '–' + num(wt.p90) + '</small></p>' +
          byTypeTable(F.by_type, F.by_type_note) + '</div>' +
        '<div class="f-card">' + backtestHtml(bt, F) + '</div>' +
      '</div>' +
      (F.method_note ? '<p class="method"><strong>Method.</strong> ' + prettyDates(F.method_note) + '</p>' : '');

    drawForecastChart($('#f-chart'), daily, T);
  }

  function methodLine(F) {
    var used = (F.backtest && F.backtest.method_used) || F.method;
    var kind = F.backtest && NAIVE[F.backtest.naive_kind];
    if (used === 'model') return 'Range from: <strong>the weighted model</strong>';
    if (used === 'naive_baseline') return 'Range from: <strong>a simple guess</strong>' + (kind ? ' (' + esc(kind) + ')' : '');
    return '';
  }

  function byTypeTable(bt, note) {
    if (!bt) return '';
    var rows = TYPES.filter(function (t) { return bt[t.key]; }).map(function (t) {
      var v = bt[t.key];
      return '<tr><th scope="row" style="font-weight:500"><i class="swatch" style="display:inline-block;margin-right:6px;background:' + t.color + '"></i>' + t.label + '</th><td>' + num(v.p50) + '</td><td>' + num(v.p10) + '–' + num(v.p90) + '</td></tr>';
    }).join('');
    if (!rows) return '';
    return '<div class="table-scroll" style="margin-top:10px;max-height:none"><table><caption class="sr-only">Forecast 7-day totals by ship type</caption><thead><tr><th scope="col">Ship type</th><th scope="col">Central</th><th scope="col">Likely range</th></tr></thead><tbody>' + rows + '</tbody></table></div>' +
      '<p class="fine-print">Each ship type is forecast on its own, so the type figures do not add up exactly to the total, and their ranges do not add up to the total range.</p>';
  }

  function windowText(w) {
    if (!w) return 'the test period';
    if (typeof w === 'string') return w;
    if (w.start && w.end) return day(w.start) + ' – ' + day(w.end);
    return 'the test period';
  }

  var NAIVE = { naive_ma7: 'last 7-day average, held flat', naive_last: 'last day, held flat' };
  /* D6: the verdict is computed by the backend only (#3). The page maps its code to words
     and never applies a rule of its own. */
  var OUTCOME = {
    model_clearly_better: 'Model clearly better',
    no_meaningful_difference: 'No meaningful difference',
    naive_better: 'Simple guess did better',
    naive_clearly_better: 'Simple guess did better'
  };
  function backtestHtml(bt, F) {
    if (!bt || bt.mae_model == null || bt.mae_naive == null) return '<p class="tile__label">How good is it?</p><p class="muted">No backtest published yet.</p>';
    var m = +bt.mae_model, nv = +bt.mae_naive, max = Math.max(m, nv);
    var naiveLabel = (bt.naive_label || NAIVE[bt.naive_kind] || 'nothing changes').toLowerCase();
    var cov = bt.coverage_80 != null ? +bt.coverage_80 * (bt.coverage_80 <= 1 ? 100 : 1) : null;
    var used = bt.method_used || (F && F.method) || null;
    var shownIsModel = used === 'model';
    var code = String(bt.outcome || '');
    var head = OUTCOME[code] || (code ? code.replace(/_/g, ' ') : null);
    var test = 'Over this period the weighted model missed the real count by ' + num(m, 1) + ' ships a day on average; a simple guess (' + esc(naiveLabel) + ') missed by ' + num(nv, 1) + '.';
    var why = used == null ? ''
      : shownIsModel
        ? (code === 'model_clearly_better' ? 'The model was clearly better, so the range above comes from the model.'
                                           : 'The range above comes from the model.')
        : (code === 'model_clearly_better' ? 'The range above is the simple guess.'
                                           : code === 'naive_clearly_better' || code === 'naive_better'
                                             ? 'The simple guess was clearly better, so the range above is the simple guess (' + esc(naiveLabel) + ').'
                                             : 'The model was not clearly better, so the range above is the simple guess (' + esc(naiveLabel) + ').');
    return '<p class="tile__label">How good is it? Tested on ' + esc(windowText(bt.window)) + '</p>' +
      (head ? '<p class="verdict">' + esc(head) + '</p>' : '') +
      '<div class="kpi-compare" role="img" aria-label="Average daily miss: weighted model ' + num(m, 1) + ', simple guess ' + num(nv, 1) + ' ships per day">' +
        '<span>Weighted model</span><span class="kpi-bar"><span style="width:' + (m / max * 100) + '%;background:var(--s1)"></span></span><strong>' + num(m, 1) + '</strong>' +
        '<span>Simple guess</span><span class="kpi-bar"><span style="width:' + (nv / max * 100) + '%;background:var(--text-3)"></span></span><strong>' + num(nv, 1) + '</strong>' +
      '</div><p class="muted" style="font-size:12px;margin-top:-4px">Average daily miss, ships per day. Shorter is better.</p>' +
      '<p class="muted">' + test + '</p>' +
      (why ? '<p class="muted">' + why + '</p>' : '') +
      (bt.verdict ? '<p class="fine-print"><strong>Detail.</strong> ' + prettyDates(bt.verdict) + '</p>' : '') +
      (cov != null ? '<p class="muted">The real count fell inside the shaded band on ' + num(cov) + '% of test days. A well-sized band would hold about 80%.</p>' : '');
  }

  function drawForecastChart(host, daily, T) {
    host.innerHTML = '';
    if (!daily.length) { host.innerHTML = '<p class="empty">The forecast file has no daily rows.</p>'; return; }
    var hist = T && T.rows ? T.rows.filter(function (r) { return r.date < daily[0].date; }).slice(-28) : [];
    var dates = hist.map(function (r) { return r.date; }).concat(daily.map(function (r) { return r.date; }));
    var box = chartBox(host), W = box.W, H = box.H;
    var o = { left: 34, right: W - 4, top: 10, bottom: H - 24, dates: dates };
    var useRefs = state.scale === 'refs' ? state.refs : [];
    var maxV = Math.max.apply(null, [5].concat(hist.map(function (r) { return r.total; }), daily.map(function (r) { return r.p90; }), useRefs.map(function (r) { return r.value; })));
    o.yMax = niceMax(maxV * 1.05);
    var refs = state.refs.filter(function (r) { return r.value <= o.yMax; });
    o.y = function (v) { return o.bottom - (v / o.yMax) * (o.bottom - o.top); };
    var n = dates.length, band = (o.right - o.left) / n;
    o.x = function (i) { return o.left + band * (i + 0.5); };
    var h0 = hist.length;

    var s = svg('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img',
      'aria-label': 'Measured daily transits for the last ' + h0 + ' days, then the forecast range from ' + day(daily[0].date) + ' to ' + day(daily[daily.length - 1].date) + '.' }, host);
    var g = svg('g', {}, s);
    drawFrame(g, o);
    drawRefs(g, refs, o);

    // band: from the last measured point into the forecast days
    var up = '', lo = '', loEdge = '';
    daily.forEach(function (r, i) {
      var x = o.x(h0 + i).toFixed(1);
      up += (i ? 'L' : 'M') + x + ' ' + o.y(r.p90).toFixed(1);
      lo = 'L' + x + ' ' + o.y(r.p10).toFixed(1) + lo;
      loEdge += (i ? 'L' : 'M') + x + ' ' + o.y(r.p10).toFixed(1);
    });
    svg('path', { class: 'band', d: up + lo + 'Z' }, s);
    // thin edges so the band's extent reads even where the faint fill does not
    svg('path', { class: 'band-edge', d: up }, s);
    svg('path', { class: 'band-edge', d: loEdge }, s);
    var p50 = '';
    if (h0) p50 = 'M' + o.x(h0 - 1).toFixed(1) + ' ' + o.y(hist[h0 - 1].total).toFixed(1);
    daily.forEach(function (r, i) { p50 += (p50 ? 'L' : 'M') + o.x(h0 + i).toFixed(1) + ' ' + o.y(r.p50).toFixed(1); });
    svg('path', { class: 'p50-line', d: p50 }, s);
    if (h0) {
      var a = '';
      hist.forEach(function (r, i) { a += (i ? 'L' : 'M') + o.x(i).toFixed(1) + ' ' + o.y(r.total).toFixed(1); });
      svg('path', { class: 'actual-line', d: a }, s);
      var dx = (o.x(h0 - 1) + o.x(h0)) / 2;
      svg('line', { class: 'divider', x1: dx, x2: dx, y1: o.top, y2: o.bottom, 'stroke-dasharray': '2 3' }, s);
      var lt = svg('text', { x: dx + 4, y: o.bottom - 6 }, s); lt.textContent = 'Forecast';
    }

    attachHover(host, s, dates.map(function (dt, i) {
      var html;
      if (i < h0) {
        html = '<div class="tooltip__date">' + esc(day(dt)) + ' · measured</div>' + tipRow('var(--actual)', 'Ships', num(hist[i].total));
      } else {
        var r = daily[i - h0];
        html = '<div class="tooltip__date">' + esc(day(dt)) + ' · forecast</div>' +
          tipRow('var(--s1)', 'Central', num(r.p50, 1)) + tipRow(null, 'Likely range', num(r.p10, 1) + '–' + num(r.p90, 1));
      }
      return { x: o.x(i), html: html };
    }), o.top, o.bottom);
  }

  // ---- Panel B: rules + countdown -----------------------------------------
  var tzFmt = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23', weekday: 'short' });
  function nyParts(d) {
    var p = {};
    tzFmt.formatToParts(d).forEach(function (x) { p[x.type] = x.value; });
    return p;
  }
  function nyOffset(d) { // ms to add to UTC to get New York wall time
    var p = nyParts(d);
    return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute, +p.second) - Math.floor(d.getTime() / 1000) * 1000;
  }
  function nextPublish(now) {
    var p = nyParts(now);
    var wd = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].indexOf(p.weekday);
    var ahead = (2 - wd + 7) % 7;
    if (ahead === 0 && (+p.hour % 24) >= 9) ahead = 7;
    var wall = Date.UTC(+p.year, +p.month - 1, +p.day + ahead, 9, 0, 0);
    var off = nyOffset(new Date(wall - nyOffset(now)));
    return new Date(wall - off);
  }
  function renderCountdown() {
    var el = $('#b-countdown'), now = new Date(), next = nextPublish(now);
    var ms = next - now, dd = Math.floor(ms / 864e5), hh = Math.floor(ms % 864e5 / 36e5), mm = Math.floor(ms % 36e5 / 6e4);
    var prev = new Date(next.getTime() - 7 * 864e5);
    el.innerHTML = '<div><p class="countdown__label">Next IMF PortWatch publish (weekly, Tuesdays 9 AM New York time)</p>' +
      '<p class="countdown__when">' + esc(fmtWkDay.format(next)) + ' ' + next.getUTCFullYear() + ', ' + esc(fmtTimeUTC.format(next)) + ' UTC</p>' +
      '<p class="countdown__label">Previous: ' + esc(fmtWkDay.format(prev)) + '. Times are the usual schedule; check the source.</p></div>' +
      '<p class="countdown__left" aria-label="in ' + dd + ' days ' + hh + ' hours ' + mm + ' minutes">' + dd + 'd ' + String(hh).padStart(2, '0') + 'h ' + String(mm).padStart(2, '0') + 'm</p>';
  }

  var CMP = {
    '>=': ['at least', '≥'], '>': ['more than', '>'], '<=': ['at most', '≤'], '<': ['fewer than', '<'],
    '≥': ['at least', '≥'], '≤': ['at most', '≤']
  };
  // A rule row has one threshold, or (ladder markets) a list of strikes, one question each.
  function conditionHtml(r, c) {
    if (r.threshold != null) return '<span class="cmp">' + esc(c[0]) + ' ' + num(r.threshold) + ' <small>(' + esc(c[1]) + ' ' + num(r.threshold) + ')</small></span>';
    var k = Array.isArray(r.strikes) ? r.strikes.filter(function (x) { return x != null && !isNaN(x); }) : [];
    if (!k.length || !r.comparison) return '';
    return '<span class="cmp">' + esc(c[0]) + ' <small>(' + esc(c[1]) + ')</small> each of ' + k.map(function (x) { return num(x, x % 1 ? 1 : 0); }).join(', ') + '</span>' +
      '<br><small style="color:var(--text-2)">One question per number in the list.</small>';
  }
  // Notes may hold long verbatim rule quotes: fold anything past a short paragraph.
  function notesHtml(n) {
    if (!n) return '';
    n = String(n);
    if (n.length <= 240) return esc(n);
    return '<details class="rule-notes"><summary><span class="rule-notes__preview">' + esc(n.slice(0, 140).replace(/\s+\S*$/, '')) + '… </span>' +
      '<span class="rule-notes__more">Show full text</span><span class="rule-notes__less">Hide full text</span></summary><p>' + esc(n) + '</p></details>';
  }

  function renderRules(res) {
    var host = $('#b-rules');
    if (!res.ok) { host.innerHTML = '<p class="empty">No rules file yet (<code>data/rules.json</code>).</p>'; return; }
    var list = (res.data.rules || []).filter(function (r) { return !r.strait || r.strait === state.strait; });
    if (!list.length) { host.innerHTML = '<p class="empty">No rules recorded for this strait yet.</p>'; return; }
    host.innerHTML = list.map(function (r) {
      var c = CMP[r.comparison] || [r.comparison || '', r.comparison || ''];
      var url = safeUrl(r.url);
      var row = function (k, v) { return v ? '<dt>' + k + '</dt><dd>' + v + '</dd>' : ''; };
      // Collapsed to its title by default; the summary is the card header.
      return '<details class="rule' + (r.example ? ' rule--example' : '') + '">' +
        '<summary class="rule__head"><span class="rule__title"><h3>' + (r.example ? '<span class="tag-example">EXAMPLE</span>' : '') + esc(r.market_family || 'Rule') + '</h3>' +
        '<span class="rule__venue">' + [esc(r.venue || ''), r.read_on ? 'read ' + esc(day(r.read_on)) : 'read date not recorded'].filter(Boolean).join(' · ') + '</span></span><span class="rule__chev" aria-hidden="true"></span></summary><dl>' +
        row('Settles on', esc(r.settlement_source)) +
        row('Field', esc(r.field)) +
        row('Counted as', esc(r.aggregation)) +
        row('Condition', conditionHtml(r, c)) +
        row('Window', esc(r.window) + (r.window_start && r.window_end ? ' <span style="color:var(--text-2)">(' + esc(day(r.window_start)) + ' – ' + esc(day(r.window_end)) + ')</span>' : '')) +
        row('Earliest settlement', r.earliest_settlement ? esc(day(r.earliest_settlement)) : '') +
        row('Notes', notesHtml(r.notes)) +
        '</dl><div class="rule__foot"><span>Rule text read on <strong>' + esc(day(r.read_on)) + '</strong></span>' +
        (url ? '<a href="' + esc(url) + '" target="_blank" rel="noopener">Source rule text ↗</a>' : '<span>No link recorded</span>') + '</div></details>';
    }).join('');
  }

  // ---- Panel C: ledger -----------------------------------------------------
  function renderLedger(res) {
    var host = $('#c-ledger'), sum = $('#c-summary');
    if (!res.ok) { sum.innerHTML = ''; host.innerHTML = '<p class="empty">No ledger file yet (<code>data/ledger.json</code>).</p>'; return; }
    var L = res.data, wd = L.window_days || 14, end = L.updated_at ? L.updated_at.slice(0, 10) : null;
    var start = end ? addDays(end, -(wd - 1)) : null;
    var rows = (L.rows || []).filter(function (r) {
      if (r.strait && r.strait !== state.strait) return false;
      if (r.example) return true;
      return !start || (r.date >= start && r.date <= end);
    }).sort(function (a, b) { return a.date < b.date ? 1 : -1; });
    var real = rows.filter(function (r) { return !r.example; });
    var counts = { claimed_only: 0, third_party_report: 0, confirmed: 0 };
    real.forEach(function (r) { if (counts[r.status] != null) counts[r.status]++; });
    sum.innerHTML = start
      ? (real.length
          ? '<strong>' + real.length + '</strong> incident' + (real.length > 1 ? 's' : '') + ' recorded, ' + esc(day(start)) + ' – ' + esc(day(end)) + ': ' +
            counts.claimed_only + ' claimed only, ' + counts.third_party_report + ' third-party report' + (counts.third_party_report === 1 ? '' : 's') + ', ' + counts.confirmed + ' confirmed.'
          : 'No incidents reported in the last ' + wd + ' days (' + esc(day(start)) + ' – ' + esc(day(end)) + ').') + ' Ledger last updated ' + esc(day(end)) + '.'
      : '';
    if (!rows.length) { host.innerHTML = start ? '' : '<p class="empty">No incidents reported for this strait.</p>'; return; }
    host.innerHTML = '<div class="ledger">' + rows.map(function (r) {
      var links = (r.links || []).map(function (l) {
        var u = safeUrl(l.url);
        return u ? '<a href="' + esc(u) + '" target="_blank" rel="noopener">' + esc(l.label || 'Source') + ' ↗</a>' : '';
      }).join('');
      var st = STATUS[r.status] ? r.status : 'claimed_only';
      return '<article class="lrow' + (r.example ? ' lrow--example' : '') + '">' +
        '<div><p class="lrow__date">' + esc(day(r.date)) + '</p></div>' +
        '<div class="lrow__body"><p class="lrow__summary">' + (r.example ? '<span class="tag-example">EXAMPLE</span>' : '') + esc(r.summary || '') + '</p>' +
          '<p class="lrow__place">' + esc(r.place || '') + '</p>' +
          '<dl class="lrow__meta"><dt>Claimed by</dt><dd>' + esc(r.claimed_by || '—') + '</dd>' +
          '<dt>Independent</dt><dd>' + esc(r.independent_source || 'None yet') + '</dd></dl>' +
          (links ? '<div class="lrow__links">' + links + '</div>' : '') +
          (r.verified_on ? '<p class="lrow__verified">Checked against sources on ' + esc(day(r.verified_on)) + '</p>' : '') +
        '</div>' +
        '<div class="lrow__status"><span class="chip chip--' + st + '"><span class="chip__icon" aria-hidden="true"></span>' + STATUS[st] + '</span></div>' +
      '</article>';
    }).join('') + '</div>';
  }

  // ---- orchestration -------------------------------------------------------
  var token = 0;
  function load(strait) {
    var my = ++token;
    state.strait = strait;
    var cfg = STRAITS[strait];
    $('#strait-title').textContent = cfg.title;
    document.title = 'Chokepoint Desk · ' + cfg.title;
    renderLegendA();
    var pTr = getJSON('data/transits/' + strait + '.json');
    var pFc = getJSON('data/forecast/' + strait + '.json');
    // Each panel draws as soon as its own file is in; the small files never wait for the big one.
    getJSON('data/rules.json').then(function (r) { if (my === token) renderRules(r); });
    getJSON('data/ledger.json').then(function (r) { if (my === token) renderLedger(r); });
    pFc.then(function (fc) {
      if (my !== token) return;
      state.forecast = fc;
      state.refs = refsFor(strait, fc);
      renderForecast(fc, state.transits && state.transits.strait === strait ? state.transits : null);
    });
    Promise.all([pTr, pFc]).then(function (res) {
      if (my !== token) return;
      var tr = res[0], fc = res[1];
      renderLegendA();
      if (tr.ok) {
        var T = state.transits = normTransits(tr.data);
        T.strait = strait;
        $('#data-stamp').innerHTML = 'Transit data through <strong>' + esc(day(T.last_data_date)) + '</strong> · fetched ' + esc(stamp(T.fetched_at)) + ' · source: ' + esc(T.source) +
          staleNote(T.last_data_date) +
          (tr.data.offline === true ? '<span class="offline-note" role="note">PortWatch was unavailable at the last refresh; showing data fetched on ' + esc(stamp(T.fetched_at)) + '. Counts run to ' + esc(day(T.last_data_date)) + '.</span>' : '');
        renderTiles(T); renderChartA(T); syncTable();
        if (fc.ok) { var fh = $('#f-chart'); if (fh) drawForecastChart(fh, sortedDaily(fc.data), T); }
      } else {
        state.transits = null;
        $('#data-stamp').textContent = 'Transit data not available yet.';
        $('#a-tiles').innerHTML = ''; $('#a-tiles').hidden = true; $('#a-table').innerHTML = ''; $('#a-note').textContent = '';
        $('#a-chart').innerHTML = '<p class="empty">No transit file yet (<code>data/transits/' + esc(strait) + '.json</code>).</p>';
      }
      var mock = [];
      if (tr.ok && isMock(tr.data.source)) mock.push('transit counts');
      if (fc.ok && isMock(fc.data.method_note, fc.data.source)) mock.push('forecast');
      $('#mock-banner').hidden = !mock.length;
      $('#mock-text').textContent = mock.length ? 'The ' + mock.join(' and ') + ' on this page are synthetic placeholders for layout, not real measurements.' : '';
    });
  }

  function sortedDaily(F) { return (F.daily || []).slice().sort(function (a, b) { return a.date < b.date ? -1 : 1; }); }

  // D7: PortWatch publishes weekly (Tuesdays, about 9 AM New York time), so a newest count
  // up to STALE_DAYS old is normal and gets a calm note; older than that, a red warning.
  var STALE_DAYS = 10;
  var fmtWeekday = new Intl.DateTimeFormat('en-GB', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' });
  // Next expected release: the first Tuesday from today (UTC) that comes more than two days
  // after the newest count (a Tuesday release runs to about the Sunday before it).
  function nextRelease(lastDay, now) {
    var t = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
    t.setUTCDate(t.getUTCDate() + (9 - t.getUTCDay()) % 7);
    while (t.getTime() - lastDay.getTime() <= 2 * 864e5) t.setUTCDate(t.getUTCDate() + 7);
    return t;
  }
  function staleNote(lastDate, nowMs) {
    var d = parseDate(lastDate);
    if (!d) return '';
    var now = new Date(nowMs == null ? Date.now() : nowMs);
    var age = Math.max(0, Math.floor((now.getTime() - d.getTime()) / 864e5));
    if (age > STALE_DAYS) {
      return '<span class="stale-note" role="note"><strong>Stale data.</strong> The newest count is from ' + esc(day(lastDate)) + ', ' + age + ' days ago, longer than PortWatch\'s weekly cycle. The daily refresh may have failed; treat every number below as out of date.</span>';
    }
    var ago = age === 0 ? 'today' : age === 1 ? '1 day ago' : age + ' days ago';
    var rel = nextRelease(d, now);
    var today = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
    var when = rel.getTime() === today.getTime() ? 'today (Tuesday)' : fmtWeekday.format(rel).replace(',', '');
    return '<span class="cadence-note" role="note">PortWatch publishes weekly; the newest count is from ' + esc(dayShort(lastDate)) + ' (' + ago + '). Next release expected ' + esc(when) + '.</span>';
  }

  // The full daily table (thousands of rows) is only built when someone opens it.
  var tableFor = null;
  function syncTable() {
    var det = $('#a-table').closest('details');
    if (det && det.open && state.transits && tableFor !== state.transits) {
      renderTableA(state.transits);
      tableFor = state.transits;
    } else if (tableFor !== state.transits) {
      $('#a-table').innerHTML = '';
    }
  }

  function redrawCharts() {
    if (state.transits) renderChartA(state.transits);
    if (state.forecast && state.forecast.ok) {
      var host = $('#f-chart');
      if (host) drawForecastChart(host, sortedDaily(state.forecast.data), state.transits);
    }
  }

  function init() {
    var initial = (location.hash || '').slice(1);
    if (!STRAITS[initial]) initial = 'hormuz';
    document.querySelectorAll('input[name="strait"]').forEach(function (inp) {
      inp.checked = inp.value === initial;
      inp.addEventListener('change', function () {
        if (!inp.checked) return;
        history.replaceState(null, '', '#' + inp.value);
        hideTip();
        load(inp.value);
      });
    });
    document.querySelectorAll('[data-range]').forEach(function (b) {
      b.addEventListener('click', function () {
        state.range = b.getAttribute('data-range');
        document.querySelectorAll('[data-range]').forEach(function (x) { x.setAttribute('aria-pressed', String(x === b)); });
        if (state.transits) renderChartA(state.transits);
      });
    });
    $('#a-table').closest('details').addEventListener('toggle', syncTable);
    document.querySelectorAll('[data-scale]').forEach(function (b) {
      b.addEventListener('click', function () {
        state.scale = b.getAttribute('data-scale');
        document.querySelectorAll('[data-scale]').forEach(function (x) { x.setAttribute('aria-pressed', String(x === b)); });
        if (state.transits) renderChartA(state.transits);
      });
    });
    window.addEventListener('hashchange', function () {
      var h = location.hash.slice(1);
      if (STRAITS[h] && h !== state.strait) { $('input[value="' + h + '"]').checked = true; load(h); }
    });
    document.addEventListener('pointerdown', function (e) { if (!e.target.closest('.chart')) hideTip(); });
    window.addEventListener('scroll', hideTip, { passive: true });
    var lastW = 0, t;
    window.addEventListener('resize', function () {
      clearTimeout(t);
      t = setTimeout(function () { var w = document.documentElement.clientWidth; if (w !== lastW) { lastW = w; redrawCharts(); } }, 120);
    });
    lastW = document.documentElement.clientWidth;
    renderCountdown();
    setInterval(renderCountdown, 30000);
    load(initial);
  }

  init();
})();
