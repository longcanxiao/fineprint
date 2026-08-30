# MetricLens(指标透镜)

[English](README.md) | **中文**

**问问你的看板:这个指标到底是什么口径?**

MetricLens 从 dbt 项目里已有的 SQL 出发,反向还原每个看板指标的业务与技术定义(口径)——无须预先注册语义层,无须手工写文档。它回答的是这样的问题:

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
- **确定性公式组合器**(0.8):第三位作者按作用域逐层展开编译 SQL,合成可证明的指标公式——聚合/窗口边界落为命名子表达式并携带定义处粒度,UNION 分支与 PIVOT 输出列确定性展开,组合结果必须通过与 LLM 公式完全相同的词表/聚合锚点校验,且叶子源集与通道一互证(round-trip)。**公式的发布权威是组合器;LLM 负责解释与叙述,仅在组合器不可证时兜底**(多目标组合、标量子查询等——每一次拒绝都带机器可读的具名原因)。已在三方言三份公开语料上校准(Fivetran ad_reporting / postgres,Snowplow web / snowflake,Cal-ITP warehouse / bigquery):**25412 个真实世界列中 25402 个(99.96%)组合成功且自证**,残余全部具名。
- 低置信的卡片进入人工审核队列,不直接发布。批次原子发布——消费者永远不会看到半更新状态。

在口径卡之外,MetricLens 还基于同一套血缘提供两件治理工具:

- **`metriclens drift`**——为每个指标的口径拍快照(源表 / 条件指纹 / 语义点 / 表达式),跨次重建做对比:14 天窗口改成 15 天,会在受影响的指标上精确浮出一条 `high` 漂移事件。
- **`metriclens govern`**——指纹扫描发现跨表重复物化的指标(同源同条件);同指纹不同名的对子交由 LLM 仲裁("计数 vs 比率 → 不同义")。

## 快速开始

```bash
pip install fineprint       # PyPI 包名(import 名为 metriclens;CLI:fineprint / metriclens)
pip install -e .            # 或从源码装,仅核心 CLI
pip install -e ".[demo,dev]"   # + 基准数仓 / 看板 / 测试依赖

cd your-dbt-project
dbt compile && dbt docs generate    # MetricLens 只读 artifacts——不连数据库

metriclens init             # 生成 metriclens.yml——列出你的看板指标(model.column;
                            # 两个包同名模型时写 package.model.column)。项目声明过
                            # dbt exposures 时自动预填注释候选,消费方随之进入
                            # 口径卡(消费方区)、漂移告警定向与治理收敛加权
metriclens graph            # 构建字段级血缘图
metriclens trace mart_orders.refund_rate_14d    # 查看单个指标的 S/F/E 三元组

export METRICLENS_LLM_API_KEY=sk-...            # 任意 OpenAI 兼容端点
export METRICLENS_LLM_MODEL=deepseek-chat       # 或 gpt-4.1-mini 等
metriclens synth            # 合成口径卡(带缓存,批次原子发布)
metriclens report           # 导出自包含的 HTML 口径报告

metriclens drift            # 口径漂移检测(--strict = CI 门禁:high 漂移
                            #   退出码 1,基线与日志不落盘)
metriclens govern           # 重复指标治理报告
```

配置都在 `metriclens.yml`(指标清单、语言 `zh|en`、词典、治理参数)。LLM 凭据只走环境变量(项目根目录的 `.env` 会被读取):`METRICLENS_LLM_BASE_URL / _API_KEY / _MODEL / _FAST_MODEL / _QUALITY_MODEL`。

**第三方 dbt 包**(Fivetran 连接器、共享的供应商模型等)按**数据源边界**处理,与 ODS 表同一约定:不解析其 SQL、文档与内部口径——血缘在其物化表处截止,卡片上带归属包标注。你治理*自己*的代码;他们的是上游基础设施。确属你所有的内部共享包,在 `metriclens.yml` 顶层声明 `internal_packages: [shared_models]` 后重建图即可看穿。

MetricLens 的全部产物都在 `your-dbt-project/.metriclens/` 下——血缘图、口径卡批次(带原子 `active_run` 指针)、快照、漂移日志、治理报告、LLM 缓存。

## 14 陷阱基准

本仓库自带一个完全可复现的基准数仓(`warehouse/`):模拟四域电商业务(90 天,约 110 万订单,固定随机种子),其 dbt 模型埋了 **14 个真实感口径陷阱**——藏在中间层 CASE WHEN 里的 14 天退款窗口、剔除 60 秒秒退的 GMV、binlog 多版本去重、直播延迟归因、SCD2 汇率、跨域重复建设的退款指标等等。每个陷阱都可在数据里验证,基准答案机器可查:

```bash
bash jobs/rebuild.sh        # 模拟数据 → dbt build(28 个测试)→ 陷阱验证 →
                            # 独立手算对账 → 血缘 golden set(目标 P/R 100%)
bash jobs/caliber_refresh.sh   # 合成 + 陷阱揭示评测(当前运行 14/14)
```

我们相信这是第一个针对 *从 SQL 加工链提取指标定义* 的带基准答案的 benchmark——如果你在评估任何"AI 文档"工具,拿它来压测同样合适。

## 状态与范围

现在可用:12 种 adapter 上的 dbt 项目(DuckDB、Snowflake、BigQuery、Postgres、Redshift、Databricks、Spark、Trino、Athena、ClickHouse、SQL Server、MySQL)——schema 来自 `catalog.json`,方言来自 `manifest.json`,不需要连接数仓。未列出的 adapter 会明确报错而不是猜方言。解析覆盖 sqlglot 能 qualify 的范围;dbt 编译后"每模型单条 SELECT"的世界恰好是这个甜区。

尚不支持:非 dbt 加工链(存储过程、脚本拼 SQL、Flink/Spark 代码)、BI 层血缘(看板字段 → 数据集 → SQL)、增量建图、负责人签发工作流。(多 database 项目与跨包同名模型自 0.7.0 起已支持——身份是 dbt `unique_id` + 物理三段名。)路线图见 `docs/`。

### 数据出境与隐私

通道二会把**编译后的模型 SQL、schema.yml 里的列描述、你在 `metriclens.yml` 维护的词典、以及指标上下文(标题、目标列、层名、取数过滤、确定性证据清单、参与归并的此前 LLM 抽取产物)**发送到你配置的 LLM 端点(`METRICLENS_LLM_BASE_URL`)——永远不含数仓数据与凭据。如果 SQL 本身敏感,请指向自托管或 VPC 内端点;通道一(血缘、漂移、指纹扫描)完全离线运行。LLM 响应按内容寻址缓存在项目的 `.metriclens/cache/` 下——请把该目录当作含有你 SQL 的目录对待。第三方 dbt 包的 SQL 与文档完全不会到达 LLM(数据源边界,见上);你自己模型里的 SQL 注释对 LLM 仍是不可信输入。机器校验框住了被提示注入的模型能塞进已发布卡片的内容——逐字引用、证据绑定条款、自由文本上的通道一词表/聚合锚点筛查,失配即降置信——但散文措辞本身仍是 LLM 产物、并未被证明正确,所以来自不可信模型代码的卡片在发布给消费者之前请先人工过目。

一个演示看板(FastAPI + React)在 `server/` + `dashboard/` 下,对着基准数仓渲染口径卡、漂移徽章、治理台与血缘画布。

## 许可证

Apache-2.0
