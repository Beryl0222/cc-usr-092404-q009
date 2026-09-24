# 国际义诊候选分配

本项目用于整理国际义诊候选分配领域中的事件名称、交换字段与脱敏样例，并提供一套可解释的分配参考实现：筛查结果、适应证、紧急程度、地区公平目标、名额技能、行程窗口、语言支持、知情授权与后续照护承诺共同参与候选排序，但自动结果只构成建议，正式归属必须经过确认事件。资料只包含领域约定与虚构样例，不包含真实个人信息、生产连接或外部账号。

## 分配流程

1. **筛查与授权**：`SCREENING_REGISTERED` 登记筛查结论与紧急程度；`ELIGIBILITY_SIGNED` 记录适应证、知情授权与后续照护承诺；`SCREENING_CORRECTED` 的更正只影响未出发安排。
2. **可解释评估**：`CANDIDATE_EVALUATED` 记录硬性门槛结果、加权因子得分、政策版本与数据版本；评估只是建议，不直接形成归属。
3. **暂占与确认**：`SLOT_HOLD_PLACED` 的暂占有期限；`ALLOCATION_CONFIRMED` 产生唯一归属，并发确认只保留第一个。
4. **释放与递补**：`SLOT_HOLD_RELEASED`（过期 / 退出 / 材料过期）后由 `WAITLIST_PROMOTED` 按原规则版本递补。
5. **例外审批**：`EXCEPTION_REQUESTED` / `EXCEPTION_APPROVED` 要求批准人与申请人不同（独立复核），并记录对建议名单与公平目标的影响。
6. **行程与诊疗事实**：`ITINERARY_CHANGED` 只触发未出发安排的重算；`DEPARTURE_RECORDED` 与 `TREATMENT_COMPLETED` 之后的事实保持不变。
7. **披露与交接**：`IDENTITY_DISCLOSED` 仅允许面向实际接诊链；`OUTCOME_HANDED_OFF` 等副作用通知携带幂等键，服务重启后重放不会重复。

## 排序门槛与因子

硬性门槛（任一不满足即失去候选资格，避免签证、术后随访、接诊能力最后才发现不成立）：

| 门槛 | 含义 |
| --- | --- |
| `CONSENT` | 知情授权已签署 |
| `INDICATION` | 适应证在本批名额接诊范围 |
| `SKILL` | 名额具备所需技能与当地接诊能力 |
| `TRAVEL_WINDOW` | 签证与行程窗口和任务窗口相交 |
| `FOLLOW_UP` | 术后随访与当地后续照护承诺已落实 |
| `SCREENING_FIT` | 筛查结论适宜出行与手术 |

加权因子（通过门槛后参与排序，权重见 `DEFAULT_POLICY`）：`urgency` 紧急程度、`equity` 地区公平（对照 `equity_targets` 的缺口）、`screening_need` 筛查需求、`language` 语言支持。政策按 `policy_version` 版本化，历史版本可通过 `policy_by_version` 取回。

## 运行不变量

`src/allocation.py` 把事件日志重放成分配状态，并检查以下不变量（违反时记入 `state["problems"]`）：

- 同一患者不能跨合作方重复占位（`DUPLICATE_HOLD`）；
- 同一名额只能确认一个归属（`SLOT_ALREADY_ALLOCATED` / `CONFIRM_WITHOUT_ACTIVE_HOLD`）；
- 递补必须按原规则版本与建议名次（`PROMOTION_ORDER` / `PROMOTION_RULE`）；
- 身份披露不超出实际接诊链（`DISCLOSURE_OUTSIDE_CARE_CHAIN`）；
- 例外批准必须独立复核并记录影响（`EXCEPTION_NEEDS_INDEPENDENT_REVIEW` / `EXCEPTION_IMPACT_MISSING`）；
- 过期释放、候补升级与交接通知由日志派生并携带幂等键，重启重放不重复。

## 审计复算

每条 `CANDIDATE_EVALUATED` 携带 `policy_version`、`data_version` 与复算所需输入（脱敏后的候选与名额快照）。审计人员可用 `policy_by_version` 取回政策并重跑 `evaluate_candidate` 比对因子、总分与数据版本；`tests/test_allocation.py` 中的审计用例对样例时间线逐条复算。

## 目录

- `src/medical_mission_allocation.py`：事件种类与最小字段校验。
- `src/allocation.py`：可解释排序、暂占期限与事件重放的参考实现。
- `data/sample.json`：用于核对资料格式的虚构事件。
- `data/sample_timeline.json`：覆盖完整分配生命周期的虚构事件序列。
- `tests/`：保证样例与领域约定及运行不变量保持一致。

## 测试与构建

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests
```
