# fineprint

**Read the fine print of your metrics. A decompiler for your dashboards.**

从 dbt 项目的编译产物(manifest / catalog / compiled SQL)逆向合成每个指标的
完整技术口径——零数据库连接。读懂指标的小字条款,给看板的反编译器。

Reverse-engineers the full technical caliber of every metric from your dbt
artifacts (manifest / catalog / compiled SQL) — zero database connection.

- **字段级血缘图 column-level lineage**:跨模型、进 CTE/子查询/UNION/PIVOT,
  条件与聚合语义一并入图。
- **口径卡 caliber cards**:确定性公式组合器(发布权威)× LLM 解读(解释与
  叙述)双通道互证,每个事实带机器证据;组合器不可证时 LLM 兜底并标注原因。
  在 5 个公开真实项目(34,499 列)上组合器覆盖 99.7%,残余全部具名。
- **口径漂移检测 caliber drift**:图快照逐语义点比对,SQL 改动落到受影响
  指标与条件粒度。

```bash
pip install fineprint

fineprint init  --project <dbt 项目目录>   # 生成 fineprint.yml 模板
fineprint graph --project DIR              # 建血缘图(需先 dbt compile + docs generate)
fineprint synth --project DIR              # 合成口径卡(需配置 LLM 端点)
fineprint drift --project DIR              # 漂移检测
```

命令与 import 名统一为 `fineprint`(0.8.4 起;老项目迁移 = 四个改名:
CLI/import、`metriclens.yml`→`fineprint.yml`、`.metriclens/`→`.fineprint/`、
`METRICLENS_*`→`FINEPRINT_*`,CLI 检测到残留会给出具体改名命令)。
CLI 与卡片双语(zh|en):
`fineprint.yml` 的 `language` 或 `FINEPRINT_LANG` 环境变量控制。
LLM 端点经环境变量或项目根 `.env` 配置(`FINEPRINT_LLM_BASE_URL` /
`FINEPRINT_LLM_API_KEY` / `FINEPRINT_LLM_MODEL`,调优项 `_CONCURRENCY` /
`_TIMEOUT` / `_RETRIES`),兼容 OpenAI 风格 API;
`graph` / `trace` / `drift` 零 LLM 即可用。

无 catalog 也能跑(列 schema 由 yml 声明 + 编译 SQL 拓扑推断补全);能执行
`dbt docs generate` 时仍建议补上,实测列集更可靠。

**Roadmap**(不随本发行版打包):2.0 = 重复指标治理 `fineprint govern`(指纹扫描 +
LLM 仲裁)与 dbt exposures 集成(指标候选预填 + 看板消费方标注);另有非 dbt SQL 管道。

License: Apache-2.0
