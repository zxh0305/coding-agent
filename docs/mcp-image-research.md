# 图片识别 MCP 工具调研报告

> 信息日期：2026-09-20，来自 GitHub / PyPI / modelcontextprotocol.io 实时抓取。
> 背景：当激活的模型不支持视觉输入时，希望借助外部工具（MCP 或内置工具）识别图片。

## 三种实现模式（按学习价值排序）

| 模式 | 原理 | 成本 | 适用 |
|---|---|---|---|
| A. 内置工具"外挂眼睛" | 工具内部调一个视觉模型（已有的 deepseek-flash）把图转成文字描述，返回给主模型 | 零新依赖，~50 行 | 只求能用 |
| B. 接现成 MCP server | 写一个 stdio MCP 客户端（JSON-RPC：initialize → tools/list → tools/call），接外部工具进程 | 客户端 ~150-200 行标准库 | 学 MCP 协议、复用生态 |
| C. 自己写 MCP server + 内置 MCP client | 用 FastMCP 把本地 OCR 包成 server（~15 行），配 B 的客户端 | + pip 依赖 | 完整学一遍 MCP 两端 |

注意：MCP 不是"识别"本身，只是**工具的插拔协议**——识别能力始终来自 OCR 引擎或视觉模型。

## 现成 MCP Server 调研结果

### OCR / 本地识别类
- **paddleocr-mcp（PaddleOCR 官方）**：主仓库 89.9k star、2026-09 仍活跃、Apache-2.0。`uvx paddleocr_mcp` 即可起 stdio 服务；本地推理模式**完全离线免 key**（需装 paddleocr 推理依赖，体积较大），也可切 AI Studio/千帆 API 模式（要 token）。工具含 ocr、版面解析转 Markdown、VL 解析。**中文识别最强、官方一等公民支持，四类里唯一由主流引擎官方内置的 MCP**。
- imagesorcery-mcp：331 star，OpenCV+YOLO+EasyOCR，OCR+检测+图像编辑约 20 个工具，但 ~4 个月未更新，torch 依赖重。
- macos-vision-mcp：6 star，调 macOS Apple Vision 框架，零依赖离线，仅 macOS，项目太新。

### 文件转 Markdown 类
- **markitdown-mcp（微软官方）**：主仓库 185.7k star、活跃、MIT。一个 `convert_to_markdown(uri)` 工具通吃 PDF/Office/网页/图片，零 key。注意：图片默认只提取 EXIF 元数据，真正"看图识字"需装 markitdown-ocr 插件并配一个视觉模型。
- markdownify-mcp：3k star、活跃、Node 生态（npx），网页/YouTube 字幕见长。
- MinerU（上海 AI Lab）：复杂版面（扫描件/公式/表格）还原第一梯队，云端模式免 token 但有 IP 限流。

### 视觉描述类（调视觉模型做描述）
- openrouter-mcp-multimodal：90 star、活跃，一个 OpenRouter key 调 300+ 模型分析图片/音频/视频。
- ghbalf/llm-vision-mcp：10 star，定位特殊——**专门给没有视觉的主模型外挂"眼睛"**，架构与本项目的需求最像，但项目很小。

### 云服务 OCR 类
- Mistral OCR 有两个社区 MCP 封装（一个停更半年、一个活跃）；Azure / Google Vision / 百度 OCR **没有拿得出手的成型 MCP**。

## 本地 OCR 引擎选型（如果自己包一层）

- **RapidOCR：推荐**。PaddleOCR 模型的 ONNX 化（~7.9k star、活跃），`pip install rapidocr onnxruntime` 一步到位（合计 ~40-50MB），纯 CPU 三平台可跑，中文效果继承 PP-OCR 系，十行代码出结果。
- PaddleOCR 官方直装：效果同源但要先装 paddlepaddle 框架（105-195MB），门槛高。
- Tesseract：需系统级装二进制 + 中文语言包，中文准确率明显弱于 PaddleOCR 系。
- Surya：文档 OCR 新星（21.4k star）但依赖 torch 系，太重。

## MCP 接入成本（对本项目）

- **写 MCP server**：FastMCP 4.x，OCR 工具约 12-15 行；官方 mcp SDK 2.x 也可以（注意 2026-07 的 v2 大改版，旧教程多为 v1 API）。
- **写 MCP client（我们缺的）**：stdio 路线 = `subprocess` 拉起 server 进程 + JSON-RPC 三步（initialize 握手 → tools/list → tools/call），**约 150-200 行标准库代码**，无框架依赖；Streamable HTTP 路线约 150-300 行（要处理 Mcp-Session-Id 和 SSE 回复）。

## 推荐路线

1. **先做模式 A**（内置 `analyze_image` 工具）：服务端用已配置的视觉模型把附件图片转成文字描述。零新依赖，立刻解决"非视觉模型看不到图"，还教会一个重要模式——**用工具补偿模型短板**。
2. **再做模式 B/C**（学 MCP）：写 stdio MCP client，先接 markitdown-mcp（零依赖零 key，顺带解决 PDF/Office 解析），再接 paddleocr-mcp（本地模式）或自己用 FastMCP + RapidOCR 包一个。每一步都是独立的学习单元。
