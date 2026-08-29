# 0.8 双写赛马:确定性公式组合器与权威裁决路径

状态:已实现(赛马期)。本文记录设计决策与首批数据,作为切换权威的裁决依据。

## 背景与动机

六轮外审的全部 P1 都指向同一个根因:**技术口径的作者是 LLM,机器只做校验**。
校验只能拦"说得出错在哪"的错误;LLM 的失败模式是无声且流畅的。GPT v1.0 设计
稿提出反转:确定性编译器作者技术事实,LLM 退居解释与审阅。

用户的关切(拍板前提):复杂场景下纯解析会不会不如 LLM 精准?结论:两种失败
模式不对称——解析器**漏而诚实**(unsupported 可枚举、修一次永久生效),LLM
**编而流畅**(错误无信号、按分布抽样)。但解析器的真实覆盖率未经真实世界校准,
所以**不做硬切换,做双写赛马**:两通道并行产出、逐卡比对、分歧与覆盖率数据
公开,由真实项目裁决权威归属。

## 组合器(metriclens/render.py)

从模型编译 SQL 出发(与建图同一 qualify),按作用域逐层展开目标列表达式:

- **作用域展开**:CTE/子查询别名经 sqlglot scope 解析到定义处投影,递归展开;
  星号在 qualify 期已展开;跨模型边界沿血缘边替换(展开上游模型的边界列)。
- **聚合/窗口 = 组合边界**:含聚合的定义不得内联进另一聚合(SUM(SUM(x)) 非法),
  也不得内联进 join 改变粒度的消费位(值的 grain 会被静默丢失)——以**命名
  子表达式(def)**保留,附定义处 grain 标注(模型边界 def 用图的 grain,
  scope 级 def 解析 group by 含序数);非聚合定义按可读性内联(单列/短表达式
  内联,长表达式命名)。
- **产物形态**:`top`(顶层公式,def 以名字引用)+ `defs[]`(name/model/expr/
  grain/kind/join_context)+ `inline`(全量回代单表达式;回代产生嵌套聚合时置
  None,仅存文本形供比较)。
- **fail-closed 清单**(unsupported/ambiguous,均带机器原因):UNION 分支定义
  不一致;模型自引用(增量回读);标量子查询(保留原文不展开,ambiguous);
  展开深度/def 规模超限;qualify 失败;找不到投影列。组合器**任何**内部错误
  都落 unsupported,绝不中断卡片生成,绝不输出猜测。

## round-trip 自检(两条独立实现互证)

组合公式必须通过**与 LLM 公式完全相同的校验器**:链内词表(verify_freetext)
与聚合锚点(formula_agg_check);且组合器走 scope 展开得到的叶子源集必须与
通道一(sqlglot lineage,独立实现)的 sources 一致(star 表级源只要求表命中)。
任一不符 → status=ambiguous + `rt_failed=true`,发布状态机阻断 VERIFIED。
机器两条实现互相矛盾比单条实现出错更值得人看。

## 赛马比对(race)

LLM 公式规范化(限定名剥到裸列、小写、count(1)/sum(1)→count(*)、剥字面量后
含非 ASCII 判散文)后与组合器各展开形比对(top / inline / 文本回代形 / 纯直通
指标的 def 体——LLM 天然在该粒度写公式):

| verdict | 含义 |
|---|---|
| agree | 规范化后与某一展开形结构一致 |
| consistent | 无机器矛盾但未达结构一致(合法的粒度差异等) |
| prose | LLM 公式不可解析为 SQL,仅 token 级校验兜底 |
| disagree | 机器矛盾实锤(聚合锚点/链内词表失配,与置信降级同源) |
| renderer_unsupported | 组合器覆盖不了——是覆盖率数据,不是分歧 |

多目标指标(如比率跨两列):目标间组合关系由配置/业务声明,非单一 SQL 事实
→ formula 恒 ambiguous,race 至多 consistent。这是诚实而非缺陷。

## 发布状态机(赛马期语义)

```
disagree / key_filters 归因不明 / rt_failed   → REVIEW_REQUIRED
置信 high                                     → VERIFIED
组合公式 proven(但置信 < high)              → TECHNICAL_ONLY(机器事实可用,叙述待审)
其余                                          → REVIEW_REQUIRED
(BLOCKED 保留:硬失败当前直接报错不落卡)
```

**权威在 0.8 不切换**:发布口径仍是 LLM 归并 + 既有置信分级;publication_status
与 race 是并行标注。renderer_unsupported 不降卡(组合器的覆盖缺口不该罚 LLM)。

## 首批数据(demo 数仓,15 卡,批次 e373b673)

- 3 agree / 6 consistent / 4 prose / **2 disagree = 恰好是已知的两张 LLM 错卡**
  (gmv 反连接重构、atv 编造 join)→ REVIEW_REQUIRED;13 高置信卡 → VERIFIED。
- 0 renderer_unsupported;round-trip 15/15 全绿(叶子源集与通道一完全一致)。
- 组合器确定性端出了当年靠 LLM 才发现的 delivered_rate CTE 内 min(sign_time)
  ——连同 [waybill_id] 粒度标注。

## 赛马期进展(按裁决路径逐项落地)

**① 提示词约束(prose 归零)**:MERGE 提示词硬性要求 formula 为可解析 SQL 表达式
片段(不含 SELECT/FROM/JOIN、说明文字归 summary、中文仅限字符串字面量)。demo
重跑(批次 b5f22809):prose 4→0,**8 agree + 7 consistent,零 disagree**;
重掷后的 gmv/atv 公式这次正确(升 high→VERIFIED)——LLM 通道跨运行漂移、组合器
逐运行恒定,本身就是赛马论据。dm_refund_rate 活演状态机:公式 agree 但 caveat
蹭写隔壁指标的"14"天数字被词表拦下 → **TECHNICAL_ONLY**(机器事实可用,叙述待审)。

**② 真实项目探针**(benchmark/probe_real_project.py,零 LLM 零 DB):GitLab
dbt docs 已上 OAuth 且无 Wayback 快照,改用 **Fivetran ad_reporting 公开 docs
产物**(12 个广告平台真实生产包,350 模型 / 6771 列,postgres,codegen 链式
CTE + inline ephemeral + 跨源 UNION rollup)。三轮 probe→fix→re-probe:

| 轮次 | proven | 主要缺口 | 修复 |
|---|---|---|---|
| 1 | 98.0% | 132× 异构 UNION(12 平台分支合法不同定义) | union def:逐分支保留(label+expr),「值=行所属分支的表达式」即可证事实 |
| 2 | 99.8% | 13× 深度护栏(实测真实路径 257 层,非环) | 环改由作用域级 in_progress 显式守卫(递归 CTE),MAX_HOPS 512 纯兜底 |
| 3 | **100.0%**(6771/6771) | 无 | — |

列级血缘错误 0;组合吞吐 ~1200 列/s(建图 sqlglot lineage 83s 为主)。
报告固化于 benchmark/reports/fivetran_ad_reporting_2026-08-29.json。

**②-2 多项目多方言扩展**(报告均固化于 benchmark/reports/):

| 语料 | 方言 | 风格 | 规模 | 建图 | 组合器 |
|---|---|---|---|---|---|
| Fivetran ad_reporting | postgres | codegen 链式 CTE + inline ephemeral + 跨源 UNION | 350 模型 / 6771 列 | 0 列错 | **100.0%** |
| Snowplow web | snowflake | 会话化/窗口/增量/FLATTEN,integration-test 产物 | 52 模型 / 1785 列 | 0 列错 + 1 边界模型(工件自身缺陷,诚实降级) | **100.0%** |
| Cal-ITP warehouse | bigquery | 真实政府数据平台,610 个手写模型(UNNEST/STRUCT/date-spine/PIVOT) | 604 模型 / 16856 列 | 0 列错 | **99.9%**(残余全部具名:defs 规模 6 + 标量子查询 4) |

**通道一被三语料推硬的地方**(全固化回归测试):单模型解析失败降级为边界节点
(血缘/组合器在其物化表截止,与第三方包同语义,graph 门禁计入);qualify 两级
退让(先部分限定,定不了的列逐列诚实报错);manifest 一方声明列回落进 qualify
schema(docs 站 catalog 常缺 sources,catalog 永远优先);裸 UNION 顶层模型经
named_selects 取列(此修复让 Cal-ITP 多识别 127 列)。

**组合器从三语料学会的标准 SQL 语义**(全固化回归测试):BigQuery UNNEST 横向
源与 Snowflake LATERAL FLATTEN / TABLE(FLATTEN)(元素引用=底层数组表达式,与
通道一记账一致;FLATTEN 非 VALUE 伪列诚实拒绝);STRUCT 字段访问(含部分限定下
结构体名被解析成表限定名的形态);链式 UNION 拍平嵌套二叉后**按位对齐**(首分支
命名、后续分支裸字面量——dbt_utils date_spine 惯用法);派生表列别名清单
as t(c1,c2);`t.* EXCEPT(col)` 星号不担保被排除列;多源作用域裸列按担保源 +
USING 最左语义归属。**PIVOT 输出列已确定性展开**:透视列改写为度量聚合参数包
CASE WHEN <FOR 字段>=<值> THEN … END(COUNT(*) → COUNT(CASE … THEN 1 END)),
def 带透视隐式 grain(输入列 − 全部度量引用 − FOR 字段),id 列直通输入作用域;
输出名 ↔ (度量,值) 映射用 sqlglot 自身的值主序 columns 元数据,不自造命名规则
——Cal-ITP 实测:trips_owl := MIN(CASE WHEN time_of_day='owl' THEN n_trips END)
per [key, service_date, route_id, direction_id]。MAX_DEFS 48→200(可读性参数,
真实 date-spine × 结构体模型合法超出)。

demo 语料回归:图语义零漂移(仅已知 sqlglot 栈序波动),门禁 14/14 + 8/8。
GitLab dbt docs 已上 OAuth 且无 Wayback 快照,Cal-ITP(dbt-docs.dds.dot.ca.gov)
是同级替身。三语料合计 **25412 列,总 proven 率 99.96%(25402/25412)**,且每一例非 proven
都有机器可读的具名原因——"漏而诚实"的失败模式经真实世界校准成立。

**③ 看板渲染(已落地)**:口径卡弹窗新增发布状态徽章 + 「机器口径」区(逐事实
status 徽章、组合公式、命名子表达式带 grain/join 上下文/union 分支、输出粒度),
LLM 技术口径明确标注「发布权威(赛马期)」;互验区展示公式赛马判定(disagree 附
机器矛盾详情);治理台新增「疑似重复·基数未证」(row_mismatch)档;批次索引逐卡
携带 publication_status 与 race 判定。

## 切换判据(剩余)

1. 更多真实项目探针(不同方言/写法风格;GitLab 若重新公开仍是首选)。
2. disagree 逐条人工裁决,记录谁对——赛马战绩公开(demo 当前零 disagree,
   历史两例均为 LLM 错)。

## 已知余量(诚实清单)

- def 的 grain 标注是展示层 best-effort,不进判定。
- inline 在嵌套聚合时不可用(数学上无单表达式形),defs 即最终形态。
- race 只比公式:sources 已有互验、key_filters 已有指纹匹配与词表校验,
  不重复立项。
- 标量子查询保留原文,其内部口径不做组合声明(ambiguous + 叶子互证部分跳过)。
