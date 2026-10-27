"""Shared validity rules for ledger relationship tables.

The same predicate guards writes (as ``NEW``) and audits stored rows (as a
table alias), so a trigger and an integrity check can never disagree.
"""

TRANSFER_LINK_VALID = """EXISTS (
  SELECT 1
    FROM canonical_events AS outflow
    JOIN canonical_events AS inflow ON inflow.id={row}.inflow_event_id
   WHERE outflow.id={row}.outflow_event_id
     AND outflow.event_type='cash'
     AND inflow.event_type='cash'
     AND outflow.flow_type='transfer'
     AND inflow.flow_type='transfer'
     AND outflow.amount_minor<0
     AND inflow.amount_minor=-outflow.amount_minor
     AND inflow.currency=outflow.currency
     AND inflow.account_id<>outflow.account_id
     AND outflow.status<>'excluded'
     AND inflow.status<>'excluded'
     AND julianday(inflow.occurred_on)-julianday(outflow.occurred_on)
         BETWEEN -3 AND 31
)"""

FX_CONVERSION_VALID = """EXISTS (
  SELECT 1
    FROM canonical_events AS sell
    JOIN canonical_events AS buy ON buy.id={row}.buy_event_id
   WHERE sell.id={row}.sell_event_id
     AND sell.event_type='cash'
     AND buy.event_type='cash'
     AND sell.account_id={row}.account_id
     AND buy.account_id={row}.account_id
     AND sell.flow_type='investment'
     AND buy.flow_type='investment'
     AND lower(COALESCE(sell.category,''))='fx'
     AND lower(COALESCE(buy.category,''))='fx'
     AND sell.amount_minor<0
     AND buy.amount_minor>0
     AND sell.currency<>buy.currency
     AND (
       ({row}.base_currency=buy.currency AND {row}.quote_currency=sell.currency)
       OR ({row}.base_currency=sell.currency AND {row}.quote_currency=buy.currency)
     )
     AND sell.occurred_on={row}.converted_on
     AND buy.occurred_on>=sell.occurred_on
     AND sell.status<>'excluded'
     AND buy.status<>'excluded'
)"""

DIVIDEND_EVENT_VALID = """EXISTS (
  SELECT 1
    FROM canonical_events AS cash
   WHERE cash.id={row}.cash_event_id
     AND cash.event_type='cash'
     AND cash.account_id={row}.account_id
     AND cash.flow_type='income'
     AND lower(COALESCE(cash.category,''))='dividend'
)"""
