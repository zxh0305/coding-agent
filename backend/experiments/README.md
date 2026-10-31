# 智序实验平台：第一阶段

实验核心支持独立命令行运行；网页管理复用同一核心，不初始化/迁移聊天数据库，不使用持久记忆。
复用现有 Agent 循环，固定 A→B/C→D，按 A、B、C、D 串行运行。
每节点重新创建 Agent 和客户端；同一成员配置可以被多个节点复用，历史不共享。

## 运行

从项目根目录进入 `backend`，复制场景模板到**同一个目录**，修改两名成员的 provider_id 和 model：

```sh
cd backend
cp experiments/cases/order_report/experiment.example.json experiments/cases/order_report/experiment.local.json
python3 -m experiments.runner --case order_report --config experiments/cases/order_report/experiment.local.json
```

供应商 ID 和模型名使用现有网页中已配置且启用的条目，必须精确匹配。
密钥仅从现有 `data/agent_data.db` 以 SQLite 只读连接加载，不写入实验配置与导出文件。
没有数据库、模型被禁用、名称不匹配、密钥缺失均明确失败，不选择替代模型。
配置只接受 agents 数组及示例成员字段；提示词路径相对配置文件所在目录。
`max_rounds` 限制常规轮数，现有 Agent 达到上限后还会进行一次禁工具收尾调用；这些调用也记录。
每请求 timeout 为秒，默认60，范围1—600。第一阶段无货币预算保证。
如 Python 提示证书错误，可以指定有效系统证书库（macOS 示例），保持证书验证开启：

```sh
SSL_CERT_FILE=/etc/ssl/cert.pem python3 -m experiments.runner --case order_report --config experiments/cases/order_report/experiment.local.json
```

可通过 `--output-root /path/to/runs` 指定运行目录。默认输出项目根目录的 `runs/<随机run_id>/`。
终端 Ctrl-C 停止当前执行，保留结果；此后节点不再执行。
退出码：0=完整证据且所有节点执行和最终评价通过；1=执行/验收/证据失败或停止；2=入口配置错误。

## 固定场景

`cases/order_report/orders.csv` 包含虚构订单与可明确判定的异常数据。
`rules.md` 定义字段、Decimal逐行ROUND_HALF_UP、拒绝优先级、重复订单保留和所有输出格式。
A 清洗，B 销售统计，C 数据质量，D 报告。两名成员分别负责A/B与C/D。
仅支持 read_file、write_file、list_dir；不能调用 Bash、记忆、子代理或未授权工具。

Agent读取 `nodes/<节点>/inputs/` 下的声明输入，输出仅可写本节点声明的文件名。
宿主在工具执行前检查解析后的真实路径，拒绝越界、符号链接越界、改写输入和未声明输出。
这是文件工具范围内的访问约束；没有提供通用代码执行隔离或容器能力。
每份声明输入必须实际执行成功的读取，读取路径和当时SHA256记录于原始事件。
上下游通过已发布产物的校验副本交接，复制前后核验哈希，不依赖聊天文本。

## 验收口径

- `validated`：通过公开的运行时产物契约，允许下游消费。**不代表业务答案正确。**
- 运行时检查文件、格式、必要字段、基本计数一致性和输入读取证据，不读取独立计算答案。
- 宿主完成执行后，依据本次冻结的原始订单输入独立计算最终答案，分别评分A/B/C/D。
- 最终评价不反馈到Agent，不触发自动修复。报告按预定义字段行检查关键数字及来源，不评价文风。
- `execution_failed`、`validation_failed`、`blocked`、`stopped` 均不算完整成功。
- 上游运行时验收失败阻塞后继；分支B失败不阻止无依赖关系的C。

## 导出与用量

- manifest.json：任务、成员、提示词正文/哈希、输入哈希、源码哈希及可获取的Git版本。
- case/：本次原始订单与规则的冻结副本。
- events.jsonl：未裁剪的Agent事件、工具请求/完整结果、节点状态、交接与发布。
- requests.jsonl：每次实际HTTP尝试的开始、请求体、返回数据、结束/异常，覆盖内部重试与协议降级。
- artifacts.json：产物哈希、生产尝试与消费输入版本。
- validation.json：运行时检查与宿主最终评价。
- summary.json：节点、分配、验收、用量与未知项、请求错误、完整证据状态。

每个模型请求有独立 request_id；工具请求/结果共享 tool_call_id，并关联触发它的请求。
原始供应商usage及返回model标识保留；聚合按成员/供应商/配置模型分组，另列实际返回模型。
OpenAI与Anthropic原始计费字段分别保留，不把两种协议的input计数含义强行等同。
缺失用量的请求保留unknown，不作为0；summary的tokens仅为已报告字段的小计，不能当完整总量。
缓存读取/创建字段在供应商返回时保留。第一阶段不计算货币费用。
导出不含认证头或API密钥，但请求正文和工具结果含任务内容，应按研究数据管理。
关键记录写入失败则 evidence_complete=false，阻止继续请求，结果不能算成功。
磁盘完全不可写时无法保证保存summary，应同时检查命令行输出和退出码。

## 验证

```sh
cd backend
python3 -m unittest tests.test_experiments -v
```

测试使用真实Agent循环和协议客户端配合模拟HTTP响应，检查模型隔离、节点依赖、文件边界、
输入版本、请求/工具关联、OpenAI/Anthropic用量、重试、异常、独立运行及证据完整性。
模拟响应仅验证基础设施，不证明模型能力或调度收益。注入测试客户端时，manifest和summary中的
`execution_backend`为`injected_client_factory`，正常命令行模型执行则为`configured_models`。

实验核心不包含策略优化、修订恢复或Skill管理。后续增加的网页管理入口见下文。

## 网页管理（2026-10-31）

从项目根目录按原方式启动：

```sh
python3 backend/app.py
```

浏览器打开启动日志所示地址（默认 http://localhost:8000），登录后点击侧栏“实验”；窄屏也有实验入口。
列表可查看本账号的历史实验。点击“新建实验”，分别选择两名成员的供应商/启用模型、轮数和请求超时，
提交后进入详情。详情展示固定任务图、配置及实际返回模型、输入来源/哈希、产物、两类验收和用量。
停止按钮使用共享取消信号；刷新或关闭页面只清理页面轮询，不停止后台实验。
详情与列表通过约2秒轮询更新；摘要只读取小文件与增量索引，原始记录按需展开。
页面保留最近150条时间线摘要，完整JSONL仍在磁盘；切换类型会从头按页重读。
Markdown复用现有净化渲染，其他文本（包含HTML）按源码展示；下载始终为附件。

### 归属和历史实验

网页创建的实验自动属于当前登录用户。归属、创建时间和后台状态写入
`data/experiment-web/<run_id>.json`，不修改数据库结构，也不修改已有runs。
无归属的命令行历史不默认公开。操作人员确认归属后，在本机明确关联已有账号：

```sh
cd backend
python3 -m experiments.web --assign-owner a1b5cc7efd06409baa29f073e0181503 --username '当前登录用户名'
```

将示例用户名替换成实际登录账号。认领只创建旁路归属记录，不改动原始产物/manifest/日志，禁止覆盖已有关联。
没有开放网页“凭编号认领”或任意文件目录导入接口；run_id本身不构成授权。
该命令仅适用于默认runs目录中格式合法的历史运行。

### 状态与后台执行

- `completed`只表示执行结束，最终评价仍可能未通过；`validated`只表示产物契约通过。
- 未执行最终评价显示“未评价”。停止后不再执行后续节点，不自动修复/重跑。
- 服务重启后，未确认结束的网页任务以“已中断”展示；不会永久显示运行中。
- 同一服务进程同时只允许一个网页实验；锁在后端检查，其他启动请求返回409。
- 首版面向单进程本机服务。多个独立后端进程之间没有跨进程执行锁，也不支持跨进程停止CLI实验。
- 原始供应商用量计数不强行统一；未知项不补零，输入/输出展示为已报告小计，不计算货币费用。
- 超大记录（单行>2MB）及产物（>4MB）会明确拒绝网页读取；磁盘原始记录保留。
- 停止沿用现有Agent及模型连接取消机制；连接尚未建立时，底层连接/请求超时可能影响收尾时延。

### HTTP接口

所有接口沿用Bearer身份认证，逐次校验归属。模型列表复用已有 `GET /api/models`，不返回密钥。

| 方法与路径 | 用途 |
| --- | --- |
| GET /api/experiments | 当前用户的实验列表 |
| POST /api/experiments | `{case:"order_report",agents:[{id,provider_id,model,max_rounds,timeout},…]}`；立即返回202及run_id |
| GET /api/experiments/{id}/summary | 轻量状态、节点和用量 |
| GET /api/experiments/{id} | 详情、产物、检查和最终评价 |
| GET /api/experiments/{id}/events?cursor=0&limit=50 | 字节游标分页的执行摘要 |
| GET /api/experiments/{id}/requests?cursor=0&limit=50 | 请求与重试摘要 |
| GET 同上并添加raw=1 | 按需加载已脱敏原始记录；用摘要的offset和limit=1展开 |
| POST /api/experiments/{id}/stop | 请求体为空对象，停止实验 |
| GET /api/experiments/{id}/artifact?id=D%2Freport.md | 受控产物预览 |
| GET /api/experiments/{id}/download?id=D%2Freport.md | 校验声明身份、真实路径、符号链接和SHA256后下载 |

事件接口cursor为服务端返回的不透明字节位置，不是行号。limit范围1—100；响应含cursor、more与items。
原始记录脱敏后返回，认证头、连接地址、密钥字段不发送给页面。后台仅接受固定场景及明确成员配置，
拒绝浏览器传入提示词路径、命令、工作目录、输出根目录和验收器。

### 网页验证

```sh
cd backend
python3 -m unittest tests.test_experiments_web -v
```

从项目根目录运行前端测试：`node --test frontend/*.test.mjs`。
这轮网页测试使用临时数据库、虚构数据和模拟客户端；未新增任何付费模型调用。
之前完成的真实DeepSeek实验仍为独立历史证据，不能将网页模拟验收当作新的真实模型结果。
