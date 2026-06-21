<!doctype html>
<html lang="en" dir="auto">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Finance — {{AS_OF_DATE}}</title>
  <style>{{FONTS_INLINE_CSS}}</style>
  <style>{{STYLES_CSS}}</style>
</head>
<body>
<div class="page">

  <header class="masthead">
    <div class="monogram">— Finance —</div>
    <div class="as-of-date">As of {{AS_OF_DATE}}</div>
    <div class="headline-amount">{{NET_WORTH_ILS}}</div>
  </header>

  <details class="section" open>
    <summary>Overview</summary>

    {{DRIFT_BANNER}}

    {{OVERVIEW_TABLE}}

    <details class="breakdown" open>
      <summary>Monthly cash flow</summary>
      {{FLOW_SUMMARY}}
    </details>

    <details class="breakdown" open>
      <summary>Net worth over time</summary>
      <div class="chart-block">
        <canvas id="chart-networth" height="120"></canvas>
      </div>
    </details>

    <p class="footnote">{{OVERVIEW_FOOTNOTE}}</p>
  </details>

  <details class="section" open>
    <summary>Expenses</summary>

    <div class="row-charts">
      <details class="breakdown row-half" open>
        <summary>Where the money went</summary>
        {{MTD_EXPENSE_NOTE}}
        <div class="chart-block chart-block-pie">
          <canvas id="chart-expenses-categories"></canvas>
        </div>
      </details>

      <details class="breakdown row-half" open>
        <summary>Daily spend</summary>
        <div class="chart-block">
          <canvas id="chart-expenses-daily" height="180"></canvas>
        </div>
      </details>
    </div>

    <details class="breakdown" open>
      <summary>Monthly totals — all time</summary>
      <div class="chart-block">
        <canvas id="chart-expenses-monthly" height="120"></canvas>
      </div>
    </details>

    <details class="breakdown" open>
      <summary>Recent expenses (last 30 days)</summary>
      <div class="expenses-scroll">
        {{EXPENSES_TABLE}}
      </div>
    </details>
  </details>

  <details class="section">
    <summary>Stocks</summary>

    <details class="breakdown" open>
      <summary>Portfolio vs. S&amp;P 500 vs. deposits</summary>
      <div class="chart-block">
        <div class="chart-toolbar">
          <button class="chart-range" data-range="3m">3M</button>
          <button class="chart-range" data-range="ytd">YTD</button>
          <button class="chart-range" data-range="1y">1Y</button>
          <button class="chart-range active" data-range="all">All</button>
        </div>
        <canvas id="chart-stocks-vs-spy" height="120"></canvas>
      </div>
    </details>

    <details class="breakdown" open>
      <summary>Current positions</summary>
      {{POSITIONS_TABLE}}
    </details>

    {{IBKR_POSITIONS_TABLE}}

    <details class="breakdown" open>
      <summary>Dividends</summary>
      {{DIVIDENDS_YEARLY_SUMMARY}}
      <div class="row-charts">
        <div class="row-half">
          <p class="chart-title">Last received</p>
          {{DIVIDENDS_RECENT_5_TABLE}}
          <details class="dividend-history">
            <summary>show 1 year back</summary>
            {{DIVIDENDS_RECENT_1Y_TABLE}}
          </details>
        </div>
        <div class="row-half">
          <p class="chart-title">Upcoming</p>
          {{DIVIDENDS_UPCOMING_TABLE}}
        </div>
      </div>
      <div class="chart-block">
        <div class="chart-toolbar">
          <button class="chart-range active" data-range="1y">1Y</button>
          <button class="chart-range" data-range="all">All</button>
        </div>
        <canvas id="chart-dividends" height="120"></canvas>
        <p class="footnote">Live Yahoo dividend events for held positions (shares held on each ex-date), net of 25% Israeli withholding. Bars show dividends received per month (per quarter in the All view) in USD, stacked by payer (top 5 + Other) — hover for the per-stock breakdown. <em>absorbed</em> = already folded into a Hafenix cash snapshot; <em>recorded</em> <strong>✓</strong> = itemized from a statement.</p>
      </div>
    </details>

    <details class="breakdown">
      <summary>Brokerage deposits</summary>
      <div class="chart-block">
        <div class="chart-toolbar">
          <button class="chart-range" data-range="3m">3M</button>
          <button class="chart-range" data-range="ytd">YTD</button>
          <button class="chart-range active" data-range="1y">1Y</button>
          <button class="chart-range" data-range="all">All</button>
        </div>
        <canvas id="chart-fx-deposits" height="140"></canvas>
        <p class="footnote">USD→ILS and GBP→ILS from Yahoo Finance. Your deposits (when you bought that currency) marked on the matching line.</p>
      </div>
      {{DEPOSITS_TABLE}}
    </details>

    <details class="breakdown">
      <summary>All trades</summary>
      {{TRADES_TABLE}}
    </details>
  </details>

  <details class="section">
    <summary>Payslips</summary>

    <details class="breakdown" open>
      <summary>Payslip gross / net</summary>
      <div class="chart-block">
        <canvas id="chart-payslips" height="120"></canvas>
      </div>
    </details>

    <details class="breakdown" open>
      <summary>Recent payslips</summary>
      {{PAYSLIPS_TABLE}}
    </details>
  </details>

  <details class="section">
    <summary>SQLite data</summary>
    {{SQLITE_TABS}}
  </details>

</div>

<script>
  window.FINANCE_DATA = {{CHART_DATA_JSON}};
</script>
<script>{{CHART_JS}}</script>
<script>{{CHART_ADAPTER_JS}}</script>
<script>{{CHARTS_JS_APP}}</script>
</body>
</html>
