-- 在 VS Code 选择 Chinook 本地数据库后逐条运行。
-- 1. 表：当前 public 中有 11 张 Chinook 表。
SELECT table_schema, table_name
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
ORDER BY table_name;

-- 2. 字段及类型：保留字符串长度、金额精度和时间类型。
SELECT table_name, column_name, data_type, character_maximum_length,
       numeric_precision, numeric_scale, is_nullable
FROM information_schema.columns
WHERE table_schema = 'public'
ORDER BY table_name, ordinal_position;

-- 3. 主键和外键定义。
SELECT c.conrelid::regclass::text AS table_name,
       c.conname AS constraint_name, c.contype AS constraint_type,
       pg_get_constraintdef(c.oid) AS definition
FROM pg_constraint c
JOIN pg_namespace n ON n.oid = c.connamespace
WHERE n.nspname = 'public' AND c.contype IN ('p', 'f')
ORDER BY table_name, constraint_type, constraint_name;

-- 4. 逐列关联：支持联合外键的字段位置对应。
SELECT sn.nspname || '.' || st.relname AS source_table,
       sa.attname AS source_column,
       tn.nspname || '.' || tt.relname AS target_table,
       ta.attname AS target_column, c.conname AS constraint_name
FROM pg_constraint c
JOIN pg_class st ON st.oid = c.conrelid
JOIN pg_namespace sn ON sn.oid = st.relnamespace
JOIN pg_class tt ON tt.oid = c.confrelid
JOIN pg_namespace tn ON tn.oid = tt.relnamespace
CROSS JOIN LATERAL unnest(c.conkey, c.confkey) AS k(source_id, target_id)
JOIN pg_attribute sa ON sa.attrelid = st.oid AND sa.attnum = k.source_id
JOIN pg_attribute ta ON ta.attrelid = tt.oid AND ta.attnum = k.target_id
WHERE c.contype = 'f' AND sn.nspname = 'public'
ORDER BY source_table, constraint_name, source_column;

-- 5. 手写关联查询示例：发票关联客户，可用来理解 schema。
-- 查询智能体当前只开放发票销售额汇总，这里展示数据库本身的 JOIN。
SELECT i.invoice_id, i.invoice_date, i.total,
       c.customer_id, c.first_name, c.last_name
FROM public.invoice i
JOIN public.customer c ON c.customer_id = i.customer_id
ORDER BY i.invoice_id
LIMIT 10;
