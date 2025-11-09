# Finance Report: Memo CWP-69/25-26

**Date:** September 6, 2025

**Memo Number:** CWP-69/25-26

## Summary

This report details the financial information extracted from Memo CWP-69/25-26. The memo has a total amount of Rs. 269,429.00 and is from CHIRAG WOOD PACKAGING. The overall confidence in the extracted data is 97%.

## Confidence Score Summary

| Field                | Confidence Score | Notes                                   |
| :------------------- | :--------------- | :-------------------------------------- |
| Memo Number          | 1.00             | High confidence.                        |
| Memo Amount          | 0.98             | High confidence.                        |
| Currency             | 1.00             | High confidence.                        |
| Memo Date            | 0.97             | High confidence.                        |
| Dealer GSTIN         | 1.00             | High confidence.                        |
| Billed GSTIN         | 0.50             | Low confidence. GSTIN is not provided.  |
| Dealer Name          | 1.00             | High confidence.                        |
| Billed Name          | 0.50             | Low confidence. Name is not provided.   |
| Items                | 0.98             | High confidence.                        |
| Taxes                | 0.97             | High confidence.                        |
| Purchase Order       | 1.00             | High confidence.                        |
| Payment Terms        | 1.00             | High confidence.                        |
| GST Validations      | 0.70             | Moderate confidence. GSTIN statuses are unknown/unverified. |
| Arithmetic Check     | 0.90             | High confidence. An arithmetic error was flagged. |
| Duplicate Check      | 0.90             | High confidence. Not a duplicate.       |
| Price Outliers       | 0.90             | High confidence. No price outliers identified. |
| **Overall Confidence** | **0.97**         | **High confidence.**                    |

## Detailed Line Items

| HSN     | Description          | Quantity | Unit Price (Rs.) | GST Rate (%) | Line Total (Rs.) |
| :------ | :------------------- | :------- | :--------------- | :----------- | :--------------- |
| 44121000 | PLYWOOD CIRCLE - (1065 MM) | 354      | 645.00           | 18.0         | 228,330.00       |

## Taxes

| Tax Type | Rate (%) | Amount (Rs.) |
| :------- | :------- | :----------- |
| CGST     | 9.0      | 20,549.70    |
| SGST     | 9.0      | 20,549.70    |

## Risk Summary and Anomalies

The following anomalies and risks have been identified:

*   **Arithmetic Error:** The memo amount (Rs. 269,429.00) does not reconcile with the sum of the taxable amount (Rs. 228,330.00), CGST (Rs. 20,549.70), and SGST (Rs. 20,549.70), with a discrepancy of Rs. 0.40. This suggests a potential rounding issue or data entry error in the calculation of the total amount.
*   **GST Validation:** The `billed_gstin` status is "unknown" and the `dealer_gstin` status is "unverified." This presents a risk of non-compliance or potential issues with tax credits if the GSTINs are invalid or unverified. The `billed_gstin` field was also extracted with low confidence (0.50).
*   **Missing Information:** The `billed_gstin` and `billed_name` fields are null, indicating a lack of critical billing information.

## Next Steps

1.  **Investigate Arithmetic Discrepancy:** The finance department should investigate the Rs. 0.40 discrepancy identified in the arithmetic check. This may involve re-calculating the total amount based on the line item totals and tax amounts.
2.  **Verify GST Information:** Steps should be taken to verify the `dealer_gstin` and, if possible, obtain and validate the `billed_gstin`. This may require contacting the dealer or using official GST portals.
3.  **Obtain Missing Billing Details:** If possible, efforts should be made to obtain the `billed_gstin` and `billed_name` to ensure complete and accurate record-keeping.
4.  **Review Data Extraction Process:** Given the moderate confidence in `gst_validations` and the low confidence in `billed_gstin` and `billed_name`, a review of the data extraction process for these fields may be warranted to improve accuracy.