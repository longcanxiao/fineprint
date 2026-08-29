# v0.7 设计:relation identity 切换到 unique_id 三段式

状态:设计定稿,未实施。前置讨论见 CHANGELOG 0.6.x 各身份修复项。

## 1. 问题与身份模型

三层名字,基数各异:

| 名字 | → 模型 | 保证方 |
|---|---|---|
| 短名 `orders` | 一对多(跨包同名合法) | 无 ← 现状主键,唯一的折叠点 |
| `model.<package>.<name>`(unique_id) | 一对一 | dbt 主键 |
| `database.schema.alias`(物理名) | 一对一(enabled 节点) | dbt 编译期 AmbiguousAlias 检查 |

现状的所有身份缺陷都源于用短名当主键、用两段 `schema.table` 当反查键:

- 跨(内部)包同名模型 → 折叠冲突,只能显式拒绝(0.6.1);
- 多 database 项目同 `schema.table` → 折叠冲突,只能显式拒绝(0.6.0);
- 物理名当身份 → 环境切换(dev/prod schema 前缀)、alias 改名都表现为"指标删旧建新",血缘断代。

目标身份模型:

- **主键 = 逻辑身份** unique_id(`model.<pkg>.<name>` / `source.<pkg>.<src>.<tbl>` /
  seed/snapshot 同理)。跨环境稳定,配置变更不换身份。
- **反查键 = 物理身份** `database.schema.alias` 三段(catalog 事实)。SQL 里写的是
  物理名,物理→逻辑的 relations 反查表永远需要;升三段后多 database 折叠自然解除。
  database 可为空串,键规范化为 `db.schema.table`(空段保留点位,与
  fingerprint_of 的 `.orders.amount` 先例一致)。
- **短名 = 纯 UI**。输入(config target/CLI/API)时解析,输出(卡片/看板/报告)时展示;
  歧义必须显式报错并列出候选限定写法,绝不静默选一个。

不变式(防御性校验,不信任外部 artifacts):enabled 节点的物理三段名全局唯一。
dbt 已保证,我们仍在装载期复查——manifest 是外部输入。

## 2. 数据结构变更

- `project.models`:键 unique_id;新增 `project.resolve_model(ref) -> uid`——
  输入短名(唯一才自动解析)、`pkg.name` 二段、或完整 unique_id;歧义/未知时
  报错列出全部候选写法。`external_models` 键升三段。
- `graph.models`:键 unique_id;条目内保留 `name`(短名)与 `table`(三段物理名)。
- `graph.relations.models/sources/external`:键升三段;models 值为 unique_id。
  `metriclens_graph_version` → 3。
- trace 输出:`expr_chain[].model` 保留短名(展示),新增 `model_uid`;
  `sources[]` 新增 `database` 字段(schema/table 不动)。
- 显示规则:短名无歧义 → 短名;有歧义 → `pkg:name`。集中在一个
  `display_name(uid, graph)` 里,全部消费方共用。

## 3. 输入层(config/CLI/API)

- `TARGET_RE` 放宽为 2~3 段:`model.column`(短名)或 `package.model.column`(消歧)。
  解析顺序:先按二段试 resolve;短名歧义 → 报错要求三段写法。
- CLI `trace`/API `/trace/{model}/{column}`:model 位接受短名或 uid,内部走
  resolve;响应体带 uid。看板展示短名。
- benchmark golden set 维持短名(经 resolve 层,零改动)。

## 4. 兼容与迁移(双轨)

- **图**:v3 读取器遇 v2 图直接报错"请重建图"。图是派生物、重建零成本,
  不做双读——真正需要兼容的是有基线语义的持久物。
- **漂移快照**:新快照 `sources_full` 升三段(`db.schema.table.column`),
  新增 `model_uids`;diff 沿用 sources_full→sources 的既有回退模式——
  双方都有新键才用新键比,否则退旧键。老基线不误报;跨 database 改指向
  自新基线起可检出。
- **口径卡**:新卡 additive 加 uid 字段,老卡不动。
- **治理指纹**:源字段串升三段。governance_report 每次重算、无基线,无迁移问题。

## 5. 实施顺序(小步提交,每步全绿)

1. **探针先行**:验证 sqlglot `qualify` 传三层 schema dict
   (`{db: {schema: {table: cols}}}`)后,`table_key()` 能稳定拿到 catalog 段
   (SQL 原文常只写 `schema.table`,db 段须由 qualify 注入)。这是唯一的
   实现期技术风险点,先证后动。
2. project.py:models 换主键 + resolve_model + 物理唯一性防御校验;
   0.6.0/0.6.1 的两处"折叠冲突拒绝"改为合法支持。
3. lineage.build_graph:relations 三段、models 键 uid、graph_version 3。
4. trace/synth/governance/drift 跟随换键;display_name 落地。
5. config 2~3 段解析 + 歧义错误信息;CLI/server/dashboard 接 resolve。
6. 测试:两内部包同名共存 + 短名歧义报错、三段反查、多 database 项目合法化、
   漂移新旧基线兼容、卡片 uid 字段。回归:demo 图除键形态外零语义漂移
   (upstreams 序不敏感比对,同 f451ff1 验证法)。
7. 文档/CHANGELOG,版本 0.7.0;changelog 注明:漂移基线建议重建,
   老基线经回退键不误报。

## 6. 顺带解锁与明确不做

解锁:

- 多 database 项目(0.6.0 起显式拒绝)转为支持;
- 两个 internal_packages 同名模型从"拒绝"变"共存 + 引用时消歧";
- 非 dbt 适配器的身份接口就位:平台适配器给任务一个 uid(任务 ID)+
  物理产出表,即可复用全部引擎。

本次明确不做:

- **一任务多表 / 多任务写一表**(任务式平台形态):v0.7 仍保持
  一 uid ↔ 一物理表的 dbt 形态;多对多留给适配器层版本,身份模型已为其留位
  (uid → relations 改一对多是局部扩展)。
- join 分组上下文进通道一、dbt unique/relationships 测试作为基数证据
  豁免 sql_quality:独立路线项,不与本重构捆绑。
