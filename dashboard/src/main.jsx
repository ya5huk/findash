import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'
import {
  DividendMonthlyChart,
  ExpenseCategoryChart,
  ExpenseMonthlyChart,
  ForexActivityChart,
  InteractiveLineChart,
} from './charts'
import './styles.css'

const POLL_MS = 60_000
const TAB_IDS = ['overview', 'cash-flow', 'payslips', 'markets', 'expenses', 'coverage']
const MARKET_TREND_NEUTRAL_PCT = 0.2
const MARKET_TREND_MAX_PCT = 3
const MARKET_TREND_MIN_ALPHA = 0.025
const MARKET_TREND_MAX_ALPHA = 0.2

function tabFromHash() {
  const requested = window.location.hash.replace(/^#/, '')
  return TAB_IDS.includes(requested) ? requested : 'overview'
}

function money(value, currency = 'ILS', digits = 0) {
  if (value == null || Number.isNaN(Number(value))) return '—'
  return new Intl.NumberFormat('en-IL', {
    style: 'currency',
    currency,
    currencyDisplay: currency === 'USD' ? 'narrowSymbol' : 'symbol',
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  }).format(value)
}

function number(value, digits = 2) {
  if (value == null || Number.isNaN(Number(value))) return '—'
  return new Intl.NumberFormat('en-IL', {
    maximumFractionDigits: digits,
  }).format(value)
}

function signedMoney(value, currency = 'USD', digits = 2) {
  if (value == null || Number.isNaN(Number(value))) return '—'
  return `${Number(value) > 0 ? '+' : ''}${money(value, currency, digits)}`
}

function signedPercent(value, digits = 2) {
  if (value == null || Number.isNaN(Number(value))) return '—'
  return `${Number(value) > 0 ? '+' : ''}${number(value, digits)}%`
}

function marketTrend(value) {
  const percent = Number(value)
  if (!Number.isFinite(percent) || Math.abs(percent) < MARKET_TREND_NEUTRAL_PCT) {
    return { tone: 'neutral', alpha: 0 }
  }

  const progress = Math.min(
    (Math.abs(percent) - MARKET_TREND_NEUTRAL_PCT)
      / (MARKET_TREND_MAX_PCT - MARKET_TREND_NEUTRAL_PCT),
    1,
  )
  const alpha = MARKET_TREND_MIN_ALPHA
    + progress * (MARKET_TREND_MAX_ALPHA - MARKET_TREND_MIN_ALPHA)
  return { tone: percent > 0 ? 'positive' : 'negative', alpha }
}

function currencySymbol(currency) {
  try {
    return new Intl.NumberFormat('en', {
      style: 'currency',
      currency,
      currencyDisplay: 'narrowSymbol',
      maximumFractionDigits: 0,
    }).formatToParts(0).find((part) => part.type === 'currency')?.value || currency
  } catch {
    return currency
  }
}

function timeLabel(value) {
  if (!value) return 'Not yet'
  const normalized = value.includes('T') ? value : `${value.replace(' ', 'T')}Z`
  const parsed = new Date(normalized)
  return Number.isNaN(parsed.valueOf())
    ? value
    : new Intl.DateTimeFormat('en-GB', {
        day: '2-digit',
        month: 'short',
        hour: '2-digit',
        minute: '2-digit',
      }).format(parsed)
}

function monthLabel(asOf) {
  const parsed = new Date(`${asOf}T00:00:00Z`)
  return Number.isNaN(parsed.valueOf())
    ? 'Current month'
    : new Intl.DateTimeFormat('en', { month: 'short', timeZone: 'UTC' }).format(parsed)
}

function isDormantZeroAccount(account, asOf) {
  if (Number(account.value) !== 0 || !account.observed_through || !asOf) return false
  const observed = new Date(`${account.observed_through}T00:00:00Z`)
  const cutoff = new Date(`${asOf}T00:00:00Z`)
  if (Number.isNaN(observed.valueOf()) || Number.isNaN(cutoff.valueOf())) return false
  const targetDay = cutoff.getUTCDate()
  cutoff.setUTCDate(1)
  cutoff.setUTCMonth(cutoff.getUTCMonth() - 1)
  const lastDay = new Date(Date.UTC(
    cutoff.getUTCFullYear(),
    cutoff.getUTCMonth() + 1,
    0,
  )).getUTCDate()
  cutoff.setUTCDate(Math.min(targetDay, lastDay))
  return observed < cutoff
}

function visibleOverviewBuckets(data) {
  return data.overview.buckets
    .map((bucket) => ({
      ...bucket,
      accounts: bucket.accounts.filter(
        (account) => !isDormantZeroAccount(account, data.as_of),
      ),
    }))
    .filter((bucket) => bucket.accounts.length)
}

function MiniPriceChart({ values = [], tone = 'neutral', ticker, previousSession = false }) {
  const finite = values.map(Number).filter(Number.isFinite)
  if (!finite.length) return <span className="market-spark-empty">No session history</span>
  const width = 128
  const height = 42
  const min = Math.min(...finite)
  const max = Math.max(...finite)
  const spread = max - min || 1
  const denominator = Math.max(finite.length - 1, 1)
  const points = finite.map((value, index) => {
    const x = (index / denominator) * width
    const y = height - 4 - ((value - min) / spread) * (height - 8)
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')

  return (
    <svg
      className={`market-spark market-spark-${tone}`}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={`${ticker} ${previousSession ? 'previous' : 'current'} session price history`}
      preserveAspectRatio="none"
    >
      <line x1="0" x2={width} y1={height / 2} y2={height / 2} />
      <polyline points={points} />
    </svg>
  )
}

function useDashboard() {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  const load = useCallback(async ({ quiet = false } = {}) => {
    if (!quiet) setLoading(true)
    try {
      const response = await fetch('/api/dashboard', { cache: 'no-store' })
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.error || 'Dashboard unavailable')
      setData(payload)
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Dashboard unavailable')
    } finally {
      if (!quiet) setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
    const timer = window.setInterval(() => load({ quiet: true }), POLL_MS)
    return () => window.clearInterval(timer)
  }, [load])

  const refresh = useCallback(async () => {
    await fetch('/api/refresh', { method: 'POST' })
    await load({ quiet: true })
  }, [load])

  return { data, error, loading, load, refresh }
}

function DashboardTabs({ tabs, activeTab, onChange }) {
  const onKeyDown = (event, index) => {
    let next = null
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (index + 1) % tabs.length
    if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (index - 1 + tabs.length) % tabs.length
    if (event.key === 'Home') next = 0
    if (event.key === 'End') next = tabs.length - 1
    if (next == null) return
    event.preventDefault()
    onChange(tabs[next].id)
    window.requestAnimationFrame(() => document.getElementById(`tab-${tabs[next].id}`)?.focus())
  }

  const active = tabs.find((tab) => tab.id === activeTab) || tabs[0]
  return (
    <div className="dashboard-workspace">
      <div className="tab-rail-wrap">
        <div className="tab-rail" role="tablist" aria-label="Dashboard views">
          {tabs.map((tab, index) => (
            <button
              id={`tab-${tab.id}`}
              key={tab.id}
              type="button"
              role="tab"
              aria-controls={`panel-${tab.id}`}
              aria-selected={tab.id === active.id}
              tabIndex={tab.id === active.id ? 0 : -1}
              onClick={() => onChange(tab.id)}
              onKeyDown={(event) => onKeyDown(event, index)}
            >
              {tab.label}
            </button>
          ))}
        </div>
      </div>
      <section
        className="tab-panel"
        id={`panel-${active.id}`}
        role="tabpanel"
        aria-labelledby={`tab-${active.id}`}
        key={active.id}
      >
        <header className="tab-panel-heading">
          <h2>{active.label}</h2>
          <span>{active.kicker}</span>
        </header>
        {active.content}
      </section>
    </div>
  )
}

function Overview({ data }) {
  const buckets = visibleOverviewBuckets(data)

  return (
    <>
      <table className="ledger overview-table">
        <thead>
          <tr><th>Account</th><th>Source</th><th className="num">Observed</th><th className="num">Live value</th></tr>
        </thead>
        <tbody>
          {buckets.flatMap((bucket) => [
            <tr className="group-row" key={`${bucket.name}-group`}>
              <td>{bucket.name}</td><td /><td />
              <td className="num">{money(bucket.total, data.reporting_currency)}</td>
            </tr>,
            ...bucket.accounts.map((account) => (
              <tr key={`${bucket.name}-${account.account_id}`}>
                <td dir="auto">{account.name}</td>
                <td className="muted">{account.institution}</td>
                <td className="muted num">{account.observed_through || '—'}</td>
                <td className="num">{money(account.value, data.reporting_currency)}</td>
              </tr>
            )),
          ])}
        </tbody>
      </table>
      <InteractiveLineChart
        labels={data.charts.netWorth.dates}
        currency={data.reporting_currency}
        breakdowns={data.charts.netWorth.breakdowns || []}
        coverageBreaks={data.charts.netWorth.coverageBreaks || []}
        initialRange="all"
        ariaLabel="Net worth history"
        series={[{
          label: 'Net worth',
          values: data.charts.netWorth.values,
          tone: 'ink',
          fill: true,
        }]}
      />
    </>
  )
}

function CashFlow({ data }) {
  const windows = [
    [monthLabel(data.as_of), data.cashflow.one_month],
    ['3 months', data.cashflow.three_month],
    ['12 months', data.cashflow.twelve_month],
    ['All time', data.cashflow.all_time],
  ]
  const metrics = [
    ['Income', 'total_income', 1, 'flow-income'],
    ['Job', 'job_income', 1, 'flow-detail'],
    ['Dividends', 'dividends', 1, 'flow-detail'],
    ['Other', 'other_income', 1, 'flow-detail'],
    ['Spend', 'expenses', -1, 'flow-spend'],
    ['Net', 'net', 1, 'emphasis-row'],
  ]
  const dividendNote = data.cashflow.dividends_basis === 'modeled_entitlement'
    ? `Dividends are modeled gross entitlement from distribution history; recorded cash is retained separately for future confirmation.${data.cashflow.dividends_partial ? ' Modeled coverage is partial.' : ''}`
    : 'Modeled distribution coverage is unavailable; dividends currently use recorded cash.'
  return (
    <>
      <table className="ledger flow-table">
        <thead><tr><th>Monthly average</th>{windows.map(([label]) => <th className="num" key={label}>{label}</th>)}</tr></thead>
        <tbody>
          {metrics.map(([label, key, sign, className]) => (
            <tr key={key} className={className}>
              <td>{label}</td>
              {windows.map(([window, values]) => (
                <td className="num" key={window}>
                  {values[key] == null ? '—' : money(values[key] * sign, data.reporting_currency)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="flow-note">{dividendNote}</p>
    </>
  )
}

function Payslips({ data }) {
  const chart = data.charts.payslips || {}
  const currency = chart.currency || data.reporting_currency
  const recent = data.payslips.slice(0, 24)

  return (
    <div className="payslips-view">
      <section className="payslip-section" aria-labelledby="payslip-chart-title">
        <h3 id="payslip-chart-title">Payslip gross / net</h3>
        <InteractiveLineChart
          labels={chart.months || []}
          currency={currency}
          initialRange="all"
          ariaLabel="Payslip gross and net income history"
          emptyMessage="No payslips yet."
          series={[
            {
              label: 'Gross',
              values: chart.gross || [],
              tone: 'muted',
              dashed: true,
            },
            {
              label: 'Net',
              values: chart.net || [],
              tone: 'ink',
            },
          ]}
        />
      </section>

      <section className="payslip-section" aria-labelledby="recent-payslips-title">
        <div className="payslip-section-heading">
          <h3 id="recent-payslips-title">Recent payslips</h3>
          <span>Amounts in {currency}<em> · Swipe →</em></span>
        </div>
        {recent.length ? (
          <div
            className="table-scroll payslip-ledger-scroll"
            role="region"
            aria-label="Recent payslip details"
            tabIndex="0"
          >
            <table className="ledger payslip-ledger">
              <colgroup>
                <col className="payslip-col-period" />
                <col className="payslip-col-employer" />
                <col className="payslip-col-primary" span="2" />
                <col className="payslip-col-detail" span="3" />
                <col className="payslip-col-detail" span="4" />
                <col className="payslip-col-paid" />
              </colgroup>
              <thead>
                <tr>
                  <th rowSpan="2">Period</th>
                  <th rowSpan="2">Employer</th>
                  <th className="num" rowSpan="2">Gross</th>
                  <th className="num" rowSpan="2">Net</th>
                  <th className="payslip-group-heading payslip-group-start" colSpan="3">Deductions</th>
                  <th className="payslip-group-heading payslip-group-start" colSpan="2">Pension</th>
                  <th className="payslip-group-heading payslip-group-start" colSpan="2">Study fund</th>
                  <th className="num payslip-group-start" rowSpan="2">Paid</th>
                </tr>
                <tr className="payslip-subhead">
                  <th className="num payslip-group-start">Tax</th>
                  <th className="num">Bituah</th>
                  <th className="num">Health</th>
                  <th className="num payslip-group-start">Employee</th>
                  <th className="num">Employer</th>
                  <th className="num payslip-group-start">Employee</th>
                  <th className="num">Employer</th>
                </tr>
              </thead>
              <tbody>
                {recent.map((payslip, index) => (
                  <tr key={`${payslip.period}-${payslip.paid_on}-${index}`}>
                    <td>{payslip.period}</td>
                    <td className="payslip-employer" dir="auto" title={payslip.employer}>
                      {payslip.employer}
                    </td>
                    <td className="num positive" title={money(payslip.gross_reporting, currency, 2)}>{number(payslip.gross_reporting)}</td>
                    <td className="num positive" title={money(payslip.net_reporting, currency, 2)}>{number(payslip.net_reporting)}</td>
                    <td className="num payslip-group-start" title={money(payslip.income_tax_reporting, currency, 2)}>{number(payslip.income_tax_reporting)}</td>
                    <td className="num" title={money(payslip.social_security_reporting, currency, 2)}>{number(payslip.social_security_reporting)}</td>
                    <td className="num" title={money(payslip.health_insurance_reporting, currency, 2)}>{number(payslip.health_insurance_reporting)}</td>
                    <td className="num payslip-group-start" title={money(payslip.pension_employee_reporting, currency, 2)}>{number(payslip.pension_employee_reporting)}</td>
                    <td className="num" title={money(payslip.pension_employer_reporting, currency, 2)}>{number(payslip.pension_employer_reporting)}</td>
                    <td className="num payslip-group-start" title={money(payslip.study_fund_employee_reporting, currency, 2)}>{number(payslip.study_fund_employee_reporting)}</td>
                    <td className="num" title={money(payslip.study_fund_employer_reporting, currency, 2)}>{number(payslip.study_fund_employer_reporting)}</td>
                    <td className="num muted payslip-group-start">{payslip.paid_on}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="empty">No payslips yet.</p>
        )}
      </section>
    </div>
  )
}

function DividendView({ data }) {
  const dividend = data.charts.dividends || {}
  const views = dividend.yearViews || []
  const [selectedYear, setSelectedYear] = useState(
    dividend.currentYear || data.as_of.slice(0, 4),
  )
  const selected = views.find((view) => view.year === selectedYear)
    || views.find((view) => view.year === dividend.currentYear)
    || views[0]
    || {}

  useEffect(() => {
    if (views.length && !views.some((view) => view.year === selectedYear)) {
      setSelectedYear(dividend.currentYear || views[0].year)
    }
  }, [dividend.currentYear, selectedYear, views])

  const got = Number(selected.historical || 0)
  const remaining = Number(selected.forecast || 0)
  const progress = selected.total > 0
    ? Math.min(100, Math.max(0, (got / selected.total) * 100))
    : 0

  return (
    <section className="dividend-view" aria-labelledby="dividend-view-title">
      <div className="dividend-year-switcher" aria-label="Dividend forecast year">
        {views.map((view) => (
          <button
            key={view.year}
            type="button"
            aria-pressed={view.year === selected.year}
            onClick={() => setSelectedYear(view.year)}
          >
            <span>{view.year}</span>
            <small>
              {view.mode === 'current'
                ? 'Current'
                : view.mode === 'forecast'
                  ? 'Forecast'
                  : 'History'}
            </small>
          </button>
        ))}
      </div>

      <div className="dividend-intro">
        <div className="dividend-income">
          <strong>
            {selected.total == null
              ? '—'
              : money(selected.total, data.reporting_currency)}
          </strong>
        </div>
        <div className="dividend-progress" aria-label={`${money(got, data.reporting_currency)} got, ${money(remaining, data.reporting_currency)} remaining`}>
          <div className="dividend-progress-stat dividend-progress-got">
            <strong>{money(got, data.reporting_currency)}</strong>
            <span>got</span>
          </div>
          <div className="dividend-progress-stat dividend-progress-remaining">
            <strong>{money(remaining, data.reporting_currency)}</strong>
            <span>remaining</span>
          </div>
          <div className="dividend-progress-track" aria-hidden="true">
            <span style={{ width: `${progress}%` }} />
          </div>
          <p className="dividend-purchase-note" id="dividend-view-title">
            A stock purchase changes only later distributions
          </p>
        </div>
      </div>

      <DividendMonthlyChart
        months={selected.months || []}
        series={selected.series || []}
        currency={data.reporting_currency}
        year={selected.year}
      />
    </section>
  )
}

function ForexView({ data }) {
  const ledgerLimit = 20
  const forex = data.forex_activity || {
    rows: [],
    summary: [],
    total_reporting_amount: 0,
  }
  const funding = forex.rows || []
  const recentFunding = funding.slice(0, ledgerLimit)
  return (
    <section className="forex-view" aria-labelledby="forex-view-title">
      <header className="market-section-heading">
        <div>
          <h3 id="forex-view-title">Forex</h3>
          <p>Historical exchange rates with brokerage funding and documented currency purchases marked.</p>
        </div>
        <span>
          {forex.funding_count || 0} funding · {forex.purchase_count || 0} purchases
          {' · '}
          {money(
            forex.total_reporting_amount,
            data.reporting_currency,
            2,
          )} valued
        </span>
      </header>
      <ForexActivityChart
        chart={data.charts.fxDeposits || {}}
        activity={funding}
        reportingCurrency={data.reporting_currency}
      />
      {funding.length ? (
        <div
          className="table-scroll forex-ledger-scroll"
          role="region"
          aria-label={`Latest ${recentFunding.length} of ${funding.length} recorded forex activities`}
          tabIndex="0"
        >
          <table className="ledger forex-ledger">
            <caption>
              Latest {recentFunding.length} of {funding.length} recorded events · newest to oldest
            </caption>
            <thead>
              <tr>
                <th>Date</th>
                <th>Type</th>
                <th className="num">Foreign amount</th>
                <th className="num">Historical rate</th>
                <th className="num">{data.reporting_currency} equivalent</th>
                <th className="num">Cumulative USD</th>
                <th className="num">Cumulative {data.reporting_currency}</th>
              </tr>
            </thead>
            <tbody>
              {recentFunding.map((event, index) => (
                <tr key={`${event.date}-${event.currency}-${index}`}>
                  <td className="muted">{event.date}</td>
                  <td>{event.kind === 'currency_purchase' ? 'Purchase' : 'Funding'}</td>
                  <td className="num">
                    {event.amount == null || !event.currency
                      ? '—'
                      : money(event.amount, event.currency, 2)}
                  </td>
                  <td className="num forex-rate-cell">
                    {event.rate == null ? '—' : number(event.rate, 4)}
                    {event.rate != null && (
                      <small>{event.rate_is_actual ? 'actual' : 'reference'}</small>
                    )}
                  </td>
                  <td className="num">
                    {event.reporting_amount == null
                      ? '—'
                      : money(event.reporting_amount, data.reporting_currency, 2)}
                  </td>
                  <td className="num forex-cumulative-cell">
                    {money(event.cumulative_usd_amount, 'USD', 2)}
                  </td>
                  <td className="num forex-cumulative-cell">
                    {money(
                      event.cumulative_reporting_amount,
                      data.reporting_currency,
                      2,
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="empty forex-empty">No forex activity is recorded yet.</p>
      )}
    </section>
  )
}

function Markets({ data }) {
  const benchmark = data.charts.stocksVsSpy
  return (
    <>
      {data.positions.length ? (
        <div className="market-prices" aria-label="Current holdings and session prices">
          {data.positions.map((position) => {
          const change = position.day_change_usd
          const trend = marketTrend(position.day_change_pct)
          const tone = trend.tone
          const pnl = position.pnl_usd
          const pnlTone = pnl == null || pnl === 0 ? 'neutral' : pnl > 0 ? 'positive' : 'negative'
          return (
            <article
              className={`market-position market-position-${tone}`}
              key={`${position.account_id}-${position.security_id}`}
              style={{ '--market-trend-alpha': trend.alpha }}
              tabIndex={0}
            >
              <div className="market-position-main">
                <div className="market-identity">
                  <strong>{position.ticker}</strong>
                  <small>{number(position.quantity, 6)} × {money(position.last_price, position.currency, 2)}</small>
                </div>
                <MiniPriceChart
                  ticker={position.ticker}
                  values={position.session_prices || []}
                  tone={tone}
                  previousSession={position.session_is_previous}
                />
                <div className="market-value">
                  <strong>{money(position.market_usd, 'USD', 0)}</strong>
                  <small className={tone === 'neutral' ? 'muted' : tone}>
                    {change == null
                      ? 'Change unavailable'
                      : `${change > 0 ? '+' : ''}${money(change, 'USD', 2)} · ${position.day_change_pct == null ? '—' : `${change > 0 ? '+' : ''}${number(position.day_change_pct, 2)}%`}`}
                  </small>
                </div>
              </div>
              <div className="market-position-detail" aria-label="Position details">
                <span>Bought <strong>{money(position.average_cost, position.currency, 2)}</strong></span>
                <span aria-label="Total profit or loss">
                  Total{' '}
                  <strong className={pnlTone === 'neutral' ? 'muted' : pnlTone}>
                    {pnl == null
                      ? 'Unavailable'
                      : `${signedMoney(pnl)} (${signedPercent(position.pnl_usd_pct)})`}
                  </strong>
                </span>
              </div>
            </article>
          )
          })}
        </div>
      ) : <p className="empty">No open positions.</p>}
      <div className="quote-tape" aria-label="Latest exchange-rate observations">
        {data.market.fx.map((quote) => (
          <div key={`${quote.base}-${quote.quote}`}>
            <span className="quote-pair">
              {quote.base} <em>{currencySymbol(quote.base)}</em>
              {' / '}
              {quote.quote} <em>{currencySymbol(quote.quote)}</em>
            </span>
            <strong>{number(quote.rate, 4)}</strong>
            <small>{timeLabel(quote.observed_at)}</small>
          </div>
        ))}
      </div>
      <InteractiveLineChart
        labels={benchmark.dates}
        currency={data.reporting_currency}
        initialRange="1y"
        ariaLabel="Portfolio, benchmark, and net funding history"
        series={[
          { label: 'Portfolio', values: benchmark.stocks, tone: 'ink' },
          { label: 'S&P 500', values: benchmark.spy, tone: 'muted', dashed: true },
          { label: 'Net funding', values: benchmark.cumulativeDeposits, tone: 'gold' },
        ]}
      />
      <DividendView data={data} />
      <ForexView data={data} />
    </>
  )
}

function Expenses({ data }) {
  const categories = data.charts.expenses.categories30 || []
  const monthly = data.charts.expenses.monthlyAlltime || []
  return (
    <div className="expense-layout">
      <div>
        <p className="measure-label">Last 30 days · {money(data.recent_expenses_total, data.reporting_currency)}</p>
        <ExpenseCategoryChart
          categories={categories}
          currency={data.reporting_currency}
        />
      </div>
      <div className="expense-ledger-slot">
        <div
          className="table-scroll expense-ledger-scroll"
          role="region"
          aria-label="Expenses from the last 30 days"
          tabIndex="0"
        >
          <table className="ledger">
            <thead><tr><th>Date</th><th>Merchant</th><th>Category</th><th>Amount</th></tr></thead>
            <tbody>
              {data.recent_expenses.map((expense, index) => (
                <tr key={`${expense.date}-${index}`}>
                  <td className="muted">{expense.date}</td>
                  <td dir="auto">{expense.merchant}</td>
                  <td className="muted">{expense.category}</td>
                  <td className="num negative">{money(-expense.amount, data.reporting_currency)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      <section className="expense-monthly" aria-labelledby="expense-monthly-title">
        <h3 id="expense-monthly-title">Monthly totals — all time</h3>
        <ExpenseMonthlyChart months={monthly} currency={data.reporting_currency} />
      </section>
    </div>
  )
}

function App() {
  const { data, error, loading, load, refresh } = useDashboard()
  const [refreshing, setRefreshing] = useState(false)
  const [activeTab, setActiveTab] = useState(tabFromHash)
  const refreshed = useMemo(() => data?.refresh?.running || refreshing, [data, refreshing])

  useEffect(() => {
    const onHashChange = () => setActiveTab(tabFromHash())
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  const selectTab = (tab) => {
    setActiveTab(tab)
    window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}#${tab}`)
  }

  const requestRefresh = async () => {
    setRefreshing(true)
    try { await refresh() } finally { setRefreshing(false) }
  }

  if (loading && !data) return <main className="loading-page">Opening the ledger…</main>
  if (!data) {
    return (
      <main className="loading-page">
        <p>{error || 'Dashboard unavailable.'}</p>
        <button onClick={() => load()}>Try again</button>
      </main>
    )
  }

  const tabs = [
    {
      id: 'overview',
      label: 'Overview',
      kicker: `${visibleOverviewBuckets(data).length} account groups`,
      content: <Overview data={data} />,
    },
    {
      id: 'cash-flow',
      label: 'Cash flow',
      kicker: `Coverage from ${data.cashflow.coverage_start || '—'}`,
      content: <CashFlow data={data} />,
    },
    {
      id: 'payslips',
      label: 'Payslips',
      kicker: `${data.payslips.length} recorded`,
      content: <Payslips data={data} />,
    },
    {
      id: 'markets',
      label: 'Markets',
      kicker: `${data.positions.length} open positions`,
      content: <Markets data={data} />,
    },
    {
      id: 'expenses',
      label: 'Expenses',
      kicker: 'Last 30 days',
      content: <Expenses data={data} />,
    },
    {
      id: 'coverage',
      label: 'Data',
      kicker: `${data.diagnostics.length} SQLite tables`,
      content: (
        <table className="ledger compact-table">
          <thead><tr><th>Table</th><th>Rows</th></tr></thead>
          <tbody>{data.diagnostics.map((row) => <tr key={row.table}><td>{row.table}</td><td className="num">{number(row.rows, 0)}</td></tr>)}</tbody>
        </table>
      ),
    },
  ]

  return (
    <main>
      <header className="masthead">
        <div className="masthead-top">
          <span className="monogram">— FinDash —</span>
          <span className={`live-state ${refreshed ? 'refreshing' : ''}`}>
            {refreshed ? 'Updating market data' : 'Live ledger'}
          </span>
        </div>
        <p className="as-of">As of {data.as_of}</p>
        <h1>{money(data.overview.total, data.reporting_currency)}</h1>
      </header>

      <div className="freshness-strip">
        <div><span>Financial sync</span><strong>{timeLabel(data.freshness.last_sync_at)}</strong></div>
        <div><span>Market observed</span><strong>{timeLabel(data.freshness.market_observed_at)}</strong></div>
        <div><span>Refresh cadence</span><strong>Every 30 minutes</strong></div>
        <button onClick={requestRefresh} disabled={refreshed}>{refreshed ? 'Refreshing…' : 'Refresh now'}</button>
      </div>
      {error && <p className="notice">The last background read failed; showing the most recent view.</p>}

      <DashboardTabs tabs={tabs} activeTab={activeTab} onChange={selectTab} />
      <footer>Local only · SQLite-backed · Market quotes are reference data, not transaction evidence.</footer>
    </main>
  )
}

createRoot(document.getElementById('root')).render(
  <React.StrictMode><App /></React.StrictMode>,
)
