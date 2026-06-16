// Finance Lodger — Chart.js theming and chart constructors.
//
// Expects:
//   1) Chart.js v4 loaded globally as `Chart` (CDN tag in the HTML).
//   2) `window.FINANCE_DATA` injected by the render skill with shape:
//        {
//          netWorth:    { dates: [...], values: [...] },              // ILS, time series
//          stocksVsSpy: { dates: [...], stocks: [...], spy: [...],
//                         cumulativeDeposits: [...] },                 // all aligned
//          payslips:    { months: [...], gross: [...], net: [...] }
//        }
//   3) <canvas> elements with IDs: chart-networth, chart-stocks-vs-spy, chart-payslips.

(() => {
  const PALETTE = {
    parchment: '#f5efe3',
    ink:       '#2a2520',
    mutedInk:  '#6b6357',
    hairline:  'rgba(201, 191, 170, 0.5)',
    ribbon:    '#8b1a1a',
    gold:      '#a8924d',
    positive:  '#3a5a40',
    negative:  '#8b1a1a',
  };

  // Global Chart.js defaults
  Chart.defaults.font.family = "'EB Garamond', Georgia, serif";
  Chart.defaults.font.size = 13;
  Chart.defaults.color = PALETTE.ink;
  Chart.defaults.plugins.legend.labels.color = PALETTE.mutedInk;
  Chart.defaults.plugins.legend.labels.usePointStyle = false;
  Chart.defaults.plugins.legend.labels.boxWidth = 24;
  Chart.defaults.plugins.legend.labels.boxHeight = 2;
  Chart.defaults.plugins.tooltip.backgroundColor = PALETTE.parchment;
  Chart.defaults.plugins.tooltip.titleColor = PALETTE.ink;
  Chart.defaults.plugins.tooltip.bodyColor = PALETTE.ink;
  Chart.defaults.plugins.tooltip.borderColor = PALETTE.hairline;
  Chart.defaults.plugins.tooltip.borderWidth = 1;
  Chart.defaults.plugins.tooltip.boxPadding = 6;
  Chart.defaults.plugins.tooltip.cornerRadius = 0;
  Chart.defaults.plugins.tooltip.displayColors = false;
  Chart.defaults.plugins.tooltip.titleFont = { family: "'Cormorant Garamond', serif", weight: '500', size: 13 };
  Chart.defaults.elements.line.tension = 0.2;
  Chart.defaults.elements.line.borderWidth = 1.5;
  Chart.defaults.elements.point.radius = 0;
  Chart.defaults.elements.point.hoverRadius = 4;

  const axisStyle = {
    grid:   { color: PALETTE.hairline, drawBorder: false, drawTicks: false },
    ticks:  { color: PALETTE.mutedInk, padding: 8 },
    border: { display: false },
  };

  const moneyFmt = (v, currency = 'ILS') => {
    const sign = v < 0 ? '-' : '';
    const abs = Math.abs(v);
    return `${sign}${abs.toLocaleString('en-IL', { maximumFractionDigits: 0 })} ${currency}`;
  };

  function render() {
    const D = window.FINANCE_DATA || {};
    if (D.netWorth) renderNetWorth(D.netWorth);
    if (D.stocksVsSpy) renderStocksVsSpy(D.stocksVsSpy);
    if (D.fxDeposits) renderFxDeposits(D.fxDeposits);
    if (D.dividends) renderDividends(D.dividends);
    if (D.payslips) renderPayslips(D.payslips);
    if (D.expenses) renderExpenses(D.expenses);
  }

  function renderNetWorth(s) {
    const el = document.getElementById('chart-networth');
    if (!el || !s.dates?.length) return;
    new Chart(el, {
      type: 'line',
      data: {
        labels: s.dates,
        datasets: [{
          label: 'Net worth (ILS)',
          data: s.values,
          borderColor: PALETTE.ink,
          backgroundColor: 'transparent',
        }],
      },
      options: {
        responsive: true,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            bodyFont: { family: "'EB Garamond', Georgia, serif", size: 12 },
            footerFont: { family: "'EB Garamond', Georgia, serif", size: 12, weight: '500' },
            footerColor: PALETTE.ink,
            footerMarginTop: 8,
            padding: 10,
            callbacks: {
              label: () => '',
              afterBody: (ctxs) => {
                const idx = ctxs[0].dataIndex;
                const rows = s.breakdowns?.[idx] || [];
                return rows.map(([name, amt]) => `${name}: ${moneyFmt(amt, 'ILS')}`);
              },
              footer: (ctxs) => `Total: ${moneyFmt(ctxs[0].parsed.y, 'ILS')}`,
            },
          },
        },
        scales: {
          x: { ...axisStyle, type: 'time', time: { unit: 'month', tooltipFormat: 'MMM yyyy', displayFormats: { month: "MMM ''yy" } } },
          y: { ...axisStyle, ticks: { ...axisStyle.ticks, callback: v => moneyFmt(v, '') } },
        },
      },
    });
  }

  function renderStocksVsSpy(s) {
    const el = document.getElementById('chart-stocks-vs-spy');
    if (!el || !s.dates?.length) return;
    const allDates    = s.dates;
    const allStocks   = s.stocks;
    const allSpy      = s.spy;
    const allDeposits = s.cumulativeDeposits;

    const chart = new Chart(el, {
      type: 'line',
      data: {
        labels: allDates,
        datasets: [
          { label: 'Portfolio',           data: allStocks,   borderColor: PALETTE.ink,      backgroundColor: 'transparent' },
          { label: 'S&P 500 (if invested)', data: allSpy,    borderColor: PALETTE.mutedInk, borderDash: [4, 4], backgroundColor: 'transparent' },
          { label: 'Cumulative deposits', data: allDeposits, borderColor: PALETTE.gold,     backgroundColor: 'transparent' },
        ],
      },
      options: {
        responsive: true,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          tooltip: { callbacks: { label: ctx => `${ctx.dataset.label}: ${moneyFmt(ctx.parsed.y, 'USD')}` } },
        },
        scales: {
          x: { ...axisStyle, type: 'time', time: { unit: 'month', tooltipFormat: 'MMM yyyy', displayFormats: { month: "MMM ''yy" } } },
          y: { ...axisStyle, ticks: { ...axisStyle.ticks, callback: v => moneyFmt(v, '') } },
        },
      },
    });

    const buttons = el.closest('.chart-block')?.querySelectorAll('.chart-range') || [];
    const sliceFrom = (range) => {
      if (!allDates.length) return 0;
      const lastDate = new Date(allDates[allDates.length - 1]);
      let cutoff = null;
      if (range === '3m') {
        cutoff = new Date(lastDate); cutoff.setMonth(cutoff.getMonth() - 3);
      } else if (range === '1y') {
        cutoff = new Date(lastDate); cutoff.setFullYear(cutoff.getFullYear() - 1);
      } else if (range === 'ytd') {
        cutoff = new Date(Date.UTC(lastDate.getFullYear(), 0, 1));
      } else {
        return 0;
      }
      const idx = allDates.findIndex(d => new Date(d) >= cutoff);
      return idx < 0 ? 0 : idx;
    };
    buttons.forEach(btn => {
      btn.addEventListener('click', () => {
        buttons.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const start = sliceFrom(btn.dataset.range);
        chart.data.labels = allDates.slice(start);
        chart.data.datasets[0].data = allStocks.slice(start);
        chart.data.datasets[1].data = allSpy.slice(start);
        chart.data.datasets[2].data = allDeposits.slice(start);
        chart.update();
      });
    });
  }

  function renderFxDeposits(s) {
    const el = document.getElementById('chart-fx-deposits');
    if (!el || !s.dates?.length) return;

    const allDates = s.dates;
    const allUsd = s.usd;
    const allGbp = s.gbp;
    const allUsdDeps = s.usdDeposits || [];
    const allGbpDeps = s.gbpDeposits || [];

    const sliceFrom = (range) => {
      if (!allDates.length) return 0;
      const lastDate = new Date(allDates[allDates.length - 1]);
      let cutoff = null;
      if (range === '3m') { cutoff = new Date(lastDate); cutoff.setMonth(cutoff.getMonth() - 3); }
      else if (range === '1y') { cutoff = new Date(lastDate); cutoff.setFullYear(cutoff.getFullYear() - 1); }
      else if (range === 'ytd') { cutoff = new Date(Date.UTC(lastDate.getFullYear(), 0, 1)); }
      else return 0;
      const idx = allDates.findIndex(d => new Date(d) >= cutoff);
      return idx < 0 ? 0 : idx;
    };

    // Mutable views — the chart's datasets and the marker plugin share these.
    let dates = allDates, usd = allUsd, gbp = allGbp;
    let usdDeps = allUsdDeps, gbpDeps = allGbpDeps;
    let curRange = '1y';                          // marker plugin reads this; ALL = dots only
    const setSlice = (range) => {
      curRange = range;
      const start = sliceFrom(range);
      dates = allDates.slice(start);
      usd = allUsd.slice(start);
      gbp = allGbp.slice(start);
      const cutoffStr = allDates[start];
      usdDeps = allUsdDeps.filter(d => d.date >= cutoffStr);
      gbpDeps = allGbpDeps.filter(d => d.date >= cutoffStr);
    };
    // Default range matches the .active button in the template (1Y).
    setSlice('1y');

    // Latest non-null series value on or before `date` — used to anchor each
    // deposit's dot to the actual line, not to an interpolated phantom.
    const yAt = (date, series) => {
      let lo = 0, hi = dates.length - 1, idx = -1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (dates[mid] <= date) { idx = mid; lo = mid + 1; } else { hi = mid - 1; }
      }
      for (let i = idx; i >= 0; i--) if (series[i] != null) return series[i];
      return null;
    };

    const depositMarkers = {
      id: 'depositMarkers',
      afterDatasetsDraw(chart) {
        const ctx = chart.ctx;
        const xs = chart.scales.x, ys = chart.scales.y;
        const top = chart.chartArea.top, bottom = chart.chartArea.bottom;

        // ALL packs in too many deposits for readable labels — show dots only.
        const showLabels = curRange !== 'all';

        // Pass 1 — vertical guides (labelled views only) + dots; stash labels for Pass 2.
        const labels = [];
        const drawDots = (deposits, series, dotColor, currencyTag) => {
          deposits.forEach(d => {
            const t = new Date(d.date).valueOf();
            if (t < xs.min || t > xs.max) return;
            const x = xs.getPixelForValue(t);
            ctx.save();
            if (showLabels) {
              ctx.strokeStyle = 'rgba(168, 146, 77, 0.42)';
              ctx.lineWidth = 1;
              ctx.setLineDash([2, 4]);
              ctx.beginPath();
              ctx.moveTo(x, top);
              ctx.lineTo(x, bottom);
              ctx.stroke();
              ctx.setLineDash([]);
            }
            const v = yAt(d.date, series);
            if (v != null) {
              const y = ys.getPixelForValue(v);
              ctx.fillStyle = dotColor;
              ctx.beginPath();
              ctx.arc(x, y, 3.5, 0, Math.PI * 2);
              ctx.fill();
              ctx.strokeStyle = PALETTE.parchment;
              ctx.lineWidth = 1.5;
              ctx.stroke();
              if (showLabels) {
                // amount = ILS paid (native × rate at deposit); rate = that day's FX price.
                const ilsAmount = Math.round(d.amount * v).toLocaleString('en-IL');
                labels.push({ x, y, text: `${ilsAmount}₪ · ${currencyTag}${v.toFixed(2)}` });
              }
            }
            ctx.restore();
          });
        };
        drawDots(usdDeps, usd, PALETTE.ink, '$');
        drawDots(gbpDeps, gbp, PALETTE.ribbon, '£');

        if (!showLabels) return;   // ALL: dots only — no guides, no labels

        // Pass 2 — labels. Sort by x and alternate above/below so clustered
        // deposits stop overlapping; a backing pill + leader line keep each one
        // legible where it crosses the price lines.
        labels.sort((a, b) => a.x - b.x);
        ctx.save();
        ctx.font = "10px 'EB Garamond', Georgia, serif";
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        const HH = 6, PADX = 3, GAP = 18;
        labels.forEach((L, i) => {
          const above = (i % 2 === 0);
          let ly = above ? L.y - GAP : L.y + GAP;
          ly = Math.max(top + HH + 2, Math.min(bottom - HH - 2, ly));
          const w = ctx.measureText(L.text).width;
          ctx.strokeStyle = 'rgba(107, 99, 87, 0.55)';   // leader: dot edge -> pill edge
          ctx.lineWidth = 0.75;
          ctx.beginPath();
          ctx.moveTo(L.x, above ? L.y - 4 : L.y + 4);
          ctx.lineTo(L.x, above ? ly + HH : ly - HH);
          ctx.stroke();
          ctx.fillStyle = 'rgba(245, 239, 227, 0.85)';   // backing pill
          ctx.fillRect(L.x - w / 2 - PADX, ly - HH - 1, w + 2 * PADX, 2 * HH + 2);
          ctx.fillStyle = PALETTE.ink;
          ctx.fillText(L.text, L.x, ly);
        });
        ctx.restore();
      },
    };

    const chart = new Chart(el, {
      type: 'line',
      data: {
        labels: dates,
        datasets: [
          { label: 'USD → ILS', data: usd, borderColor: PALETTE.ink, backgroundColor: 'transparent', spanGaps: true },
          { label: 'GBP → ILS', data: gbp, borderColor: PALETTE.ribbon, backgroundColor: 'transparent', spanGaps: true, borderDash: [4, 3] },
        ],
      },
      options: {
        responsive: true,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          tooltip: {
            callbacks: {
              label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y != null ? ctx.parsed.y.toFixed(3) : '—'}`,
            },
          },
        },
        scales: {
          x: { ...axisStyle, type: 'time', time: { unit: 'month', tooltipFormat: 'MMM d, yyyy', displayFormats: { month: "MMM ''yy" } } },
          y: { ...axisStyle, ticks: { ...axisStyle.ticks, callback: v => v.toFixed(2) } },
        },
      },
      plugins: [depositMarkers],
    });

    const buttons = el.closest('.chart-block')?.querySelectorAll('.chart-range') || [];
    buttons.forEach(btn => {
      btn.addEventListener('click', () => {
        buttons.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        setSlice(btn.dataset.range);
        chart.data.labels = dates;
        chart.data.datasets[0].data = usd;
        chart.data.datasets[1].data = gbp;
        chart.update();
      });
    });
  }

  function renderDividends(s) {
    const el = document.getElementById('chart-dividends');
    if (!el || !s.months?.length || !s.series?.length) return;

    const ccy = s.currency || 'USD';
    const allMonths = s.months;
    const allSeries = s.series;               // [{ticker, data:[...]}, ...] — top 5 then "Other"

    const ABBR = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    const FULL = ['January','February','March','April','May','June','July','August','September','October','November','December'];
    // 'YYYY-MM' -> "Jun '24" (axis) / "June 2024" (tooltip title)
    const ymAxis  = (ym) => { const [y, m] = ym.split('-').map(Number); return `${ABBR[m - 1]} '${String(y).slice(2)}`; };
    const ymTitle = (ym) => { const [y, m] = ym.split('-').map(Number); return `${FULL[m - 1]} ${y}`; };
    // 'YYYY-Qn' -> "Q1 '24" (axis) / "Q1 2024" (tooltip title)
    const qAxis  = (q) => { const [y, n] = q.split('-Q'); return `Q${n} '${y.slice(2)}`; };
    const qTitle = (q) => { const [y, n] = q.split('-Q'); return `Q${n} ${y}`; };

    // Roll the monthly months + per-payer series up into calendar quarters,
    // preserving payer order so palette indexing stays put.
    const toQuarters = () => {
      const keys = [];
      const at = new Map();
      const qOf = (ym) => { const [y, m] = ym.split('-').map(Number); return `${y}-Q${Math.floor((m - 1) / 3) + 1}`; };
      allMonths.forEach((ym) => { const k = qOf(ym); if (!at.has(k)) { at.set(k, keys.length); keys.push(k); } });
      const series = allSeries.map((band) => {
        const data = new Array(keys.length).fill(0);
        band.data.forEach((v, i) => { data[at.get(qOf(allMonths[i]))] += (v || 0); });
        return { ticker: band.ticker, data: data.map((x) => Math.round(x * 100) / 100) };
      });
      return { labels: keys, series };
    };

    // 1Y = last 12 months (monthly bars); ALL = whole history, one bar per quarter.
    const viewFor = (range) => {
      if (range === 'all') return { mode: 'quarter', ...toQuarters() };
      const start = Math.max(0, allMonths.length - 12);
      return {
        mode: 'month',
        labels: allMonths.slice(start),
        series: allSeries.map((b) => ({ ticker: b.ticker, data: b.data.slice(start) })),
      };
    };

    // "Other" rides in a muted taupe; the top 5 take the shared category palette by rank.
    const bandColor = (label, i) => label === 'Other' ? '#948977' : categoryColor(i);
    const datasetsFrom = (series) => series.map((band, i) => ({
      label: band.ticker,
      data: band.data,
      backgroundColor: bandColor(band.ticker, i),
      borderColor: PALETTE.parchment,
      borderWidth: 0.5,
      borderRadius: 1,
      stack: 'dividends',
    }));

    let mode = 'month';                         // current bucket granularity (drives axis/tooltip labels)
    const fmtAxis  = (lbl) => mode === 'quarter' ? qAxis(lbl) : ymAxis(lbl);
    const fmtTitle = (lbl) => mode === 'quarter' ? qTitle(lbl) : ymTitle(lbl);

    const v0 = viewFor('1y');                   // default matches the active 1Y button
    mode = v0.mode;
    const chart = new Chart(el, {
      type: 'bar',
      data: { labels: v0.labels, datasets: datasetsFrom(v0.series) },
      options: {
        responsive: true,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: true, position: 'bottom' },
          tooltip: {
            bodyFont: { family: "'EB Garamond', Georgia, serif", size: 12 },
            footerFont: { family: "'EB Garamond', Georgia, serif", size: 12, weight: '500' },
            footerColor: PALETTE.ink,
            footerMarginTop: 8,
            padding: 10,
            filter: (item) => (item.parsed.y || 0) > 0,    // hide payers with nothing this period
            callbacks: {
              title: (ctxs) => ctxs.length ? fmtTitle(ctxs[0].label) : '',
              label: (ctx) => `${ctx.dataset.label}: ${moneyFmt(ctx.parsed.y, ccy)}`,
              footer: (ctxs) => `Total: ${moneyFmt(ctxs.reduce((t, c) => t + (c.parsed.y || 0), 0), ccy)}`,
            },
          },
        },
        scales: {
          x: {
            ...axisStyle,
            stacked: true,
            ticks: { ...axisStyle.ticks, maxRotation: 0, autoSkipPadding: 14,
                     callback(value) { return fmtAxis(this.getLabelForValue(value)); } },
          },
          y: { ...axisStyle, stacked: true, beginAtZero: true,
               ticks: { ...axisStyle.ticks, callback: v => moneyFmt(v, '') } },
        },
      },
    });

    const buttons = el.closest('.chart-block')?.querySelectorAll('.chart-range') || [];
    buttons.forEach(btn => {
      btn.addEventListener('click', () => {
        buttons.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const v = viewFor(btn.dataset.range);
        mode = v.mode;
        chart.data.labels = v.labels;
        chart.data.datasets = datasetsFrom(v.series);
        chart.update();
      });
    });
  }

  function renderPayslips(s) {
    const el = document.getElementById('chart-payslips');
    if (!el || !s.months?.length) return;
    new Chart(el, {
      type: 'line',
      data: {
        labels: s.months,
        datasets: [
          { label: 'Gross', data: s.gross, borderColor: PALETTE.mutedInk, borderDash: [4, 4], backgroundColor: 'transparent' },
          { label: 'Net',   data: s.net,   borderColor: PALETTE.ink,      backgroundColor: 'transparent' },
        ],
      },
      options: {
        responsive: true,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          tooltip: { callbacks: { label: ctx => `${ctx.dataset.label}: ${moneyFmt(ctx.parsed.y, 'ILS')}` } },
        },
        scales: {
          x: axisStyle,
          y: { ...axisStyle, ticks: { ...axisStyle.ticks, callback: v => moneyFmt(v, '') } },
        },
      },
    });
  }

  function renderExpenses(s) {
    if (s.categories30) renderExpenseCategories(s.categories30);
    if (s.daily30) renderExpenseDaily(s.daily30);
    if (s.monthlyAlltime) renderExpenseMonthly(s.monthlyAlltime);
  }

  // Shared palette for category-keyed charts. Index = position in the
  // descending-by-total category list emitted by render_dashboard.py.
  const CATEGORY_COLORS = [
    '#8b1a1a', // ribbon
    '#a8924d', // faded gold
    '#3a5a40', // positive
    '#6b6357', // muted ink
    '#2a2520', // ink
    '#6b1212', // deeper burgundy
    '#c9b27a', // light gold
    '#5c6b50', // sage
    '#8a6f3a', // bronze
    '#4a3f37', // dark brown
    '#948977', // warm taupe
    '#b8483a', // terra cotta
  ];
  const categoryColor = (i) => CATEGORY_COLORS[i % CATEGORY_COLORS.length];

  function renderExpenseCategories(cats) {
    const el = document.getElementById('chart-expenses-categories');
    if (!el || !cats?.length) return;
    const total = cats.reduce((s, c) => s + c.total, 0);
    const fmtAmt = (v) => v.toLocaleString('en-IL', { maximumFractionDigits: 0 });

    // Size the canvas to give each row consistent vertical space, regardless
    // of how many categories there are.
    const ROW_HEIGHT = 30;
    el.parentNode.style.height = (cats.length * ROW_HEIGHT + 36) + 'px';

    new Chart(el, {
      type: 'bar',
      data: {
        labels: cats.map(c => c.category),
        datasets: [{
          data: cats.map(c => c.total),
          backgroundColor: cats.map((_, i) => categoryColor(i)),
          borderColor: PALETTE.parchment,
          borderWidth: 0.5,
          borderRadius: 2,
        }],
      },
      options: {
        indexAxis: 'y',
        responsive: true,
        maintainAspectRatio: false,
        layout: { padding: { top: 4, bottom: 4, left: 4, right: 12 } },
        plugins: {
          legend: { display: false },
          tooltip: {
            bodyFont: { family: "'EB Garamond', Georgia, serif", size: 12 },
            footerFont: { family: "'EB Garamond', Georgia, serif", size: 12, weight: '500' },
            footerColor: PALETTE.ink,
            footerMarginTop: 8,
            padding: 10,
            callbacks: {
              title: (ctxs) => {
                const row = cats[ctxs[0].dataIndex];
                const pct = total > 0 ? ((row.total / total) * 100) : 0;
                return `${row.category} · ${pct.toFixed(0)}%`;
              },
              label: () => '',
              afterBody: (ctxs) => {
                const row = cats[ctxs[0].dataIndex];
                const txns = row.txns || [];
                if (!txns.length) return ['(no expenses)'];
                const max = 10;
                const lines = txns.slice(0, max).map(([date, merchant, amt]) =>
                  `${date} · ${merchant} — ${moneyFmt(amt, 'ILS')}`);
                if (txns.length > max) lines.push(`…and ${txns.length - max} more`);
                return lines;
              },
              footer: (ctxs) => {
                const row = cats[ctxs[0].dataIndex];
                const n = row.count;
                return `Total: ${moneyFmt(row.total, 'ILS')} · ${n} txn${n === 1 ? '' : 's'}`;
              },
            },
          },
        },
        scales: {
          x: {
            ...axisStyle,
            beginAtZero: true,
            ticks: { ...axisStyle.ticks, callback: v => fmtAmt(v) },
          },
          y: {
            grid: { display: false, drawBorder: false },
            ticks: {
              color: PALETTE.ink,
              font: { family: "'Cormorant Garamond', Georgia, serif", size: 12, weight: '500' },
              padding: 6,
              autoSkip: false,
              callback: function(value, index) {
                const row = cats[index];
                if (!row) return '';
                return `${row.category} · ${fmtAmt(row.total)} ILS`;
              },
            },
          },
        },
      },
    });
  }

  function renderExpenseDaily(d) {
    const el = document.getElementById('chart-expenses-daily');
    if (!el || !d.dates?.length) return;

    const datasets = [{
      label: 'ILS',
      data: d.totals,
      backgroundColor: PALETTE.gold,
      borderColor: PALETTE.ink,
      borderWidth: 0.5,
    }];

    new Chart(el, {
      type: 'bar',
      data: { labels: d.dates, datasets },
      options: {
        responsive: true,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            bodyFont: { family: "'EB Garamond', Georgia, serif", size: 12 },
            footerFont: { family: "'EB Garamond', Georgia, serif", size: 12, weight: '500' },
            footerColor: PALETTE.ink,
            footerMarginTop: 8,
            padding: 10,
            filter: (item) => item.datasetIndex === 0,
            callbacks: {
              label: () => '',
              afterBody: (ctxs) => {
                const idx = ctxs[0].dataIndex;
                const txns = d.txns?.[idx] || [];
                if (!txns.length) return ['(no expenses)'];
                const max = 8;
                const lines = txns.slice(0, max).map(([merchant, cat, amt]) =>
                  `${merchant} · ${cat} — ${moneyFmt(amt, 'ILS')}`);
                if (txns.length > max) lines.push(`…and ${txns.length - max} more`);
                return lines;
              },
              footer: (ctxs) => `Total: ${moneyFmt(d.totals?.[ctxs[0].dataIndex] || 0, 'ILS')}`,
            },
          },
        },
        scales: {
          x: {
            ...axisStyle,
            type: 'time',
            time: { unit: 'day', tooltipFormat: 'MMM d, yyyy', displayFormats: { day: 'd MMM' } },
            ticks: { ...axisStyle.ticks, maxRotation: 0, autoSkipPadding: 18 },
          },
          y: {
            ...axisStyle,
            type: 'logarithmic',
            ticks: { ...axisStyle.ticks, callback: v => moneyFmt(v, '') },
          },
        },
      },
    });
  }

  function renderExpenseMonthly(monthly) {
    const el = document.getElementById('chart-expenses-monthly');
    if (!el || !monthly?.length) return;
    new Chart(el, {
      type: 'bar',
      data: {
        labels: monthly.map(m => m.month),
        datasets: [{
          label: 'ILS',
          data: monthly.map(m => m.total),
          backgroundColor: PALETTE.gold,
          borderColor: PALETTE.ink,
          borderWidth: 0.5,
        }],
      },
      options: {
        responsive: true,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            bodyFont: { family: "'EB Garamond', Georgia, serif", size: 12 },
            footerFont: { family: "'EB Garamond', Georgia, serif", size: 12, weight: '500' },
            footerColor: PALETTE.ink,
            footerMarginTop: 8,
            padding: 10,
            callbacks: {
              label: () => '',
              afterBody: (ctxs) => {
                const row = monthly[ctxs[0].dataIndex];
                if (!row?.top?.length) return [];
                return row.top.map(([cat, amt]) => `${cat}: ${moneyFmt(amt, 'ILS')}`);
              },
              footer: (ctxs) => `Total: ${moneyFmt(ctxs[0].parsed.y, 'ILS')}`,
            },
          },
        },
        scales: {
          x: {
            ...axisStyle,
            ticks: { ...axisStyle.ticks, maxRotation: 0, autoSkipPadding: 14 },
          },
          y: { ...axisStyle, ticks: { ...axisStyle.ticks, callback: v => moneyFmt(v, '') } },
        },
      },
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', render);
  } else {
    render();
  }
})();
