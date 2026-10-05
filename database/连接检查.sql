-- 在 VS Code 选择“Chinook 本地数据库”连接后运行。
SELECT current_database() AS database_name,
       current_user AS username,
       inet_server_addr() AS server_address,
       inet_server_port() AS server_port;

-- 当前演示数据为 412 张发票、2240 条发票明细、3503 首曲目。
SELECT COUNT(*) AS invoice_count FROM public.invoice;
SELECT COUNT(*) AS invoice_line_count FROM public.invoice_line;
SELECT COUNT(*) AS track_count FROM public.track;

-- 发票日期无时区；金额未声明币种。
SELECT invoice_id, customer_id, invoice_date, billing_country, total
FROM public.invoice
ORDER BY invoice_id
LIMIT 10;

SELECT COALESCE(SUM(total), 0) AS sales_amount
FROM public.invoice
WHERE invoice_date >= TIMESTAMP '2024-01-01'
  AND invoice_date < TIMESTAMP '2025-01-01'
  AND billing_country = 'USA';
