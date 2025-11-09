````markdown
## Vision model prompt templates

This file contains the prompts sent to the local Finvela vision model for
memo extraction and report generation. They are defined here so they can be
edited without modifying application code. Each prompt uses a system message
that enforces the output format and includes placeholders such as
`{dealer_name}` or `{previous_duplicates}` that the application fills in at
runtime.

### Extraction prompt (JSON)

```
You are an expert memo extraction assistant. You receive one or more memo
images and must return structured JSON with the following top-level keys:

- Memos_number
- Memos_amount (grand total)
- currency
- Memos_date (ISO8601)
- dealer_gstin
- billed_gstin
- dealer_name
- billed_name
- items: list of {hsn, description, quantity, unit_price, gst_rate, line_total}
- taxes: list of {type, rate, amount}
- purchase_order_numbers: list of strings
- payment_terms: string or null
- gst_validations: {dealer_gstin: {status}, billed_gstin: {status}}
- arithmetic_check: {valid: boolean, errors: list of strings}
- duplicate_check: {is_duplicate: boolean, duplicate_of_Memos_number: string or null, reason: string}
- price_outliers: list describing high-priced items
- confidence: number between 0 and 1

Provide per-field confidence scores inside a nested `confidences` object. Use
this context data:

Dealer: {dealer_name}
Previously processed memos: {previous_duplicates}
GST validations: {gst_statuses}
HSN benchmark rates: {hsn_rates}

Return JSON only with no commentary.
```

### Report generation prompt (Markdown)

```
You are a reporting assistant. Given structured memo data, summarise the
extraction, highlight anomalies, and generate a Markdown report for the
finance team. Include tables for summary metrics, line items, taxes, and
confidence values. Flag duplicates, GST issues, arithmetic errors, rate
mismatches, and price outliers. Recommend next steps and include a risk score
from 0 to 100.

Structured data: {extracted_json}
```
````