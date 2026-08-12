import {
  BarElement,
  BarController,
  CategoryScale,
  Chart,
  Filler,
  Legend,
  LinearScale,
  LineElement,
  LineController,
  PointElement,
  Tooltip,
} from 'chart.js'
import { useEffect, useMemo, useRef, useState } from 'react'

Chart.register(
  BarElement,
  BarController,
  CategoryScale,
  Filler,
  Legend,
  LinearScale,
  LineElement,
  LineController,
  PointElement,
  Tooltip,
)

const PALETTE = {
  parchment: '#f5efe3',
  ink: '#2a2520',
  mutedInk: '#6b6357',
  hairline: 'rgba(201, 191, 170, 0.55)',
  ribbon: '#8b1a1a',
  gold: '#a8924d',
  positive: '#3a5a40',
}

const SERIES_COLORS = {
  ink: PALETTE.ink,
  muted: PALETTE.mutedInk,
  gold: PALETTE.gold,
  ribbon: PALETTE.ribbon,
  positive: PALETTE.positive,
}

const CATEGORY_COLORS = [
  PALETTE.ribbon,
  PALETTE.gold,
  PALETTE.positive,
  PALETTE.mutedInk,
  PALETTE.ink,
  '#6b1212',
  '#c9b27a',
  '#5c6b50',
]

const FOREX_COLORS = [
  PALETTE.ink,
  PALETTE.ribbon,
  PALETTE.positive,
  PALETTE.gold,
  PALETTE.mutedInk,
]

const RANGE_OPTIONS = [
  ['3m', '3M'],
  ['ytd', 'YTD'],
  ['1y', '1Y'],
  ['all', 'All'],
]

function parsedDate(value) {
  if (!/^\d{4}-\d{2}(?:-\d{2})?$/.test(value || '')) return null
  const date = new Date(`${value.length === 7 ? `${value}-01` : value}T00:00:00Z`)
  return Number.isNaN(date.valueOf()) ? null : date
}

function shortDate(value) {
  const date = parsedDate(value)
  if (!date) return value
  return new Intl.DateTimeFormat('en-GB', {
    month: 'short',
    year: '2-digit',
    timeZone: 'UTC',
  }).format(date)
}

function fullDate(value) {
  const date = parsedDate(value)
  if (!date) return value
  return new Intl.DateTimeFormat('en-GB', {
    day: value.length === 10 ? 'numeric' : undefined,
    month: 'long',
    year: 'numeric',
    timeZone: 'UTC',
  }).format(date)
}

function money(value, currency, digits = 0) {
  if (!Number.isFinite(value)) return '—'
  return new Intl.NumberFormat('en-IL', {
    style: 'currency',
    currency,
    currencyDisplay: currency === 'USD' ? 'narrowSymbol' : 'symbol',
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  }).format(value)
}

function compactMoney(value, currency) {
  if (!Number.isFinite(Number(value))) return '—'
  return new Intl.NumberFormat('en-IL', {
    style: 'currency',
    currency,
    currencyDisplay: currency === 'USD' ? 'narrowSymbol' : 'symbol',
    notation: 'compact',
    maximumFractionDigits: 1,
  }).format(Number(value))
}

function rangeStart(labels, range) {
  if (range === 'all' || labels.length < 2) return 0
  const last = parsedDate(labels.at(-1))
  if (!last) return 0
  const cutoff = new Date(last)
  if (range === '3m') cutoff.setUTCMonth(cutoff.getUTCMonth() - 3)
  if (range === '1y') cutoff.setUTCFullYear(cutoff.getUTCFullYear() - 1)
  if (range === 'ytd') cutoff.setUTCMonth(0, 1)
  const index = labels.findIndex((label) => {
    const date = parsedDate(label)
    return date && date >= cutoff
  })
  return index < 0 ? 0 : index
}

function rangeCutoff(latest, range) {
  if (!latest || range === 'all') return Number.NEGATIVE_INFINITY
  const cutoff = new Date(latest)
  if (range === '3m') cutoff.setUTCMonth(cutoff.getUTCMonth() - 3)
  if (range === '1y') cutoff.setUTCFullYear(cutoff.getUTCFullYear() - 1)
  if (range === 'ytd') cutoff.setUTCMonth(0, 1)
  return cutoff.valueOf()
}

function timestampLabel(value, full = false) {
  const parsed = new Date(Number(value))
  if (Number.isNaN(parsed.valueOf())) return String(value)
  const iso = parsed.toISOString().slice(0, 10)
  return full ? fullDate(iso) : shortDate(iso)
}

function lineBackground(context) {
  const { chart } = context
  const area = chart.chartArea
  if (!area) return 'rgba(168, 146, 77, 0.08)'
  const gradient = chart.ctx.createLinearGradient(0, area.top, 0, area.bottom)
  gradient.addColorStop(0, 'rgba(168, 146, 77, 0.20)')
  gradient.addColorStop(1, 'rgba(168, 146, 77, 0)')
  return gradient
}

const hoverGuide = {
  id: 'findashHoverGuide',
  afterDraw(chart) {
    const active = chart.getActiveElements()
    if (!active.length) return
    const x = active[0].element.x
    const { top, bottom } = chart.chartArea
    chart.ctx.save()
    chart.ctx.strokeStyle = 'rgba(168, 146, 77, 0.55)'
    chart.ctx.lineWidth = 1
    chart.ctx.setLineDash([2, 4])
    chart.ctx.beginPath()
    chart.ctx.moveTo(x, top)
    chart.ctx.lineTo(x, bottom)
    chart.ctx.stroke()
    chart.ctx.restore()
  },
}

function sharedOptions(currency) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: 'index', intersect: false },
    animation: { duration: 520, easing: 'easeOutQuart' },
    layout: { padding: { top: 8, right: 8, bottom: 0, left: 0 } },
    onHover(event, elements, chart) {
      chart.canvas.style.cursor = elements.length ? 'crosshair' : 'default'
    },
    elements: {
      line: { tension: 0.28, borderWidth: 1.75 },
      point: { radius: 0, hoverRadius: 4, hitRadius: 16, borderWidth: 1 },
    },
    plugins: {
      legend: {
        display: true,
        position: 'bottom',
        align: 'center',
        labels: {
          color: PALETTE.mutedInk,
          boxWidth: 24,
          boxHeight: 2,
          padding: 20,
          font: { family: "'EB Garamond', Georgia, serif", size: 13 },
        },
      },
      tooltip: {
        backgroundColor: 'rgba(245, 239, 227, 0.98)',
        titleColor: PALETTE.ink,
        bodyColor: PALETTE.ink,
        footerColor: PALETTE.ink,
        borderColor: PALETTE.hairline,
        borderWidth: 1,
        cornerRadius: 0,
        displayColors: false,
        padding: 11,
        titleFont: {
          family: "'Cormorant Garamond', Georgia, serif",
          size: 14,
          weight: '600',
        },
        bodyFont: { family: "'EB Garamond', Georgia, serif", size: 13 },
      },
    },
    scales: {
      x: {
        grid: { display: false },
        border: { display: false },
        ticks: {
          autoSkip: true,
          autoSkipPadding: 20,
          color: PALETTE.mutedInk,
          maxRotation: 0,
          padding: 10,
          font: { family: "'EB Garamond', Georgia, serif", size: 12 },
          callback(value) {
            return shortDate(this.getLabelForValue(value))
          },
        },
      },
      y: {
        grid: { color: PALETTE.hairline, drawTicks: false },
        border: { display: false },
        ticks: {
          color: PALETTE.mutedInk,
          padding: 10,
          maxTicksLimit: 6,
          font: { family: "'EB Garamond', Georgia, serif", size: 12 },
          callback: (value) => compactMoney(value, currency),
        },
      },
    },
  }
}

export function InteractiveLineChart({
  labels = [],
  series = [],
  currency = 'ILS',
  breakdowns = [],
  coverageBreaks = [],
  initialRange = 'all',
  ariaLabel = 'Financial history chart',
  emptyMessage = 'Not enough history to chart yet.',
}) {
  const canvas = useRef(null)
  const [range, setRange] = useState(initialRange)
  const hasDates = labels.some((label) => parsedDate(label))
  const start = useMemo(() => rangeStart(labels, range), [labels, range])
  const visibleLabels = useMemo(() => labels.slice(start), [labels, start])
  const visibleBreakdowns = useMemo(
    () => breakdowns.slice(start),
    [breakdowns, start],
  )
  const visibleBreaks = useMemo(
    () => coverageBreaks.slice(start),
    [coverageBreaks, start],
  )
  const hasValues = series.some((item) => item.values?.some(Number.isFinite))

  useEffect(() => {
    if (!canvas.current || !visibleLabels.length || !hasValues) return undefined
    const options = sharedOptions(currency)
    options.plugins.legend.display = series.length > 1
    options.plugins.tooltip.callbacks = {
      title: (items) => fullDate(items[0]?.label || ''),
      label: (context) => (
        breakdowns.length
          ? ''
          : `${series.length > 1 ? `${context.dataset.label}: ` : ''}${money(context.parsed.y, currency)}`
      ),
      afterBody: (items) => {
        if (!breakdowns.length) return []
        return (visibleBreakdowns[items[0]?.dataIndex] || []).map(
          ([name, amount]) => `${name}: ${money(amount, currency)}`,
        )
      },
      footer: (items) => (
        breakdowns.length && items[0]
          ? `Total: ${money(items[0].parsed.y, currency)}`
          : ''
      ),
    }

    const chart = new Chart(canvas.current, {
      type: 'line',
      data: {
        labels: visibleLabels,
        datasets: series.map((item, index) => {
          const color = SERIES_COLORS[item.tone] || PALETTE.ink
          return {
            label: item.label,
            data: (item.values || []).slice(start),
            borderColor: color,
            backgroundColor: index === 0 && item.fill ? lineBackground : 'transparent',
            fill: Boolean(index === 0 && item.fill),
            borderDash: item.dashed ? [7, 6] : undefined,
            borderWidth: item.tone === 'gold' ? 1.5 : 1.8,
            pointBackgroundColor: PALETTE.parchment,
            pointBorderColor: color,
            spanGaps: false,
            pointRadius: (context) => (
              index === 0 && visibleBreaks[context.dataIndex] ? 3 : 0
            ),
            segment: index === 0 && visibleBreaks.length
              ? {
                  borderColor: (context) => (
                    visibleBreaks[context.p1DataIndex]
                      ? 'rgba(42, 37, 32, 0)'
                      : color
                  ),
                }
              : undefined,
          }
        }),
      },
      options,
      plugins: [hoverGuide],
    })
    return () => chart.destroy()
  }, [
    breakdowns.length,
    currency,
    hasValues,
    series,
    start,
    visibleBreakdowns,
    visibleBreaks,
    visibleLabels,
  ])

  if (!labels.length || !hasValues) return <p className="empty">{emptyMessage}</p>

  return (
    <div className="interactive-chart">
      {hasDates && (
        <div className="chart-ranges" aria-label="Chart date range">
          {RANGE_OPTIONS.map(([value, label]) => (
            <button
              key={value}
              type="button"
              aria-pressed={range === value}
              onClick={() => setRange(value)}
            >
              {label}
            </button>
          ))}
        </div>
      )}
      <div className="chart-stage">
        <canvas ref={canvas} role="img" aria-label={ariaLabel} />
      </div>
    </div>
  )
}

export function ForexActivityChart({
  chart = {},
  activity = [],
  reportingCurrency = 'ILS',
}) {
  const canvas = useRef(null)
  const [range, setRange] = useState('1y')
  const dates = chart.dates || []
  const rateSeries = useMemo(() => {
    const supplied = chart.series?.length
      ? chart.series
      : [
          { currency: 'USD', values: chart.usd || [] },
          { currency: 'GBP', values: chart.gbp || [] },
        ]
    return supplied.map((series) => ({
      currency: series.currency,
      points: dates.flatMap((date, index) => {
        const parsed = parsedDate(date)
        const rawRate = series.values?.[index]
        if (rawRate == null || rawRate === '') return []
        const rate = Number(rawRate)
        return parsed && Number.isFinite(rate)
          ? [{ x: parsed.valueOf(), y: rate, date }]
          : []
      }),
    })).filter((series) => series.points.length)
  }, [chart.gbp, chart.series, chart.usd, dates])
  const activityPoints = useMemo(() => activity.flatMap((event) => {
    const parsed = parsedDate(event.date)
    if (event.rate == null || event.rate === '') return []
    const rate = Number(event.rate)
    return parsed && Number.isFinite(rate)
      ? [{ ...event, timestamp: parsed.valueOf(), rate }]
      : []
  }), [activity])
  const latest = Math.max(
    ...rateSeries.flatMap((series) => series.points.map((point) => point.x)),
    ...activityPoints.map((event) => event.timestamp),
    Number.NEGATIVE_INFINITY,
  )
  const cutoff = rangeCutoff(
    Number.isFinite(latest) ? latest : null,
    range,
  )
  const visibleRates = useMemo(() => rateSeries.map((series) => ({
    ...series,
    points: series.points.filter((point) => point.x >= cutoff),
  })), [cutoff, rateSeries])
  const visibleFunding = useMemo(
    () => activityPoints.filter((event) => event.timestamp >= cutoff),
    [activityPoints, cutoff],
  )
  const hasValues = rateSeries.length > 0 || activityPoints.length > 0

  useEffect(() => {
    if (!canvas.current || !hasValues) return undefined
    const options = sharedOptions(reportingCurrency)
    options.interaction = { mode: 'nearest', intersect: false, axis: 'x' }
    options.plugins.legend.labels = {
      ...options.plugins.legend.labels,
      filter: (item, data) => !data.datasets[item.datasetIndex]?.isFunding,
    }
    options.plugins.tooltip.displayColors = false
    options.plugins.tooltip.callbacks = {
      title: (items) => timestampLabel(items[0]?.parsed.x, true),
      label: (context) => {
        const event = context.raw?.funding
        if (!event) {
          return `${context.dataset.label}: ${Number(context.parsed.y).toFixed(4)}`
        }
        const kind = event.kind === 'currency_purchase'
          ? 'Currency purchase'
          : 'Brokerage funding'
        return [
          event.amount == null || !event.currency
            ? `${kind} · amount unavailable`
            : `${kind} ${money(event.amount, event.currency, 2)}`,
          event.reporting_amount == null
            ? `${reportingCurrency} equivalent unavailable`
            : `${reportingCurrency} equivalent ${money(event.reporting_amount, reportingCurrency, 2)}`,
          `${event.rate_is_actual ? 'Actual' : 'Reference'} rate ${Number(event.rate).toFixed(4)}`,
        ]
      },
    }
    options.scales.x = {
      type: 'linear',
      grid: { display: false },
      border: { display: false },
      ticks: {
        autoSkip: true,
        autoSkipPadding: 24,
        color: PALETTE.mutedInk,
        maxRotation: 0,
        padding: 10,
        font: { family: "'EB Garamond', Georgia, serif", size: 12 },
        callback: (value) => timestampLabel(value),
      },
    }
    options.scales.y = {
      grid: { color: PALETTE.hairline, drawTicks: false },
      border: { display: false },
      ticks: {
        color: PALETTE.mutedInk,
        maxTicksLimit: 6,
        padding: 10,
        font: { family: "'EB Garamond', Georgia, serif", size: 12 },
        callback: (value) => Number(value).toFixed(2),
      },
    }

    const colorByCurrency = new Map(
      rateSeries.map((series, index) => [
        series.currency,
        FOREX_COLORS[index % FOREX_COLORS.length],
      ]),
    )
    const lineDatasets = visibleRates.map((series, index) => ({
      label: `${series.currency} → ${reportingCurrency}`,
      data: series.points,
      parsing: false,
      borderColor: colorByCurrency.get(series.currency),
      backgroundColor: 'transparent',
      borderDash: index ? [7, 6] : undefined,
      pointRadius: 0,
      pointHoverRadius: 4,
      spanGaps: true,
    }))
    const activityCurrencies = [...new Set(
      visibleFunding.map((event) => event.currency).filter(Boolean),
    )]
    const markerDatasets = activityCurrencies.map((currency, index) => ({
      label: `${currency} activity`,
      isFunding: true,
      data: visibleFunding
        .filter((event) => event.currency === currency)
        .map((event) => ({
          x: event.timestamp,
          y: event.rate,
          funding: event,
        })),
      parsing: false,
      showLine: false,
      pointRadius: 5,
      pointHoverRadius: 7,
      pointHitRadius: 14,
      pointBackgroundColor: colorByCurrency.get(currency)
        || FOREX_COLORS[(rateSeries.length + index) % FOREX_COLORS.length],
      pointBorderColor: PALETTE.parchment,
      pointBorderWidth: 2,
    }))

    const instance = new Chart(canvas.current, {
      type: 'line',
      data: { datasets: [...lineDatasets, ...markerDatasets] },
      options,
      plugins: [hoverGuide],
    })
    return () => instance.destroy()
  }, [
    hasValues,
    rateSeries,
    reportingCurrency,
    visibleFunding,
    visibleRates,
  ])

  if (!hasValues) {
    return <p className="empty">No FX rate or activity history is available yet.</p>
  }
  return (
    <div className="interactive-chart forex-chart">
      <div className="chart-ranges" aria-label="Forex chart date range">
        {RANGE_OPTIONS.map(([value, label]) => (
          <button
            key={value}
            type="button"
            aria-pressed={range === value}
            onClick={() => setRange(value)}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="chart-stage">
        <canvas
          ref={canvas}
          role="img"
          aria-label="Foreign exchange rates with funding and currency-purchase points"
        />
      </div>
    </div>
  )
}

export function ExpenseCategoryChart({ categories = [], currency = 'ILS' }) {
  const canvas = useRef(null)
  const total = categories.reduce((sum, row) => sum + row.total, 0)

  useEffect(() => {
    if (!canvas.current || !categories.length) return undefined
    const options = sharedOptions(currency)
    options.indexAxis = 'y'
    options.interaction = { mode: 'index', axis: 'y', intersect: false }
    options.layout.padding = { top: 2, right: 12, bottom: 2, left: 0 }
    options.plugins.legend.display = false
    options.plugins.tooltip.callbacks = {
      title: (items) => {
        const row = categories[items[0]?.dataIndex]
        const share = total > 0 ? (row.total / total) * 100 : 0
        return `${row.category} · ${share.toFixed(0)}%`
      },
      label: () => '',
      afterBody: (items) => {
        const row = categories[items[0]?.dataIndex]
        const transactions = row?.txns || []
        if (!transactions.length) return []
        const lines = transactions.slice(0, 7).map(
          ([date, merchant, amount]) => `${date} · ${merchant} — ${money(amount, currency)}`,
        )
        if (transactions.length > 7) lines.push(`…and ${transactions.length - 7} more`)
        return lines
      },
      footer: (items) => {
        const row = categories[items[0]?.dataIndex]
        if (!row) return ''
        return `${money(row.total, currency)}${row.count ? ` · ${row.count} transactions` : ''}`
      },
    }
    options.scales.x = {
      beginAtZero: true,
      grid: { color: PALETTE.hairline, drawTicks: false },
      border: { display: false },
      ticks: {
        color: PALETTE.mutedInk,
        padding: 10,
        maxTicksLimit: 5,
        font: { family: "'EB Garamond', Georgia, serif", size: 12 },
        callback: (value) => compactMoney(value, currency),
      },
    }
    options.scales.y = {
      grid: { display: false },
      border: { display: false },
      ticks: {
        color: PALETTE.ink,
        padding: 8,
        autoSkip: false,
        font: {
          family: "'Cormorant Garamond', Georgia, serif",
          size: 13,
          weight: '600',
        },
      },
    }

    const chart = new Chart(canvas.current, {
      type: 'bar',
      data: {
        labels: categories.map((row) => row.category),
        datasets: [{
          data: categories.map((row) => row.total),
          backgroundColor: categories.map(
            (_, index) => CATEGORY_COLORS[index % CATEGORY_COLORS.length],
          ),
          borderWidth: 0,
          borderRadius: 2,
          barPercentage: 0.68,
          categoryPercentage: 0.72,
        }],
      },
      options,
    })
    return () => chart.destroy()
  }, [categories, currency, total])

  if (!categories.length) return <p className="empty">No consumer expenses in this period.</p>
  const height = Math.max(260, categories.length * 38 + 48)
  return (
    <div className="interactive-chart category-chart">
      <div className="chart-stage" style={{ height }}>
        <canvas ref={canvas} role="img" aria-label="Spending by category chart" />
      </div>
    </div>
  )
}

export function ExpenseMonthlyChart({ months = [], currency = 'ILS' }) {
  const canvas = useRef(null)

  useEffect(() => {
    if (!canvas.current || !months.length) return undefined
    const options = sharedOptions(currency)
    options.plugins.legend.display = false
    options.plugins.tooltip.callbacks = {
      title: (items) => fullDate(items[0]?.label || ''),
      label: () => '',
      afterBody: (items) => {
        const row = months[items[0]?.dataIndex]
        return (row?.top || []).map(
          ([category, amount]) => `${category}: ${money(amount, currency)}`,
        )
      },
      footer: (items) => `Total: ${money(items[0]?.parsed.y, currency)}`,
    }
    options.scales.y.beginAtZero = true

    const chart = new Chart(canvas.current, {
      type: 'bar',
      data: {
        labels: months.map((row) => row.month),
        datasets: [{
          data: months.map((row) => row.total),
          backgroundColor: PALETTE.gold,
          borderColor: PALETTE.ink,
          borderWidth: 0.5,
          borderRadius: 1,
          barPercentage: 0.74,
          categoryPercentage: 0.82,
        }],
      },
      options,
    })
    return () => chart.destroy()
  }, [currency, months])

  if (!months.length) {
    return <p className="empty">No monthly consumer-expense history yet.</p>
  }

  return (
    <div className="interactive-chart expense-monthly-chart">
      <div className="chart-stage">
        <canvas ref={canvas} role="img" aria-label="Monthly expense totals for all time" />
      </div>
    </div>
  )
}

export function DividendMonthlyChart({
  months = [],
  series = [],
  currency = 'ILS',
  year,
}) {
  const canvas = useRef(null)
  const hasValues = series.some((row) => row.data?.some((value) => Number(value) > 0))

  useEffect(() => {
    if (!canvas.current || !months.length || !hasValues) return undefined
    const options = sharedOptions(currency)
    options.interaction = { mode: 'index', intersect: false }
    options.plugins.legend.labels = {
      ...options.plugins.legend.labels,
      boxWidth: 10,
      boxHeight: 10,
      padding: 16,
    }
    options.plugins.tooltip.displayColors = true
    options.plugins.tooltip.callbacks = {
      title: (items) => fullDate(items[0]?.label || ''),
      label: (context) => `${context.dataset.label}: ${money(context.parsed.y, currency)}`,
      footer: (items) => `Total: ${money(
        items.reduce((sum, item) => sum + (item.parsed.y || 0), 0),
        currency,
      )}`,
    }
    options.scales.x = {
      stacked: true,
      grid: { display: false },
      border: { display: false },
      ticks: {
        color: PALETTE.ink,
        padding: 10,
        font: {
          family: "'Cormorant Garamond', Georgia, serif",
          size: 14,
          weight: '600',
        },
        callback(value) {
          const label = this.getLabelForValue(value)
          const parsed = parsedDate(label)
          return parsed
            ? new Intl.DateTimeFormat('en', {
                month: 'short',
                timeZone: 'UTC',
              }).format(parsed)
            : label
        },
      },
    }
    options.scales.y = {
      stacked: true,
      beginAtZero: true,
      grid: { color: PALETTE.hairline, drawTicks: false },
      border: { display: false },
      ticks: {
        color: PALETTE.mutedInk,
        padding: 10,
        maxTicksLimit: 5,
        font: { family: "'EB Garamond', Georgia, serif", size: 12 },
        callback: (value) => compactMoney(value, currency),
      },
    }

    const chart = new Chart(canvas.current, {
      type: 'bar',
      data: {
        labels: months,
        datasets: series.map((row, index) => ({
          label: row.ticker,
          data: row.data,
          backgroundColor: row.ticker === 'Other'
            ? '#948977'
            : CATEGORY_COLORS[index % 5],
          borderColor: PALETTE.parchment,
          borderWidth: 0.75,
          borderRadius: 1,
          barPercentage: 0.74,
          categoryPercentage: 0.82,
          stack: 'dividends',
        })),
      },
      options,
    })
    return () => chart.destroy()
  }, [currency, hasValues, months, series])

  if (!months.length || !hasValues) {
    return <p className="empty">No modeled dividends are available for {year}.</p>
  }
  return (
    <div className="interactive-chart dividend-chart">
      <div className="chart-stage">
        <canvas
          ref={canvas}
          role="img"
          aria-label={`${year} monthly dividend entitlement by holding`}
        />
      </div>
    </div>
  )
}
