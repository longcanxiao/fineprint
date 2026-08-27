# MetricLens 指标透镜(开发文档)

在数据看板上自动嵌入指标**业务口径**与**技术口径**。M1 数据底座 + M2 业务大盘 + M3 血缘引擎 + M4 口径合成 + M5 治理与漂移已完成,并已通用化重构为 `metriclens` pip 包(任意 dbt 项目可用,见根 README)。

## 结构

```
metriclens/             # 通用包:project.py(dbt artifacts 读取) lineage.py(字段级 DAG+七类结构语义)
│                       #   trace.py(S/F/E 回溯) synth.py(双通道口径合成) llm.py(OpenAI 兼容客户端)
│                       #   store.py(批次原子发布) drift.py(快照+漂移) governance.py(指纹扫描)
│                       #   arbitrate.py(B 档仲裁) config.py(metriclens.yml) prompts.py(zh/en) cli.py report.py
warehouse/
├── simulator/          # 数据模拟器:scenarios.yml(14 道口径陷阱的场景注入配置) + generate.py
├── dbt_project/        # 四域数仓:ODS(9 源表) → DWD(5) → DWM(3) → DM(4) → APP(2),schema.yml 含中文口径元数据
│                       #   metriclens.yml(15 指标配置);.metriclens/(图/口径批次/快照/治理报告,不入库)
├── evaluate/           # validate_traps.py(14 陷阱数据可验证) + handcheck_metrics.py(指标手算对账)
└── metriclens.duckdb   # DuckDB 单文件数仓(由模拟器 + dbt 生成)
benchmark/              # 评测:eval_lineage.py(golden set) manifest_check.py eval_caliber.py(陷阱揭示)
│                       #   eval_governance.py governance_scan_check.py golden_set.yml
server/                 # FastAPI 取数服务(端口 8612;/api/lineage/* 血缘,/api/caliber/* 口径卡)
dashboard/              # React 18 + TS + ECharts 5 业务大盘(端口 5273,/api 代理到 8612)
jobs/                   # rebuild.sh(一键重建+后置漂移检测) caliber_refresh.sh governance_refresh.sh
docs/                   # metriclens-asbuilt.html(M1-M5 demo 期落地方案,历史文档) metric-landscape.html(竞品调研) + 人工复核记录
```

## 快速开始

```bash
# 0) 依赖(首次):uv venv .venv && uv pip install -p .venv/bin/python -e ".[demo,dev]"
#    前端(首次):cd dashboard && npm install
#    LLM 配置:cp .env.example warehouse/dbt_project/.env 并填入(METRICLENS_LLM_*)
bash jobs/rebuild.sh                                             # 原子重建:build 库上全验收通过后才替换正式库
.venv/bin/python -m uvicorn server.main:app --port 8612 &        # 取数 API
cd dashboard && npm run dev                                      # 大盘 http://localhost:5273
```

## 血缘引擎(M3)

```bash
.venv/bin/metriclens graph --project warehouse/dbt_project        # dbt 编译产物 → 字段级血缘图
.venv/bin/metriclens trace --project warehouse/dbt_project app_business_overview_1d.refund_rate_14d
.venv/bin/python -m benchmark.manifest_check                      # 表级骨架三方对拍
.venv/bin/python -m benchmark.eval_lineage                        # golden set 评测
```

回溯输出 S/F/E 三元组:源字段集合(ODS)、过滤条件集(where/having/qualify/join_on 分类,含
「关联即过滤」与纯关联键区分、行集条件闭包判定),表达式链(逐层逐列),以及七类结构语义点
(窗口去重/序号惯用法、CASE WHEN、COALESCE 兜底、统计日归属),全部携带模型文件与编译行号锚定。

## 口径合成(M4)

```bash
bash jobs/caliber_refresh.sh        # 全量刷新 15 张口径卡(LLM 调用,带缓存)+ 陷阱揭示评测
.venv/bin/metriclens synth --project warehouse/dbt_project --only gmv    # 单卡重跑
```

双通道设计:通道一 = M3 确定性血缘(S₁/F₁/E₁);通道二 = DeepSeek 逐跳解析单模型 SQL
(输入独立,不喂通道一结果),每条过滤必须附 SQL 原文引用并过机器校验(幻觉引用结构上无法通过)。
互验规则:源字段集合逐项对齐(join 条件列豁免)、过滤条件经同一归一化管道指纹匹配,
且每条 LLM 过滤按相关性标注(matched/纯关联键/范围外/未解析/可疑)——只有 matched 才进归并,
范围外与可疑内容不得进入卡片(可疑同时惩罚置信)。
一致→高置信,表述差异→中置信标注发布,实质分歧→低置信进人工审核队列;
审核中的卡 API 只返回状态占位,技术/业务内容不对外暴露。
业务口径 = 已互验技术口径 + 编号证据清单 + schema.yml 中文注释 + lexicon.yml 业务词典 → 受控生成:
证据只来自血缘/AST/已验证原文(E 条件、S 语义、X 表达式、Q 引用),每条业务条款必须绑定有效证据 ID,
任一未绑定条款该卡即不得 high;元数据缺失时降级"技术直译 + 待补充业务注释"。
发布原子性:整批卡写入 `caliber/store/runs/<run_id>/`,全部成功后才切换 `active_run` 指针,
API 只读 active 批次——线上不存在半新半旧的中间态(--only 单卡重跑会从 active 批次补齐其余卡再整批发布)。
指纹重复扫描(A 档)结果注入卡片 `governance` 字段,看板口径弹层展示"同源同构"治理提示。
模型:deepseek-v4-flash(逐跳)+ v4-pro(归并/业务)。

## 治理与漂移(M5)

```bash
.venv/bin/metriclens drift --project warehouse/dbt_project             # 口径漂移检测(rebuild.sh 已后置挂载)
.venv/bin/metriclens drift --project warehouse/dbt_project --strict    # 门禁:high 漂移退出 1 且基线/日志不推进
bash jobs/governance_refresh.sh                    # 治理报告:指纹扫描 + B 档 LLM 语义仲裁(有 LLM 调用,带缓存)
.venv/bin/python -m benchmark.eval_governance      # M5 验收
```

三件事:
1. **口径快照与漂移检测**——每次重建后对 15 个指标的确定性回溯结果(源字段集/条件指纹/语义点/表达式链)做快照对比;
   源字段与过滤条件增删、语义点(窗口去重/CASE WHEN/COALESCE/统计日)增删 = high,表达式文本变化 = medium(可能是无害重构)。
   事件进 append-only 日志,看板指标卡挂"口径变更"角标,口径弹层展示逐条变更历史。默认记录不拦截,`--strict` 可作门禁。
2. **重复建设治理**——指纹(ODS 源字段集 + 归一化条件集)扫描 dwm/dm/app 全列:A 档(基名一致)直判重复;
   B 档(同源同条件但列名不同)指纹无法区分,把两列的表达式链交给 LLM 仲裁"重复物化 vs 同源不同指标"
   (如 妥投率 vs 签收运单数 = 比率 vs 计数,判 distinct 不收敛)。报告进看板"治理台"。
3. **血缘画布**——口径弹层内 ODS→DWD→DWM→DM→APP 分层 DAG(指标链路子图,目标模型高亮),API `/api/lineage/graph/{model}/{column}`。

漂移演练已跑通:将 14 天退款窗口临时改为 15 天 → compile → 血缘图重建 → 检测正确命中仅
refund_rate_14d / refund_amt_14d 两个指标(语义点替换 high + 表达式变更 medium),还原后检出对称回归事件;
数据库全程不动。事件流见看板治理台或 `/api/governance/drift`。

## M1/M2 验收状态

- ✅ 90 天数据一键重建(seed 固定可复现;110 万订单/10 万用户/四域)
- ✅ dbt build:14 模型 + 28 测试全通过;最长链路 6 层且必经跨域 join
- ✅ 14 道口径陷阱全部在数据上可验证(`validate_traps.py`,含证据数字)
- ✅ 大盘 14 指标与 DWD 独立手算 SQL 完全一致(`handcheck_metrics.py`,3 个抽样日)
- ✅ M3:任一 APP 指标一键回溯至 ODS;golden set 源字段识别召回/精确 100%、关键条件召回 100%(目标≥95%);manifest 骨架 14 模型三方一致
- ✅ M4:15 张口径卡全部生成(14 看板指标 + 1 张 T7 治理对比卡),置信分布以 active 批次索引为准(`/api/caliber/index`);**14 道陷阱揭示 14/14**(验收线 ≥12);看板口径 ⓘ 点亮,L1 业务口径 + L2 技术口径(条款级证据编号)+ 双通道互验状态 + 治理提示 + 行号溯源引用
- ✅ M5:治理报告分档(粒度/聚合签名后):A 档 4 对真重复直判(T8 靶向对在列)+ 聚合语义直判 4 对不同义 + 同家族不同粒度 12 对单列 + B 档 6 对 LLM 仲裁全部有判据(比率 vs 计数类判不同义);漂移演练(14→15 天窗口往返)精准命中受影响指标;快照与当前图账实一致;看板治理台 + 变更角标 + 血缘画布点亮(`eval_governance` 6/6)

## 质量门禁

```bash
.venv/bin/python -m pytest tests/ -q     # 归一化判等/LLM 校验/空&幻觉引用拒绝(调生产函数)/批次原子性/漂移对比/仲裁校验/API 契约
.venv/bin/ruff check .                   # lint(已清零)
.venv/bin/python -m benchmark.governance_scan_check   # 指纹重复扫描(T8 靶向对自动发现)
.venv/bin/python -m benchmark.eval_governance         # M5 治理与漂移验收
```

已知工程债(记录在案,后续治理):模拟器主函数偏长待拆分;缺 LLM 限流/损坏缓存注入测试、
SCD2 区间重叠 dbt test、前端组件测试;破坏性回归(故意改错数据断言门禁变红)待补。

## 14 道口径陷阱(golden set)

T1 GMV剔秒退 / T2 退款14天窗口+join状态限定 / T3 新客首购口径 / T4 妥投率分母揽收 /
T5 客单价按人 / T6 发货时长剔预售+揽收锚点 / T7 退款率同名不同义 / T8 退款金额两域重复 /
T9 binlog多版本窗口去重 / T10 复购率窗口序号 / T11 直播延迟归因(CASE WHEN) /
T12 SCD2汇率区间join / T13 打款缺失coalesce兜底+分转元 / T14 统计日跨日归属

各陷阱的埋设位置与考察能力见产品方案(Artifact:MetricLens 指标透镜)。

## 备注

- 大盘取数以 APP 层为主;「支付人数/复购率」等周期去重指标辅以 DWM 明细重算,「妥投率/发货时长」周期值从 DWD 物流明细加权——均在 `server/main.py` 中注明。
- 看板配色遵循 dataviz 规范:分类三色位(App=蓝/H5=橙/直播=青)固定映射,浅深双模式均通过 CVD 校验;GMV 与退款率为共享十字线的双面板(不做双轴)。
