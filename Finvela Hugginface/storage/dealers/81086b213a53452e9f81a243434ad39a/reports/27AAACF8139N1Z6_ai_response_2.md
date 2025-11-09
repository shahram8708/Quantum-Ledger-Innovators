# Finance Report: Memo 270

## Executive Summary

This report details Memo number **270**, dated **27-07-2025**, with a total amount of **920,400.00 INR**. The memo is from **Firefly Fire Pumps Pvt. Ltd.** and is associated with Purchase Order **PO-5130010213**. The overall confidence score for this memo's data is **0.95**.

## Confidence Summary

| Field                     | Confidence Score | Notes                                              |
| :------------------------ | :--------------- | :------------------------------------------------- |
| Memo Number               | 1.0              | High confidence.                                   |
| Memo Amount               | 0.95             | High confidence.                                   |
| Currency                  | 1.0              | High confidence.                                   |
| Memo Date                 | 1.0              | High confidence.                                   |
| Dealer GSTIN              | 1.0              | High confidence.                                   |
| Billed GSTIN              | 0.0              | **Anomaly:** Billed GSTIN is missing.              |
| Dealer Name               | 1.0              | High confidence.                                   |
| Billed Name               | 0.5              | Moderate confidence. "CONFIDENTIAL" provided.      |
| Items                     | 0.95             | High confidence.                                   |
| Taxes                     | 0.95             | High confidence.                                   |
| Purchase Order Numbers    | 1.0              | High confidence.                                   |
| Payment Terms             | 1.0              | High confidence.                                   |
| GST Validations           | 0.5              | Moderate confidence. Dealer GSTIN unverified.      |
| Arithmetic Check          | 1.0              | High confidence. No arithmetic errors detected.    |
| Duplicate Check           | 1.0              | High confidence. Not a duplicate.                  |
| Price Outliers            | 1.0              | High confidence. No price outliers detected.       |
| **Overall Memo Confidence** | **0.95**         |                                                    |

## Detailed Line Items

| HSN      | Description                                | Quantity | Unit Price   | GST Rate | Line Total |
| :------- | :----------------------------------------- | :------- | :----------- | :------- | :--------- |
| 84137010 | Trailer Fire Fighting Pump MFT 1800 D With Accessories | 1.0      | 780,000.00   | 18.0%    | 780,000.00 |

**Analysis:**
*   The line item total calculation is consistent with the unit price and quantity.

## Taxes

| Tax Type | Rate  | Amount     |
| :------- | :---- | :--------- |
| IGST     | 18.0% | 140,400.00 |

**Analysis:**
*   The IGST amount is correctly calculated as 18% of the line item total (780,000.00 * 0.18 = 140,400.00).
*   The total memo amount (920,400.00) is the sum of the line item total and the IGST amount (780,000.00 + 140,400.00 = 920,400.00), indicating arithmetic consistency.

## Risk Summary

*   **Missing Billed GSTIN:** The `billed_gstin` is `null`. This is a significant anomaly as it prevents proper validation of the recipient and may impact input tax credit claims. The confidence score for this field is **0.0**.
*   **Unverified Dealer GSTIN:** The `dealer_gstin` is marked as `unverified` in the GST validations. While the GSTIN itself is present with high confidence, its verification status indicates a potential for it not being active or valid. The confidence score for `gst_validations` is **0.5**.
*   **Confidential Billed Name:** The `billed_name` is marked as "CONFIDENTIAL", which has a moderate confidence score of **0.5**. This should ideally be a specific entity name for better record-keeping.
*   **No Arithmetic Errors or Duplicates:** The `arithmetic_check` and `duplicate_check` both indicate that there are no such issues, which is positive.
*   **No Price Outliers:** The `price_outliers` check found no anomalies, suggesting that the unit price is within expected ranges based on internal checks.

## Next Steps

1.  **Obtain Billed GSTIN:** The immediate priority is to obtain the correct and complete `billed_gstin` from the relevant department or source document. This is crucial for regulatory compliance and financial reconciliation.
2.  **Verify Dealer GSTIN:** Initiate a process to verify the `dealer_gstin` (27AAACF8139N1Z6) through official GST portals to confirm its validity and active status.
3.  **Clarify Billed Name:** If possible, seek to replace "CONFIDENTIAL" with the actual name of the billed entity to ensure accurate record-keeping.
4.  **Record Keeping:** File Memo **270** with the associated Purchase Order **PO-5130010213** and ensure all documentation is complete, especially regarding the missing `billed_gstin`.
5.  **Review Payment Terms:** Note the payment terms of "30 Days after receipt of Material" for timely processing.