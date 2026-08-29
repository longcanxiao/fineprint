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

## 切换判据(下一步)

1. **真实项目探针**(GitLab dbt 等):量出 renderer_unsupported 率与 disagree
   分布;unsupported 率决定切换节奏(直接上权威位 vs TECHNICAL_ONLY 混合期)。
2. disagree 逐条人工裁决,记录谁对——赛马战绩公开。
3. prose 率(本批 4/15)是 LLM 侧的改进项:提示词要求公式必须为可解析 SQL 片段,
   可把 prose 压向 agree/consistent/disagree,提高比对覆盖。

## 已知余量(诚实清单)

- def 的 grain 标注是展示层 best-effort,不进判定。
- inline 在嵌套聚合时不可用(数学上无单表达式形),defs 即最终形态。
- race 只比公式:sources 已有互验、key_filters 已有指纹匹配与词表校验,
  不重复立项。
- 标量子查询保留原文,其内部口径不做组合声明(ambiguous + 叶子互证部分跳过)。
- 看板尚未渲染 technical_facts/race/publication_status(与 row_mismatch 档同批
  待办)。
