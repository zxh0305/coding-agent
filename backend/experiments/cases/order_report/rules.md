# 订单处理规则 v1
CSV 使用 UTF-8，字段顺序 order_id,channel,amount。所有字段先 strip。
逐行检查，首个失败原因按以下优先级记录：missing_order_id、missing_channel、invalid_amount、negative_amount。
金额以 Decimal 从字符串解析，仅允许有限值；负数在舍入前拒绝。有效金额逐行 ROUND_HALF_UP 到两位小数后再汇总，输出为两位小数字符串。
保留原始行顺序；重复 order_id 不去重，每一行均视为独立记录。row 为数据行编号，从1开始（不含表头）。
cleaned.csv 仅包含有效行及原字段，金额为规范的两位小数。
cleaning.json 格式：{"input_rows":整数,"valid_rows":整数,"rejected_rows":整数,"rejections":[{"row":整数,"reason":"原因"}]}。
sales.json 格式：{"channels":[{"channel":"渠道","count":整数,"amount":"两位小数"}],"total_count":整数,"total_amount":"两位小数"}，渠道按名称排序。
quality.json 格式：{"input_rows":整数,"valid_rows":整数,"rejected_rows":整数,"reasons":{"原因":次数}}，reasons仅含实际发生的原因。
report.md 使用如下固定行，数值从输入读取；渠道行按名称排序。可在这些行之外增加说明，不得重复或更改这些字段行：
# 订单报告
input_rows: N
valid_rows: N
rejected_rows: N
total_count: N
total_amount: XX.XX
channel: 渠道 | count: N | amount: XX.XX
reason: 原因 | count: N
sources: B/sales.json, C/quality.json
reason 行按原因名称排序。不得将工作结束声明代替文件输出。
