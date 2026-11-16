-- Eén rij per gecontroleerde waarde; k is de order waar de rij bij hoort.
{{ config(materialized='view') }}
with m as (select window_start::timestamp as ws, window_end::timestamp as we from {{ source('landing', 'manifest') }})

  select 'order_items' as table_name, 'price' as column_name, 'price_out_of_range' as check_name, order_id as k,
         try_cast(price as double) <= 0 or try_cast(price as double) > 10000 as bad,
         'prijs moet groter dan 0 en hoogstens 10000 zijn' as details
  from {{ source('landing', 'order_items') }}
  union all
  select 'order_items', 'freight_value', 'freight_out_of_range', order_id,
         try_cast(freight_value as double) < 0 or try_cast(freight_value as double) > 2000,
         'vrachtkosten moeten tussen 0 en 2000 liggen'
  from {{ source('landing', 'order_items') }}
  union all
  select 'order_payments', 'payment_value', 'payment_out_of_range', order_id,
         try_cast(payment_value as double) < 0 or try_cast(payment_value as double) > 20000,
         'betaling moet tussen 0 en 20000 liggen'
  from {{ source('landing', 'order_payments') }}
  union all
  select 'order_payments', 'payment_installments', 'installments_out_of_range', order_id,
         try_cast(payment_installments as integer) < 0 or try_cast(payment_installments as integer) > 24,
         'aantal termijnen moet tussen 0 en 24 liggen'
  from {{ source('landing', 'order_payments') }}
  union all
  select 'orders', 'order_purchase_timestamp', 'purchase_outside_batch_window', order_id,
         try_cast(order_purchase_timestamp as timestamp) < m.ws or try_cast(order_purchase_timestamp as timestamp) >= m.we,
         'aankoopmoment valt buiten de week van deze batch'
  from {{ source('landing', 'orders') }}, m
  union all
  select 'orders', 'order_status', 'unknown_order_status', order_id,
         order_status not in ('delivered','shipped','canceled','unavailable','invoiced','processing','created','approved'),
         'onbekende orderstatus'
  from {{ source('landing', 'orders') }}
