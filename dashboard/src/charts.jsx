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

// Earlier months hold too little data to be meaningful.
const MONTHLY_FLOW_START = '2024-06'

function niceStep(raw) {
  const magnitude = 10 ** Math.floor(Math.log10(raw))
  const fraction = raw / magnitude
  const nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 2.5 ? 2.5 : fraction <= 5 ? 5 : 10
  return nice * magnitude
}

const INCOME_PARTS = [
  ['salary', 'Salary'],
  ['dividends', 'Dividends'],
  ['refunds', 'Refunds'],
  ['other', 'Other income'],
]

const NET_ROW_PILL_PADDING = 4
// y-axis gutter plus right padding; a slight overestimate only adds a spare line.
const NET_ROW_GUTTER = 70
const NET_ROW_MAX_LINES = 6

function netRowFont(size) {
  return `600 ${size}px 'EB Garamond', Georgia, serif`
}

function signedMoney(value, currency) {
  return new Intl.NumberFormat('en-IL', {
    style: 'currency',
    currency,
    currencyDisplay: currency === 'USD' ? 'narrowSymbol' : 'symbol',
    maximumFractionDigits: 0,
    signDisplay: 'exceptZero',
  }).format(value)
}

function compactSignedMoney(value, currency) {
  return new Intl.NumberFormat('en-IL', {
    style: 'currency',
    currency,
    currencyDisplay: currency === 'USD' ? 'narrowSymbol' : 'symbol',
    notation: 'compact',
    // Below a thousand, compact notation would otherwise print agorot.
    maximumFractionDigits: Math.abs(value) < 1000 ? 0 : 1,
    signDisplay: 'exceptZero',
  }).format(value)
}

// Tried in order: the first that fits within three staggered lines wins.
const NET_ROW_STYLES = [
  { format: signedMoney, fontSize: 12 },
  { format: signedMoney, fontSize: 11 },
  { format: compactSignedMoney, fontSize: 12 },
  { format: compactSignedMoney, fontSize: 11 },
  { format: compactSignedMoney, fontSize: 10 },
  { format: compactSignedMoney, fontSize: 9 },
]

export function MonthlyFlowChart({ months = [], currency = 'ILS' }) {
  const canvas = useRef(null)
  const [showNetRows, setShowNetRows] = useState(false)
  const visible = useMemo(
    () => months.filter((row) => row.month >= MONTHLY_FLOW_START),
    [months],
  )

  useEffect(() => {
    if (!canvas.current || !visible.length) return undefined
    const incomes = visible.map((row) => row.income?.total || 0)
    const expenses = visible.map((row) => row.total || 0)
    // One symmetric scale keeps the zero line centred and bar heights comparable.
    const peak = Math.max(...incomes, ...expenses, 1)
    const step = niceStep(peak / 3)
    const extent = Math.ceil((peak * 1.04) / step) * step

    const options = sharedOptions(currency)
    options.interaction = { mode: 'nearest', intersect: true }
    // The net rows occupy the space under the month labels, so the legend sits on top.
    options.plugins.legend.position = 'top'
    options.plugins.legend.labels = {
      ...options.plugins.legend.labels,
      boxWidth: 10,
      boxHeight: 10,
    }
    options.plugins.tooltip.footerFont = {
      family: "'EB Garamond', Georgia, serif",
      size: 13,
      weight: '600',
    }
    options.plugins.tooltip.callbacks = {
      title: (items) => fullDate(items[0]?.label || ''),
      label: () => '',
      afterBody: (items) => {
        const item = items[0]
        const row = visible[item?.dataIndex]
        if (!row) return []
        if (item.datasetIndex === 0) {
          return INCOME_PARTS
            .filter(([key]) => (row.income?.[key] || 0) !== 0)
            .map(([key, label]) => `${label}: ${money(row.income[key], currency)}`)
        }
        return (row.top || []).map(
          ([category, amount]) => `${category}: ${money(amount, currency)}`,
        )
      },
      footer: (items) => {
        const item = items[0]
        const row = visible[item?.dataIndex]
        if (!row) return ''
        return item.datasetIndex === 0
          ? `Money in: ${money(row.income?.total || 0, currency)}`
          : `Money out: ${money(row.total || 0, currency)}`
      },
    }
    options.scales.x.stacked = true
    options.scales.y = {
      ...options.scales.y,
      stacked: true,
      min: -extent,
      max: extent,
      grid: {
        drawTicks: false,
        color: (context) => (context.tick.value === 0 ? PALETTE.ink : PALETTE.hairline),
        lineWidth: (context) => (context.tick.value === 0 ? 1.25 : 1),
      },
      ticks: {
        ...options.scales.y.ticks,
        stepSize: step,
        maxTicksLimit: 9,
        callback: (value) => compactMoney(Math.abs(value), currency),
      },
    }

    const bar = {
      borderWidth: 0,
      borderRadius: 1,
      barPercentage: 0.74,
      categoryPercentage: 0.82,
    }
    // Hovering anywhere in a month's column writes both totals beside their bars,
    // rotated so the neighbouring columns never cover them.
    const columnTotals = {
      id: 'monthlyFlowColumnTotals',
      afterEvent(chart, args) {
        const { event } = args
        const { chartArea } = chart
        const inside = event.type !== 'mouseout'
          && event.x >= chartArea.left && event.x <= chartArea.right
          && event.y >= chartArea.top
          && event.y <= (showNetRows ? chart.height : chartArea.bottom)
        const index = inside
          ? Math.min(Math.max(chart.scales.x.getValueForPixel(event.x), 0), visible.length - 1)
          : null
        if (index !== chart.$hoveredMonth) {
          chart.$hoveredMonth = index
          args.changed = true
        }
      },
      afterDatasetsDraw(chart) {
        const index = chart.$hoveredMonth
        if (index == null) return
        const { ctx, chartArea } = chart
        const gap = 6
        ctx.save()
        ctx.font = "600 13px 'EB Garamond', Georgia, serif"
        ctx.textBaseline = 'middle'
        const label = (element, value, outward, color) => {
          const text = money(value, currency)
          const room = outward < 0 ? element.y - chartArea.top : chartArea.bottom - element.y
          // A bar reaching the plot edge leaves no room outside, so write inside it.
          const fits = room >= ctx.measureText(text).width + gap * 2
          const direction = fits ? outward : -outward
          ctx.save()
          ctx.fillStyle = fits ? color : PALETTE.parchment
          ctx.translate(element.x, element.y + direction * gap)
          ctx.rotate(-Math.PI / 2)
          ctx.textAlign = direction < 0 ? 'left' : 'right'
          ctx.fillText(text, 0, 0)
          ctx.restore()
        }
        label(chart.getDatasetMeta(0).data[index], incomes[index], -1, PALETTE.positive)
        label(chart.getDatasetMeta(1).data[index], expenses[index], 1, PALETTE.ribbon)
        ctx.restore()
      },
    }

    // Two ledger rows under the month labels: each month's net, then the running
    // total of those nets since the first visible month.
    const nets = incomes.map((income, index) => income - expenses[index])
    const runningTotals = nets.map(
      (_, index) => nets.slice(0, index + 1).reduce((sum, value) => sum + value, 0),
    )
    const netRowSpecs = [
      { label: 'Net', values: nets, tinted: true },
      { label: 'Total', values: runningTotals, tinted: false },
    ]
    // Neighbouring months alternate across several lines, so each number may span
    // that many columns. Wider charts use fewer lines and fuller amounts.
    const allRowValues = [...nets, ...runningTotals]
    const measure = canvas.current.getContext('2d')
    measure.save()
    const rowStyles = NET_ROW_STYLES.map((style) => {
      measure.font = netRowFont(style.fontSize)
      const widest = Math.max(
        ...allRowValues.map((value) => measure.measureText(style.format(value, currency)).width),
      )
      return { ...style, span: widest + NET_ROW_PILL_PADDING * 2 + 6 }
    })
    measure.restore()
    const rowLayout = (chartWidth) => {
      const columnWidth = Math.max(1, (chartWidth - NET_ROW_GUTTER) / visible.length)
      const linesFor = (style) => Math.ceil(style.span / columnWidth)
      const style = rowStyles.find((candidate) => linesFor(candidate) <= 3)
        || rowStyles.reduce((best, candidate) => (linesFor(candidate) < linesFor(best) ? candidate : best))
      const lines = Math.min(Math.max(linesFor(style), 1), NET_ROW_MAX_LINES)
      const lineHeight = style.fontSize + 8
      return { ...style, lines, lineHeight, rowHeight: lines * lineHeight + 8 }
    }
    const rowsTopGap = 14
    if (showNetRows) {
      const basePadding = options.layout.padding
      options.layout.padding = ({ chart }) => ({
        ...basePadding,
        bottom: rowsTopGap + rowLayout(chart.width).rowHeight * netRowSpecs.length,
      })
    }
    const netRows = {
      id: 'monthlyFlowNetRows',
      afterDatasetsDraw(chart) {
        const { ctx, chartArea, scales } = chart
        const { format, fontSize, lines, lineHeight, rowHeight } = rowLayout(chart.width)
        const rowsTop = chart.height - rowHeight * netRowSpecs.length
        const pillHeight = fontSize + 6
        ctx.save()
        ctx.font = netRowFont(fontSize)
        ctx.textBaseline = 'middle'
        netRowSpecs.forEach(({ label, values, tinted }, rowIndex) => {
          const top = rowsTop + rowIndex * rowHeight
          ctx.strokeStyle = PALETTE.hairline
          ctx.lineWidth = 1
          ctx.beginPath()
          ctx.moveTo(chartArea.left, top + 0.5)
          ctx.lineTo(chartArea.right, top + 0.5)
          ctx.stroke()
          ctx.save()
          ctx.font = "500 10px 'EB Garamond', Georgia, serif"
          ctx.fillStyle = PALETTE.mutedInk
          ctx.textAlign = 'right'
          ctx.fillText(label.toUpperCase(), chartArea.left - 6, top + rowHeight / 2)
          ctx.restore()
          values.forEach((value, index) => {
            const x = scales.x.getPixelForValue(index)
            const line = index % lines
            const y = top + 4 + lineHeight * line + lineHeight / 2
            const text = format(value, currency)
            const pillWidth = ctx.measureText(text).width + NET_ROW_PILL_PADDING * 2
            if (line > 0) {
              ctx.strokeStyle = PALETTE.hairline
              ctx.beginPath()
              ctx.moveTo(x + 0.5, top + 2)
              ctx.lineTo(x + 0.5, y - pillHeight / 2)
              ctx.stroke()
            }
            ctx.beginPath()
            ctx.roundRect(x - pillWidth / 2, y - pillHeight / 2, pillWidth, pillHeight, 2)
            if (tinted && value !== 0) {
              ctx.fillStyle = value > 0 ? 'rgba(58, 90, 64, 0.16)' : 'rgba(139, 26, 26, 0.13)'
              ctx.fill()
            }
            if (chart.$hoveredMonth === index) {
              ctx.strokeStyle = PALETTE.ink
              ctx.stroke()
            }
            ctx.fillStyle = PALETTE.ink
            ctx.textAlign = 'center'
            ctx.fillText(text, x, y)
          })
        })
        ctx.restore()
      },
    }

    const chart = new Chart(canvas.current, {
      type: 'bar',
      data: {
        labels: visible.map((row) => row.month),
        datasets: [
          { ...bar, label: 'Money in', data: incomes, backgroundColor: PALETTE.positive },
          {
            ...bar,
            label: 'Money out',
            data: expenses.map((value) => -value),
            backgroundColor: PALETTE.ribbon,
          },
        ],
      },
      options,
      plugins: showNetRows ? [columnTotals, netRows] : [columnTotals],
    })
    return () => chart.destroy()
  }, [currency, visible, showNetRows])

  if (!visible.length) {
    return <p className="empty">No monthly cash-flow history yet.</p>
  }

  return (
    <div className={`interactive-chart expense-monthly-chart${showNetRows ? ' with-net-rows' : ''}`}>
      <label className="flow-net-toggle">
        <input
          type="checkbox"
          role="switch"
          checked={showNetRows}
          onChange={(event) => setShowNetRows(event.target.checked)}
        />
        <span>Monthly net &amp; running total</span>
      </label>
      <div className="chart-stage">
        <canvas
          ref={canvas}
          role="img"
          aria-label="Monthly money in above and money out below the timeline"
        />
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
