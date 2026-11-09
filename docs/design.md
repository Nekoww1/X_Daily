# 设计说明

本文说明从采集到企业微信推送的实际数据流、设计取舍和保护措施。日常部署在 Ubuntu 上，由 systemd user 定时器于北京时间 08:00 启动 `radar.py`，采集完成后运行 `digest.py --send`。

## 1. 信息源与采集：`radar.py`

### 来源配置与时间口径

`sources.json` 保存来源数组 `sources` 和文字说明 `not_covered`。采集按 `type` 分派到不同函数，`kind` 用于候选评分和后续展示。

| `type` | 采集函数 | 材料 | 时间口径 |
|---|---|---|---|
| `x` | `collect_x` | 本地 RSSHub 的账号时间线，必要时读取 thread 详情 | 发布时间落在报告窗口内 |
| `feed` | `collect_feed` | RSS / Atom 标题、摘要、链接 | 优先发布时间，否则使用更新时间，落在报告窗口内 |
| `hf_daily` | `collect_hf_daily` | Hugging Face Daily Papers | 使用窗口起始日查询上榜列表，保留 `submittedOnDailyAt` |
| `arxiv` | `collect_arxiv` | arXiv 查询结果中的标题和摘要 | 提交时间，默认额外回看 2 天 |

`report_window(day)` 返回北京时间 `day 08:00` 到次日 `08:00`，左闭右开。两个主程序默认将昨天作为 `day`。因此每天 08:00 启动时，常规窗口正好覆盖前一天 08:00 至当天 08:00。采用这一窗口，是为了将北京时间 22:00–08:00 的美国研究者发帖高峰放在同一期中。

代码中的时区常量名为 `TAIPEI`，实际使用固定 UTC+8；本项目的部署、报告文字和定时器均采用北京时间口径。

论文不是严格按这个窗口中的公开时间采集：

- Hugging Face 请求日期列表，单次 `limit=100`，不再用 `in_window` 过滤返回结果，采集报告中标为“上榜”。
- arXiv 通过 `submittedDate` 查询，从窗口起点减去 `lookback_days`，默认 2 天，到窗口终点前一分钟；单次 `max_results=100`，按提交时间倒序，报告中标为“提交”。
- 两者反映上榜或提交日期，论文公开时间可能不同。arXiv 的回看与后面的历史去重配合使用。

### 请求组织与错误隔离

普通 feed 与 Hugging Face 来源通过最多 8 个工作线程采集。arXiv 串行采集，来源之间等待 3 秒。X 最后单独串行处理，并在每轮随机排列账号，避免每次都由相同账号消耗剩余额度。

通用 `fetch` 的默认超时为 45 秒、失败后最多重试 2 次。对可重试的 HTTP 状态及网络错误，等待时间按重试次数递增。X 关闭这层重试，统一由 `XPacer` 管理节奏。

每个来源单独返回结果。一个来源的网络、日期或结构异常只使该来源失败，不丢掉整轮报告。采集完仍会保存报告和数据；只有所有来源均为 `failed` 时，命令行退出码为 1。

### X 的节流、预算与熔断

采集端调用以下 RSSHub 路由：

```text
/twitter/user/HANDLE/count=20&includeRts=false&includeReplies=false
/twitter/user/HANDLE/count=40&includeRts=false&includeReplies=false
/twitter/tweet/HANDLE/status/POST_ID
```

先读取 `count=20`。若返回条目中早于窗口起点的不足 2 条，再请求一次 `count=40`。要求至少两条旧帖，是为了降低置顶帖或旧 thread 根帖造成的误判；读完仍不足时标为 `partial`。

`patches/rsshub-timeline.diff` 对外部 RSSHub 做两项修改：按底部游标读取所需页数，并保留作者回复自己的贴文。没有这项补丁，`count=40` 和 thread 合并所依赖的时间线行为可能不成立。

所有时间线、扩展时间线和 thread 详情请求共用一个 `XPacer`：

| 常量或规则 | 当前值与行为 | 目的 |
|---|---|---|
| `X_REQUEST_DELAY_SECONDS` | 同账号后续请求随机等待 12–24 秒 | 拉开分页和详情请求 |
| `X_ACCOUNT_DELAY_SECONDS` | 切换账号随机等待 20–34 秒 | 拉开账号之间的访问 |
| `X_MAX_REQUESTS` | 每轮最多 75 次，包含失败请求 | 限制采集端请求总量 |
| `X_PHASE_SECONDS` | 45 分钟 | 限制 X 阶段的等待和请求耗时 |
| `X_BACKOFF_SECONDS` | 首次限流信号后至少等待 180 秒 | 降低异常后的访问频率 |
| `Retry-After` | 支持秒数与 HTTP 日期；比 180 秒更长时取更长值 | 尊重上游退避要求 |
| `X_MAX_TROUBLE_SIGNALS` | 第二次限流信号立即停止本轮 X | 避免持续撞限流，期间成功不会清零 |
| `X_MAX_CONSECUTIVE_FAILURES` | 连续 3 个账号为 `failed` 后停读剩余账号 | 在登录或服务整体异常时提前结束 |

首次请求没有随机等待。请求前若等待将触及阶段截止时间，则直接停止；实际请求超时也会缩短到阶段剩余时间内。普通时间线默认超时 45 秒，thread 详情请求超时 90 秒，但都受剩余时间约束。

限流信号包括 HTTP 429，以及错误信息中匹配限流含义的文本。RSSHub 对多种错误都可能返回 503，因此普通 503 只算失败；响应为 HTTP 200 但内容是无 feed 条目的限流错误页，也会触发退避。真实贴文谈论限流不会仅因此触发熔断。

`x_metrics.requests` 记录采集端发出的 RSSHub 请求次数，`duration_seconds` 包括 X 阶段的等待和响应耗时。它不是 RSSHub 内部向 X 发出的底层请求次数；补丁中的分页可能让一个路由请求读取多页。

### Thread 合并

只有标题以 `Re ` 开始，或材料中出现 `🧵`、`thread` 的候选才读取详情。普通贴文不补查详情，以节省请求。

详情处理规则如下：

1. 只保留链接属于目标作者、且有发布时间的贴文。
2. 对以 `Re @` 开始的内容，只有它也在原创时间线中出现时才保留，借助补丁排除作者回复他人的内容。
3. 按贴文 ID 排序，拼接作者自己的内容，并清理 `Re ` 前缀。
4. 根帖属于本期时，使用根帖链接和发布时间，将同一 thread 的其他候选合并掉。
5. 如果是给旧 thread 新增内容，保留新帖自己的链接，并注明“接续”，避免被旧根帖的历史去重吞掉。

合并摘要最多保留 `THREAD_SUMMARY_LIMIT=4000` 个字符。`thread_parts` 记录读到的段数，不保证是完整 thread。详情读取失败时保留已经取得的候选，将来源标为部分覆盖，并停止该账号余下的详情请求。

标题以 `[Subscribers Only]` 开始的贴文会被跳过，并记入来源说明。

### 候选结构与材料长度

基础候选由 `make_item` 构建：

```text
source, kind, coverage, title, original_url,
published_at, fetched_at, summary
```

来源还可能添加 `upvotes`、`time_label`、`thread_parts`。合并后增加 `key`、`also_from`、`section`、`score` 和 `id`。

HTML 标签被去除，空白被合并。一般摘要默认截到 1200 个字符；X 设置为 4000。系统没有自动读取博客全文、论文 PDF 或贴文外链的步骤，后续模型只能使用留下来的材料。

### 去重与 `state/seen.json`

`canonical_key` 将 arXiv 的摘要页、PDF 链接和不同版本统一为论文 ID。其他链接去掉查询参数、片段及末尾斜杠，并将 HTTP 形式统一为 HTTPS。

同轮相同键只保留一条候选，将其他来源记入 `also_from`，点赞数取较大值。它不会自动拼接两份不同摘要。同一事件但链接不同的候选，留给 Codex 选题阶段合并。

`state/seen.json` 保存规范键到首次处理日报日期的映射：

- 先前日期已处理的候选，本期不再加入。
- 同日期重跑仍保留候选。
- 记录的是所有候选，不只是最终入选条目。
- 删除历史状态可能使回看范围内的旧论文重新出现。

候选按规则分类和评分排序，再分配 `c1` 形式的 ID。规则分只用于初筛和排序，不代替 Codex 的最终选题；主生成流水线会接收全部候选。

### 覆盖说明

| 状态 | 含义与处理 |
|---|---|
| `ok_new` | 抓取成功并有候选 |
| `ok_empty` | 抓取成功但没有候选 |
| `partial` | 缺少日期、窗口覆盖不足、达到返回数量限制、X 数据疑似过旧或详情未完整取得等 |
| `failed` | 无法读取或解析；不能视为没有更新 |

X 最新贴文距窗口终点超过 `X_STALE_DAYS=30` 天时，会提示可能返回了登录失效后的旧数据。这是启发式判断，也可能只是账号长期不发原创帖。

Markdown 采集报告包含完整来源状态表。正常状态的 X 账号不足一半时，报告增加 X 内容不可信的警告。`not_covered` 的文字也会附在报告末尾。

最终 HTML 的覆盖信息较简略，只列总来源数、失败来源、分页覆盖不足的账号及候选与入选数量。排查详细的 `partial` 原因，应查看采集 JSON 或 Markdown，不能只看最终文章。

## 2. 找新来源：`find_sources.py`

来源发现与每日写作分开运行。它用于扩大候选池，不在日报生成期间临时搜索补充事实。

脚本读取画像的 `focus`，以及 `sources.json` 中已有的账号和来源名，将其填入 `prompts/find_sources.md`。这次调用通过 `run_codex(..., search=True)` 显式启用搜索，默认使用选题模型。

提示词要求推荐约 45 个 X 账号、约 10 个博客和 3–5 条较窄的 arXiv 查询，并要求为 X handle 给出核实网页。数量与方向配额是提示词要求，程序没有硬性校验。

返回 schema 为：

| 数组 | 每项必需字段 |
|---|---|
| `x` | `handle`、`name`、`affiliation`、`direction`、`tier`、`why`、`evidence_url` |
| `blogs` | `name`、`url`、`direction`、`tier`、`why` |
| `arxiv` | `query`、`direction`、`why` |

`direction` 必须取自画像方向名，`tier` 为 `core` 或 `public`。`public` 为以后面向大众的版本保留分类，目前不参与每日采集筛选。

搜索结果先写入 `data/source-candidates.json`，随后逐个调用本地 RSSHub 的 `count=40` 路由，检查 X 账号能否读取、样本中有多少条发表于近 30 天，以及最新日期。账号之间等待 20 秒，每查完一个就保存进度。

这项检查直接使用 `radar.fetch`，没有复用每日采集的 `XPacer`，也没有相同的总预算和熔断。`posts_30d` 只反映返回样本，不是完整月度统计；读不到也不能直接判定账号不存在。

`--skip-search` 读取已有推荐并重新检查 X。脚本不会修改 `sources.json`，也不会独立审核搜索证据或博客 feed 是否有效，最终由维护者决定添加哪些来源。

## 3. 读者画像：三张卡片

画像来自 `profiles/NAME.toml`，默认名称由 `DIGEST_PROFILE` 决定，未设置时为 `personal`。仓库只公开 `profiles/example.toml`。

| 卡片 | 字段 | 职责 | 如何进入提示词 |
|---|---|---|---|
| `[audience]` | `who`、`known`、`unfamiliar` | 决定解释对象和术语门槛 | `reader_text` 填入选题、正文的 `PROFILE` |
| `[[focus]]` | `name`、`share`、`topics` | 决定选题方向和参考比例 | `focus_text` 填入选题及来源发现的 `FOCUS` |
| `[research]`，可选 | `summary`、`links`、`max_items` | 提供个人研究背景，约束“和你的研究” | `summary` 进入读者文字，`links` 和数量限制进入正文的 `YOU_RULE` |

设计意图是把“选什么”和“怎么解释”拆开：选题以方向卡为主，读者卡用于表达难度，研究卡只在存在具体联系时增加点评。实际调用中，`reader_text` 也把研究摘要传给选题模型，因此这一分工依靠提示词约束，并非信息完全隔离。

`share` 不会由代码校验总和或强制执行。没有 `[research]` 时，提示词要求 `you` 为空，构建阶段还会再次清空它；有研究卡时，代码只保留前 `max_items` 个非空点评。

`DIGEST_PROFILE` 和模型配置均在模块导入时读取。只在之后由程序加载的 `.env` 中填写这些变量，不会更新已经求值的常量；应在启动 Python 前导出，或在 systemd 服务中设置。

## 4. 生成流水线：`digest.py`

### 输入与调用边界

`build` 读取 `data/YYYY-MM-DD.json`、读者画像和 `glossary.json`。没有候选或选题后没有条目时直接失败。

`SOURCE_CHARS` 对送给模型的单条摘要设置上限：X 为 4000、论文为 2000、博客为 2500 个字符。这只是在已有候选上的再次截断，不能恢复采集阶段已经丢掉的内容。

每次 `run_codex` 在临时目录中写 schema 和输出文件，通过标准输入发送提示词。调用包含：

```text
codex exec --ephemeral --skip-git-repo-check --ignore-user-config
-m MODEL -c model_reasoning_effort="EFFORT"
-s read-only -C TEMP_DIR --output-schema SCHEMA -o OUTPUT -
```

Codex 使用 ChatGPT 账号登录，`--ignore-user-config` 用于忽略本地其他 provider 配置。日常四阶段没有启用 `--search`。提示词要求不执行命令、不读写文件、不联网，并将抓取材料视为外部数据。

结构化输出约束 JSON 形状；它不保证事实、数量和文字质量。

### 阶段与模型

| 阶段 | 输入 | 输出 | 默认模型 / 推理强度 |
|---|---|---|---|
| `select` | 全部候选、读者文字、方向比例 | 入选候选组合、来源显示名、头条、证据类型、未选理由 | `gpt-6-luna` / `max` |
| `write` | 入选条目的原始材料、画像、术语名列表 | 标题、正文、研究点评、补充、引文、新术语 | `gpt-6.1-sol` / `high` |
| `write-retry` | 校验失败或漏写的条目，以及问题列表 | 与 `write` 相同 | `gpt-6.1-sol` / `high` |
| `highlight` | 写好并完成兜底整理的正文 | 带 `==…==` 的正文副本 | `gpt-6.1-sol` / `high` |
| `lead` | 所有条目的来源、头条状态、去标记标题与正文 | 今日导读 | `gpt-6-luna` / `max` |

选题、导读由 `DIGEST_MODEL` 和 `DIGEST_EFFORT` 覆盖。正文、正文重写、重点标记由 `DIGEST_WRITER_MODEL` 和 `DIGEST_WRITER_EFFORT` 覆盖。

### `select` 的 schema 与整理

模型输入每条包含：

```text
id, source, kind, title, text, thread_parts, upvotes
```

模型输出：

```json
{
  "items": [
    {"ids": ["c1"], "who": "来源显示名", "top": true, "ev": "first"}
  ],
  "cut": ["来源：内容——未选原因"]
}
```

`ev` 只能是 `first`、`third`、`comment`、`claim`，分别表示一手发布、第三方评测、观点评论和未独立核验的声称。

提示词要求优先一手研究和有实质内容的讨论，通常选 10–14 条，X 与博客应占一半以上，论文最多 5 篇。同一事件可合并多个 `ids`，信息最完整的一手候选排第一。

代码会过滤不存在或已被使用的候选 ID，最多保留 3 个头条，并将非空 `cut` 截到 10 行。论文数、总条目数和来源占比没有代码硬限制。

链接由程序从第一个真实候选取得，模型不能自由指定链接。合并后的 `source_text` 包含各候选材料，但最终条目只展示第一条候选的原文链接。非论文按第一条候选的来源分组，论文统一归入论文组。

### `write` 与 `write-retry` 的 schema

写作输入每条包含 `id`、`top`、`who`、`source`，其中 `source` 是合并后的原始材料。重写输入额外包含 `上次的问题` 列表，只发送需要修复的条目。

输出为：

```json
{
  "items": [
    {
      "id": "c1",
      "title": "中文标题",
      "text": "一段连贯正文",
      "you": "",
      "extra": "",
      "quote": "",
      "new_terms": [
        {"term": "术语", "definition": "通俗释义"}
      ]
    }
  ]
}
```

所有字段均必需，可选文字使用空字符串。`new_terms` 可为空数组。schema 中的对象都禁止额外属性。

正文要求将事件、细节和意义写成一段，不拆成列表。程序按 ID 接收结果，发现漏写或校验问题后，以相同写作提示词附带反馈，执行一次 `write-retry`。两次都漏写的条目跳过，其余进入兜底整理。

### `highlight` 与 `lead` 的 schema

重点输入：

```text
items: [{id, top, text}]
```

重点输出：

```json
{"items": [{"id": "c1", "text": "带 ==重点== 的正文"}]}
```

导读输入：

```text
[{top, who, title, text}]
```

导读输出：

```json
{"lead": "今日导读正文"}
```

导读不接收画像，由已经写好的条目归纳主线。代码随后检查导读数字；检查不通过时整段导读不用。

### 缓存、日志与重试

`Steps` 在 `data/digest-work/YYYY-MM-DD/` 下按阶段保存：

| 文件形式 | 内容 |
|---|---|
| `select.json`、`write.json`、`write-retry.json`、`highlight.json`、`lead.json` | 阶段原始返回值，按实际执行的阶段生成 |
| `LABEL.prompt.md` | 该阶段完整提示词 |
| `LABEL.1.log`、`LABEL.2.log` | 调用退出码，以及 stdout、stderr 各自末尾最多 20000 个字符 |

已有阶段 JSON 时直接读取，不再次调用模型。缺失时执行，遇到超时、非零退出或 JSON 读取错误等异常，最多尝试两次。仍失败则整条流水线报错。

这里有两层不同的重试：调用异常由 `Steps` 重试；正文内容校验失败由 `write-retry` 重写。重点和导读没有对应的内容重写阶段，不能把当前实现描述成每个校验问题都会自动重写。

缓存只按日期和阶段名区分，不包含输入、画像、提示词或模型的指纹。修改这些内容后必须使用 `--fresh`，否则可能复用旧结果。`--fresh` 删除该工作目录的 `*.json`，不清理日志，也不重跑采集或清空历史去重状态。

## 5. 忠实性检查与降级边界

### 检查范围

`check_written` 检查标题、正文、研究点评、补充信息中的数字，正文长度、引文以及术语标记。目标是发现明显偏离材料的输出，不能替代语义核验。

| 项目 | 提示词要求 | 实际代码行为 |
|---|---|---|
| 数字 | 只用原始材料里的数字，允许合理单位换算 | 比对数字记号集合，并支持有限英文数词和缩放换算 |
| 引文 | 原样复制英文句子，不超过 30 词 | 规范化后检查是否为材料的连续子串；不检查词数 |
| 术语 | 陌生词首次出现时标记，每条 0–3 个，并有释义 | 检查已标术语是否在术语表或 `new_terms` 中，以及标记数量是否配对 |
| 正文长度 | 头条 180–230 字，其他 120–180 字 | 去标记后按 Python 字符数接受头条 170–240、其他 110–190 |
| 标题 | 不超过 40 字 | 检查非空，未实现标题长度检查 |
| 重点 | 每段 1–2 处，每处 6–30 字 | 接受 1–2 处、每处 4–40 个去标记字符，并映射回原正文 |
| 导读 | 2–3 句、90–140 字 | 检查数字来源，未检查长度或句数 |
| 研究点评 | 不超过 70 字，限制每日条数 | 代码限制非空条数，未检查单条长度 |

正文长度的代码范围留有容差，反馈仍要求回到提示词目标范围。这里的“字数”包含英文字符和空格，不是中文分词或英文词数。

### 数字来源

`source_numbers` 从材料中建立允许集合：

- 提取阿拉伯数字，去掉千位逗号及小数尾零。
- 支持 `zero` 到 `twenty`，以及 `thirty`、`forty`、`fifty`、`hundred` 的单个英文数词。
- 支持 `M` / `million`、`B` / `billion`、`k` / `thousand` 的缩放，例如 `16M` 对应 `1600` 万、`2B` 对应 `20` 亿、`50k` 对应 `5` 万。

这是有限的记号匹配，不是完整数词解析或量纲检查。它不会验证数字是否用于正确对象、单位是否正确，或结论是否由数字支持；没有数字的错误事实也可能通过。

### 引文

`normalize` 统一部分弯引号、撇号、长短横线和连续空白，再做连续子串检查。因此实现接近规范化后的逐字一致，不是逐字节相等，也不允许拼接两段原话。

正文重写后仍对不上的引文会被删除，并记录警告。

### 术语

`[[术语]]` 只用于标题、正文和研究点评。写作提示词提供已有术语的名称列表；模型为新术语返回 `term` 和 `definition`。

兜底阶段会去掉没有可用解释的新术语标记，保留词本身。构建时收集真正使用的术语，优先使用已有 `glossary.json` 的释义，再使用新释义，并将使用的新词合并回术语表。

代码检查的是标记和解释是否存在，不核验释义事实，也不保证所有应解释的陌生词都已标记。生成日报会修改 `glossary.json`，维护者提交术语表前应审阅新增内容。

### 重点只能回填到原正文

`apply_highlight` 只从模型返回值中提取 `==…==` 片段，然后回到写作阶段保存的正文中寻找对应位置。匹配可忽略空白差异，但实际输出始终取原正文片段。

它拒绝不存在于原正文、相互重叠、数量不对、长度不符或拆开 `[[术语]]` 的标记。模型在重点以外改了其他字，也不会被带入成品。

这里的原正文是写作完成的段落，不是外部来源全文。重点映射保证排版阶段不改写正文，不能证明被强调的事实本身正确。失败时该段保留正文但不加重点，并记警告。

### 中英文间距与最终降级

`space_cjk` 在汉字与部分英文字母、数字、美元符号、百分号之间补空格，并识别相邻的术语和重点标记。它用于标题、正文、研究点评、补充文字和导读，不改英文引文。

重写后，`finalize_written` 会删除无效引文、移除无释义标记、整理间距，再检查一次并记录残留问题。

需要明确当前降级边界：残留的无来源数字、空正文或长度问题可能只写进 `warnings`，不会自动删除对应内容，也不会阻止 `--send`。调用两次失败会中止生成；内容检查没有达到同样严格的阻断程度。`warnings` 保存在最终 JSON，并打印到进程日志，不显示在文章正文中。

导读若出现无法在条目标题、正文或关联材料中找到的数字，会整段删除；未出现此类问题时保留。系统没有跨来源独立事实核验步骤。

## 6. 排版渲染：`render_mpnews.py`

渲染输入是最终日报对象，主要字段包括：

```text
日期：date, date_label
正文：lead, items
辅助：gloss, cut, coverage, warnings, profile, model
```

每条 `items` 包含选题产生的 `id`、`ids`、`who`、`group`、`handle`、`kind_label`、`ev`、`url`、`top`，以及写作产生的 `title`、`text`、`you`、`extra`、`quote`。

| 版式 | 实现规则 |
|---|---|
| 来源顺序 | 先按是否头条稳定排序，再按 `group` 首次出现顺序分组；头条所在组优先 |
| 来源卡片 | 首字色块头像、来源名、账号和条目数，配同色左边线 |
| 色彩 | 来源色从 `AVATARS` 循环取用；标题统一使用绿色系 |
| 多条同来源 | 后续条目增加虚线分隔和较小的来源栏 |
| 标题与正文 | 标题下有短横线；正文一段展示，使用内联样式 |
| 头条 | 单独显示“头条”文字，不另建独立头条章节 |
| `==重点==` | 转为粗体、深色文字和浅绿色背景 |
| `[[术语]]` | 转为虚线下划线，释义放在条目注释区 |
| 灰色注释 | 按术语、补充、英文原话、证据类型及原文链接组织 |
| 研究点评 | `you` 非空时显示“和你的研究” |
| 文末 | 显示未选理由和简要覆盖说明 |

`group` 来自来源键，不单纯依赖模型生成的 `who`。同一人物的不同来源可能分成两组；论文统一归组，并在条目元信息中展示可识别的 arXiv ID。

文字先经过 HTML 转义，再转换有限的术语与重点标记，避免模型文字直接成为任意 HTML。

`render_article` 返回 `title`、`author`、`digest`、`content`。文章标题按 UTF-8 截至 128 字节，摘要截至 512 字节，并避免留下不完整的 UTF-8 字符；摘要优先使用头条标题。

`render_page` 将相同文章内容包装成本地 HTML。它是查看与归档副本，不需要额外前端构建。封面由 VPS 发送程序上传 `logo-paper.png` 后添加，不在渲染阶段生成。

## 7. 推送与失败通知：`wecom/`

### 传输边界

Ubuntu 的 `deliver` 将以下一种 JSON 写入 SSH 标准输入：

```json
{"mpnews": {"title": "标题", "author": "作者", "digest": "摘要", "content": "HTML"}}
```

或：

```json
{"text": "失败通知"}
```

目标由 `WECOM_RELAY` 指定，私钥由 `WECOM_RELAY_KEY` 指定。SSH 使用 `IdentitiesOnly=yes` 和 `BatchMode=yes`，超时 180 秒。退出码非零，或 stdout 中没有 `sent`，均视为推送失败。

VPS 的 `authorized_keys` 将专用公钥限制为运行 `wecom/receive.py`，并带 `restrict`。这样 Ubuntu 只负责提交消息内容，企业微信 Secret 保留在可信 IP 所在 VPS。

### 接收与发送

`receive.py` 使用固定目录 `~/x-daily-wecom/`：

| 检查或动作 | 行为 |
|---|---|
| 输入长度 | 最多接受 `2_000_000` 字节，超出拒绝 |
| 图文校验 | `title`、`content` 必须为非空字符串 |
| 图文留存 | 写入 `inbox/` 下按时间戳命名的 JSON |
| 封面 | 固定使用同目录的 `logo-paper.png` |
| 文本通知 | 非空字符串，截到 1000 个字符 |
| 子进程 | 调用同目录 `send.py`，超时 120 秒，并返回其结果 |

`send.py` 直接读取 VPS 的 `.env`：`WECOM_CORP_ID`、`WECOM_SECRET`、`WECOM_AGENT_ID`。它获取 access token，必要时上传图片，然后调用企业微信消息接口，将消息发给应用的 `@all`。每个 HTTP 请求超时 60 秒，并检查返回的 `errcode`。

发送脚本另支持 `--text`、`--image` 和 `--mpnews`，图文通过 `--thumb` 指定封面。日常接收链路使用图文或文本两种形式。

`verify_callback.py` 只用于首次配置回调验证：校验签名，用 `openssl` 解密 `echostr`，并在配置了企业 ID 时核对接收者。它读取 `WECOM_TOKEN`、`WECOM_AES_KEY` 和 `WECOM_CORP_ID`，默认端口为 8088。完成配置后停止；日常推送不依赖它。

### 失败通知的覆盖范围

`digest.py` 在 `build` 抛出异常且带有 `--send` 时，会尝试发送“没有生成”的文本通知，然后返回失败状态。

通知不是所有错误的统一兜底：

- `radar.py` 全部来源失败会返回非零，使 systemd 不再执行后面的生成命令，没有单独通知。
- `Steps` 内调用两次失败会进入生成失败通知路径。
- 内容警告一般继续生成和发送，不触发失败通知。
- HTML 渲染、输出文件写入和正式图文推送位于生成异常捕获之外，没有第二条通知保证。
- SSH 或 VPS 本身不可用时，失败通知也可能发不出去。

推送没有发送记录去重、自动补发队列或多通道告警。重复 `--send` 会再次发送，失败后应先确认企业微信是否已经收到，再决定重跑。

## 8. 调度与运维

### systemd user 单元

| 单元 | 关键设置 | 含义 |
|---|---|---|
| `x-daily-rsshub.service` | `Restart=on-failure`、`RestartSec=5min` | 维持本地 RSSHub 运行 |
| `x-daily-rsshub.service` | `After=mihomo.service` | 表达启动顺序；代理仍需单独部署 |
| `x-daily-report.service` | `Wants`、`After` RSSHub 服务 | 日报运行前拉起 RSSHub 并排序 |
| `x-daily-report.service` | `ExecStartPre=/bin/sleep 20` | 为 RSSHub 留出启动时间，不是健康检查 |
| `x-daily-report.service` | 两条顺序 `ExecStart` | 先采集，再执行 `digest.py --send` |
| `x-daily-report.service` | `TimeoutStartSec=3h` | 限制整个日报服务启动过程 |
| `x-daily-report.timer` | `OnCalendar=*-*-* 08:00:00 Asia/Shanghai` | 每天北京时间 08:00 启动 |
| `x-daily-report.timer` | `Persistent=true` | 恢复后补触发一次错过的定时任务 |

项目路径模板为 `%h/projects/X_Daily`，Python 使用项目 `.venv`。RSSHub 服务配置了额外的 Node.js 搜索路径。部署时必须根据本机位置调整，并替换日报服务中的 VPS 占位值。

`Persistent=true` 不是历史补采机制。程序仍按执行时的昨天计算日期；多天停机后不会自动逐日重建。退出登录后持续运行需要配置 user lingering。

### 配置加载与超时

`radar.load_dotenv` 使用 `setdefault`，不覆盖已存在环境变量，也不展开路径中的 shell 表达式。`start_rsshub.py` 会加载 `.env`；正常的 `radar.py` 采集入口不会自动加载；`digest.py` 加载时又晚于画像和模型常量初始化。

因此服务中的代理、非默认 RSSHub 地址、画像和模型选择应通过进程环境显式传入。模板中的 `WECOM_RELAY` 也会优先于 `.env`，部署时不能保留占位值。

`CODEX_TIMEOUT=60*60` 为每一次 Codex 调用的超时，不是整份日报的时限。单阶段可能尝试两次，多个阶段累计后仍可能达到 systemd 的 3 小时总上限。被 systemd 超时终止时，没有专用的失败通知逻辑。

### 日志与定位顺序

| 位置 | 用途 |
|---|---|
| `reports/YYYY-MM-DD.md` | 查看窗口、候选、来源失败和不完整原因 |
| `data/YYYY-MM-DD.json` | 查看实际材料、来源状态和 `x_metrics` |
| `data/digest-work/YYYY-MM-DD/` | 定位模型返回、提示词、重写反馈及调用失败 |
| `data/YYYY-MM-DD.digest.json` | 查看成品结构、实际使用术语和 `warnings` |
| systemd user journal | 查看采集退出、生成警告、发送和服务超时 |
| VPS `~/x-daily-wecom/inbox/` | 检查图文是否已到达接收端 |

RSSHub 启动脚本设置 `NO_LOGFILES=true`，服务输出仍可从 journal 查看。日志和提示词可能含个人画像及原始材料，不能直接当作可公开附件。

### 手动重跑

保留采集结果，只补齐缺失的生成阶段：

```bash
.venv/bin/python digest.py --date 2026-11-06
```

重新采集，再清空该日期的阶段 JSON 重新生成：

```bash
.venv/bin/python radar.py --date 2026-11-06
.venv/bin/python digest.py --date 2026-11-06 --fresh
```

确认本地 HTML 后，运行相同日期的 `digest.py --send` 即可复用缓存并发送。`--fresh` 不清理 `state/seen.json`。同日期重跑不因历史状态丢掉候选，但补跑旧日期可能改变后续跨日报去重的首次日期记录。

这些步骤要求环境变量已在 Python 启动前配置。手动命令与正在运行的定时服务应避免并发操作同一日期的数据；代码没有额外文件锁或原子更新保护。

### 测试与本次核对范围

`tests/test_radar.py` 使用模拟网络和时钟检查窗口、覆盖状态、X 分页、节奏、预算、退避、熔断、thread、去重和采集报告。`tests/test_digest.py` 使用模拟 Codex 检查正文校验、重写反馈、重点回填、术语保存、画像注入、模型分配和缓存。

测试会创建临时文件，不需要网络、RSSHub 或 Codex 登录。

部署后可使用示例画像运行测试：

```bash
DIGEST_PROFILE=example .venv/bin/python -m unittest discover -s tests
```

Windows 的 `pull_reports.ps1` 是可选归档同步工具，通过 `scp` 复制 `reports/` 与 `data/`，失败写入 `pull.log` 后退出，等待下次运行补齐。脚本不创建计划任务，也不参与 Ubuntu 上的生成和推送。

## 9. 已知限制与后续方向

| 限制 | 当前影响 |
|---|---|
| X 是非官方接口 | Cookie 可能失效，来源可能限流、漏帖或返回旧数据 |
| X 深度有限 | 最多请求扩展时间线，thread 由启发式触发；付费内容和他人回复不覆盖 |
| 摘要截断 | 日报无法反映材料中未采集或已截掉的细节 |
| 论文时间不同 | 上榜、提交和公开时间不完全对应每日统计窗口 |
| 选题软约束 | 论文篇数、来源比例和方向比例主要由提示词约束 |
| 校验有限 | 数字记号匹配不等于事实正确，释义也没有独立核验 |
| 仍可能带警告推送 | 正文重写后残留问题未统一阻断 |
| 缓存不感知输入变化 | 修改配置或候选后需要主动 `--fresh` |
| 运维恢复有限 | 无自动逐日补采、推送去重、通用失败通知或自动补发队列 |
| 外部版本未锁定 | RSSHub 补丁与 Codex CLI 参数兼容性需部署者检查 |

目前还有一条独立的 `radar.py --llm` 路径：最多将前 60 条候选发给 OpenAI 兼容 API，并输出到 Markdown 采集报告；API 缺配置或调用失败时回退规则清单。它不使用这里的画像、四阶段提示词和成品 HTML 流水线，正常定时部署也不启用它。

可继续推进的方向包括：

- 基于通用读者画像准备公众号版，利用来源推荐中的 `public` 分类扩大覆盖。现有推送只实现企业微信应用，尚无公众号发布链路。
- 通过 `find_sources.py` 补充更多研究者、实验室、博客及更具体的论文查询，审核后加入来源配置。
- 将论文上限、关键数字和残留警告升级为更明确的代码约束，并为缓存增加输入变化识别。

这些是后续方向，不应在对外介绍中表述为已经实现的功能。
