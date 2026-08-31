# FinePrint(指标透镜)

[English](README.md) | **中文**

**Read the fine print of your metrics——读懂指标的小字条款;A decompiler for your dashboards——给看板的反编译器。**

问问你的看板:这个指标到底是什么口径?

FinePrint 从 dbt 项目里已有的 SQL 出发,反向还原每个看板指标的业务与技术定义(口径)——无须预先注册语义层,无须手工写文档。它回答的是这样的问题:

> *"这个退款率是无限期计退款,还是只算支付后 14 天内的?"*
> *"这里的 GMV 剔没剔秒退?测试账号是在哪一层被排除的?"*

它像一位仔细的分析师那样通读你的多层加工链,然后**证明**自己的答案。

## 工作原理

两条独立通道,机器互验:

```
                      ┌─ 通道一:确定性字段级血缘 ──────────────────┐
dbt artifacts ──────► │  源表 / 过滤条件 / 表达式链(sqlglot)       │ ──┐
(manifest, catalog,   └─────────────────────────────────────────────┘   ├─► 双通道互验
 编译 SQL)            ┌─ 通道二:LLM 逐模型阅读 SQL ────────────────┐  │   → 置信分级
                ────► │  逐字引用,机器回查原文核验                  │ ──┘   → 证据绑定的
                      └─────────────────────────────────────────────┘        口径卡
```

- **通道一**把每个指标列一路回溯到源表:源字段、塑造行集的每一个过滤(WHERE / JOIN ON / QUALIFY / HAVING,带作用域分析)、窗口去重惯用法、CASE WHEN 归因、COALESCE 兜底、统计日归属——全部带文件/行号锚点。
- **通道二**让 LLM 独立阅读每个模型的 SQL。它主张的每一条过滤都必须携带**逐字引用**,由机器回查原文核验——编造引文在结构上不可能成立。
- 两条通道逐条件做指纹匹配,只有互验通过的过滤才进入归并后的技术口径。业务条款必须引用带编号的确定性证据(`E`/`S`/`X`/`Q`);条款未绑定证据——或条款列表为空——都会封顶卡片置信。(一句话定义与告诫是 LLM 基于证据写的散文,本身不做机器证明。)
- **确定性公式组合器**(0.8):第三位作者按作用域逐层展开编译 SQL,合成可证明的指标公式——聚合/窗口边界落为命名子表达式并携带定义处粒度,UNION 分支与 PIVOT 输出列确定性展开,组合结果必须通过与 LLM 公式完全相同的词表/聚合锚点校验,且叶子源集与通道一互证(round-trip)。**公式的发布权威是组合器;LLM 负责解释与叙述,仅在组合器不可证时兜底**(多目标组合、标量子查询等——每一次拒绝都带机器可读的具名原因)。已在三方言五份公开语料上校准(Fivetran ad_reporting / postgres,Snowplow web / snowflake,Cal-ITP warehouse / bigquery,Mattermost analytics 与 snowflake-dbt / snowflake——后三者为真实生产仓):**34,499 个真实世界列中 34,405 个(99.7%)组合成功且自证**,残余全部具名。
- 低置信的卡片进入人工审核队列,不直接发布。批次原子发布——消费者永远不会看到半更新状态。

在口径卡之外,同一套血缘还支撑漂移检测:

- **`fineprint drift`**——为每个指标的口径拍快照(源表 / 条件指纹 / 语义点 / 表达式),跨次重建做对比:14 天窗口改成 15 天,会在受影响的指标上精确浮出一条 `high` 漂移事件。

**Roadmap**(本仓库已有原型,不随 PyPI 发行版打包;详见 [docs/stability.md](docs/stability.md)):

- **2.0——`fineprint govern`** 重复指标治理:指纹扫描发现跨表重复物化的指标(同源同条件),同指纹不同名的对子交由 LLM 仲裁("计数 vs 比率 → 不同义")。
- **2.0——dbt exposures 集成**:自动发现指标候选预填进 `fineprint.yml`,看板消费方标注到口径卡、漂移告警定向与治理收敛加权。
- 非 dbt SQL 管道(见「现状与边界」)。

## 快速开始

```bash
# Python 3.10+
pip install fineprint       # PyPI 包名,import 名与 CLI 统一为 fineprint
# 想先上手体验?examples/quickstart/ 是 10 分钟完整走读:内置 dbt 编译产物,
# 不装 dbt、不连数据库即可体验 graph/trace/synth/drift 全流程
pip install -e .            # 或从源码装,仅核心 CLI
pip install -e ".[demo,dev]"   # + 基准数仓 / 看板 / 测试依赖

cd your-dbt-project
dbt compile && dbt docs generate    # FinePrint 只读 artifacts——不连数据库

fineprint init             # 生成 fineprint.yml——列出你的看板指标(model.column;
                            # 两个包同名模型时写 package.model.column)
fineprint graph            # 构建字段级血缘图
fineprint trace mart_orders.refund_rate_14d    # 单个指标的口径树(--full 附出处明细)

export FINEPRINT_LLM_API_KEY=sk-...            # 任意 OpenAI 兼容端点
export FINEPRINT_LLM_MODEL=deepseek-chat       # 或 gpt-4.1-mini 等
fineprint synth            # 合成口径卡(带缓存,批次原子发布)
fineprint report           # 导出自包含的 HTML 口径报告

fineprint drift            # 口径漂移检测(--strict = CI 门禁:high 漂移
                            #   退出码 1,基线与日志不落盘)
```

notebook / BI 插件 / 编排集成走 Python API(0.9 起的最小公开面,详见
[docs/stability.md](docs/stability.md)):

```python
import fineprint
fineprint.build_graph("path/to/dbt_project")             # 血缘图(零 LLM)
print(fineprint.trace("path/to/dbt_project", "dm.gmv"))  # 口径树(零 LLM)
batch = fineprint.cards("path/to/dbt_project")           # 已发布口径卡批次
batch["gmv"]["technical_facts"]["formula"]               # 卡片 JSON 即契约(schema_version 冻结)
```

配置都在 `fineprint.yml`(指标清单、语言 `zh|en`、词典)。`language` 同时驱动卡片内容与 CLI 自身输出(`FINEPRINT_LANG` 可覆盖)。LLM 凭据只走环境变量(项目根目录的 `.env` 会被读取):`FINEPRINT_LLM_BASE_URL / _API_KEY / _MODEL / _FAST_MODEL / _QUALITY_MODEL`,调优项 `_CONCURRENCY / _TIMEOUT / _RETRIES`。

### 从 ≤0.8.3(`metriclens`)升级

0.8.4 一次切净统一为 `fineprint`——PyPI 包名不变,无兼容垫片。迁移就是四个改名,格式全部未变:

| 改名前 | 改名后 |
|---|---|
| `metriclens` 命令 · `import metriclens` | `fineprint` · `import fineprint` |
| `metriclens.yml` | `fineprint.yml` —— `mv metriclens.yml fineprint.yml` |
| `.metriclens/` 工作区 | `.fineprint/` —— `mv .metriclens .fineprint` 原样保留 LLM 缓存、口径批次与漂移历史 |
| `METRICLENS_*` 环境变量 / `.env` 键 | `FINEPRINT_*`(值不用变) |

0.8.9 起 CLI 会检测残留——旧配置文件、旧工作区目录、`METRICLENS_*` 键——并直接给出改名命令,不再只报"未找到"。

**第三方 dbt 包**(Fivetran 连接器、共享的供应商模型等)按**数据源边界**处理,与 ODS 表同一约定:不解析其 SQL、文档与内部口径——血缘在其物化表处截止,卡片上带归属包标注。你治理*自己*的代码;他们的是上游基础设施。确属你所有的内部共享包,在 `fineprint.yml` 顶层声明 `internal_packages: [shared_models]` 后重建图即可看穿。

FinePrint 的全部产物都在 `your-dbt-project/.fineprint/` 下——血缘图、口径卡批次(带原子 `active_run` 指针)、快照、漂移日志、LLM 缓存。

## 14 陷阱基准

本仓库自带一个完全可复现的基准数仓(`warehouse/`):模拟四域电商业务(90 天,约 110 万订单,固定随机种子),其 dbt 模型埋了 **14 个真实感口径陷阱**——藏在中间层 CASE WHEN 里的 14 天退款窗口、剔除 60 秒秒退的 GMV、binlog 多版本去重、直播延迟归因、SCD2 汇率、跨域重复建设的退款指标等等。每个陷阱都可在数据里验证,基准答案机器可查:

```bash
bash jobs/rebuild.sh        # 模拟数据 → dbt build(28 个测试)→ 陷阱验证 →
                            # 独立手算对账 → 血缘 golden set(目标 P/R 100%)
bash jobs/caliber_refresh.sh   # 合成 + 陷阱揭示评测(当前运行 14/14)
```

我们相信这是第一个针对 *从 SQL 加工链提取指标定义* 的带基准答案的 benchmark——如果你在评估任何"AI 文档"工具,拿它来压测同样合适。

## 文档

- [架构](docs/architecture.md) — 双通道、公式组合器、发布状态机(英文)
- [准确性](docs/accuracy.md) — 五项目探针(34,499 列)与 14 陷阱金集(英文)
- [隐私与数据边界](docs/privacy.md) — 读什么、发什么、什么永不离开本机(英文)
- [配置参考](docs/configuration.md) — fineprint.yml 全键、环境变量、退出码(英文)
- [Python API](docs/python-api.md) — 最小公开面(0.9 起,英文)
- [已知边界](docs/known-boundaries.md) — 工具明知自己不会什么(英文)
- [稳定性策略](docs/stability.md) — 什么被冻结、何时冻结(英文)

## 状态与范围

现在可用:12 种 adapter 上的 dbt 项目(DuckDB、Snowflake、BigQuery、Postgres、Redshift、Databricks、Spark、Trino、Athena、ClickHouse、SQL Server、MySQL)——schema 来自 `catalog.json`,方言来自 `manifest.json`,不需要连接数仓。未列出的 adapter 会明确报错而不是猜方言。解析覆盖 sqlglot 能 qualify 的范围;dbt 编译后"每模型单条 SELECT"的世界恰好是这个甜区。

尚不支持:非 dbt 加工链(存储过程、脚本拼 SQL、Flink/Spark 代码)、BI 层血缘(看板字段 → 数据集 → SQL)、增量建图、负责人签发工作流。(多 database 项目与跨包同名模型自 0.7.0 起已支持——身份是 dbt `unique_id` + 物理三段名。)路线图见 `docs/`。

### 数据出境与隐私

通道二会把**编译后的模型 SQL、schema.yml 里的列描述、你在 `fineprint.yml` 维护的词典、以及指标上下文(标题、目标列、层名、取数过滤、确定性证据清单、参与归并的此前 LLM 抽取产物)**发送到你配置的 LLM 端点(`FINEPRINT_LLM_BASE_URL`)——永远不含数仓数据与凭据。如果 SQL 本身敏感,请指向自托管或 VPC 内端点;通道一(血缘、漂移、指纹扫描)完全离线运行。LLM 响应按内容寻址缓存在项目的 `.fineprint/cache/` 下——请把该目录当作含有你 SQL 的目录对待。第三方 dbt 包的 SQL 与文档完全不会到达 LLM(数据源边界,见上);你自己模型里的 SQL 注释对 LLM 仍是不可信输入。机器校验框住了被提示注入的模型能塞进已发布卡片的内容——逐字引用、证据绑定条款、自由文本上的通道一词表/聚合锚点筛查,失配即降置信——但散文措辞本身仍是 LLM 产物、并未被证明正确,所以来自不可信模型代码的卡片在发布给消费者之前请先人工过目。

一个演示看板(FastAPI + React)在 `server/` + `dashboard/` 下,对着基准数仓渲染口径卡、漂移徽章、治理台与血缘画布。

## 许可证

Apache-2.0
