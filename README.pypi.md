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

metriclens init  --project <dbt 项目目录>   # 生成 metriclens.yml 模板
metriclens graph --project DIR              # 建血缘图(需先 dbt compile + docs generate)
metriclens synth --project DIR              # 合成口径卡(需配置 LLM 端点)
metriclens drift --project DIR              # 漂移检测
```

`fineprint` 与 `metriclens` 两个命令等价;import 名为 `metriclens`。
LLM 端点经环境变量或项目根 `.env` 配置(`METRICLENS_LLM_BASE` /
`METRICLENS_LLM_KEY` / `METRICLENS_LLM_MODEL`),兼容 OpenAI 风格 API。

无 catalog 也能跑(列 schema 由 yml 声明 + 编译 SQL 拓扑推断补全);能执行
`dbt docs generate` 时仍建议补上,实测列集更可靠。

License: Apache-2.0
