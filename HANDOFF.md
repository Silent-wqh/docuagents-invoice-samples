# T16 回归交接清单（P6 故障注入）

本文件交接 P6 故障注入套件。数据在样本仓库。运行时修复与评测在主项目。
本文件不含运行时实现，也不含评测口径之外的评分规则。

## 1. 产物与位置

| 产物 | 路径 | 数量 |
| --- | --- | --- |
| 评估封套 | `datasets/docuagents_v1/fault_injection/<case_id>.json` | 50 |
| 中性运行时候选 | `datasets/docuagents_v1/fault_injection/candidates/inj-NNNN.json` | 50 |
| 评估封套 Schema | `schemas/fault_injection.schema.json` | 1 |
| 运行时封套 Schema（逐字复制） | `schemas/extraction_envelope.v1.1.schema.json` | 1 |
| 注入生成器 | `docuagents_gen/inject.py` | 1 |
| 测试 | `tests/test_inject.py` | 1 |

封套保存 `case_id → candidate_id / initial_candidate_file` 的映射。候选文件与候选内的
`document_id`、`page_id` 只含中性编号（`inj-0001` … `inj-0050`）。候选不含类别、故障类型、
期望路由或基文档语义。

Schema 复制记录：来源 `Silent-wqh/DocuAgentS`，远程 `main` 最近触碰该文件的 commit 为
`84ad75e58e39e242072646077eade1f6be4ef1da`。复制文件 `sha256 =
6d1d6610bd1990c177af8e9b528a10e87889ed8792f5684c048b11af0b69160a`，大小 18730 字节。
本地主项目工作树与远程 `main` 的该文件逐字节一致。

## 2. 责任分工

| 角色 | 责任 | 不负责 |
| --- | --- | --- |
| 样本 Agent | 数据、封套、候选、真实文件引用、数量核对 | 修复逻辑、评分、运行路径 |
| 主项目工作流 Agent | 修复、重验、路由、人工复核行为 | 生成注入数据、改基准 hash |
| 主项目评测 Agent | 评分、指标、真实/注入统计分离 | 生成注入数据、改注入定义 |

## 3. 样本仓库固定命令

```bash
# 全量测试（含 slow）
uv run pytest -q

# 注入测试
uv run pytest tests/test_inject.py -q

# 端到端检查：重新生成并逐字节比较
uv run python -m docuagents_gen.generate \
  --out <fresh-dir> --check \
  --reference datasets/docuagents_v1/manifest.json --quiet
```

期望状态：

| 命令 | 期望 |
| --- | --- |
| `uv run pytest -q` | 全部通过，无 skip、无 xfail |
| `uv run pytest tests/test_inject.py -q` | 全部通过 |
| 端到端检查 | exit 0；`files compared: 142/142`；`fixture files compared: 64/64`；`injection files compared: 100/100` |

审计断言：注入文件在主 manifest 内，`artifact_role=generated`，`sha256` 与磁盘一致。
修改任一注入文件后，检查必须非零退出。

## 4. 用例分类

### 4.1 Dev 标量误读（QA13 正向功能门）

文件：`fault_injection/<case_id>.json` 与对应 `candidates/inj-NNNN.json`。
责任人：主项目工作流 Agent。命令：主项目 `uv run pytest -q`（按其仓库约定运行修复流程测试）。

| case_id | 注入 | 期望状态 | 审计断言 |
| --- | --- | --- | --- |
| `da-a1-01__unit_decimal_shift` | 单价 4.25 → 42.50 | 打补丁并重验；不得自动接受 | 修复后单价 4.25；行金额 6 × 4.25 = 25.50 通过 |
| `da-a1-01__line_digit_transpose` | 行金额 25.50 → 52.50 | 打补丁并重验 | 行金额回到 25.50；其他行不变 |
| `da-a1-02__unit_decimal_shift` | 28.20 → 282.00 | 打补丁并重验 | 单价回到 28.20 |
| `da-a1-02__line_digit_transpose` | 84.60 → 48.60 | 打补丁并重验 | 行金额回到 84.60 |
| `da-a3-01__unit_decimal_shift` | 3.10 → 31.00 | 打补丁并重验 | 单价回到 3.10 |
| `da-a3-01__line_digit_transpose` | 6.20 → 6.02 | 打补丁并重验 | 行金额回到 6.20 |

`da-a1-01__unit_decimal_shift` 是明确的 dev PDF 标量误读用例（candidate `inj-0001`）。用它验证
QA13 的正向功能门。`da-a1-01` 的原件为 PDF。

### 4.2 Test 标量误读（封存）

文件同上。责任人：主项目评测 Agent。命令：最终评测运行。
封套字段 `sealed_for_final_evaluation=true`。这类用例只用于最终评测。不得用于开发调参。

覆盖：`da-a1-03`、`da-a1-04`、`da-a1-05`、`da-a1-06`、`da-a2-01`、`da-a3-02`、`da-a4-01`
的 `unit_decimal_shift` 与 `line_digit_transpose`。

期望状态：同 4.1。审计断言：修复值等于封套 `injected.original`；不得自动接受。

### 4.3 结构变化：漏行与重复行

文件：`fault_injection/da-*__missing_row.json` 与 `fault_injection/da-*__duplicate_row.json`。
责任人：主项目工作流 Agent。命令：主项目 `uv run pytest -q`。

| 用例 | 期望状态 | 审计断言 |
| --- | --- | --- |
| 漏一行（10 例） | 默认进人工复核 | 不得只改 subtotal 或 invoice_total 消掉结构错误 |
| 重复一行（10 例） | 默认进人工复核 | 不得自动删除行；两行内容一致是有意设计 |

漏行用例确实少一行。后续行下标、证据路径与 `row_references` 已重建。
重复行用例确实多一行。两行内容一致。禁止去重后当作已修复。

### 4.4 税额置空

文件：`fault_injection/da-*__tax_null.json`。责任人：主项目工作流 Agent。

期望状态：可回看原件找回已印税额，然后重验。不得自动接受。
审计断言：候选 `invoice.tax_amount` 为 null，证据为 `not_found`，`searched_pages` 保留。
封套 `injected.original` 保存原印刷值。`da-a4-01` 的原值为 `"0.00"`：原件确实印了税额。

### 4.5 C 类印刷矛盾重读

文件：`datasets/docuagents_v1/gold/da-c1-01.json`（dev）与 `da-c1-02`、`da-c2-01`、`da-c3-01`
（test）。责任人：主项目工作流 Agent。

期望状态：重读不得把印刷矛盾调平。审计断言：不得用算术结果覆盖印刷值。
本套件不注入 C 类。此用例验证重读边界。

### 4.6 合法重复行不得误删

文件：`datasets/docuagents_v1/boundary_fixtures/`（`da-fx-dup`）。责任人：主项目工作流 Agent。
期望状态：合法重复行保留。审计断言：不得因重复而删除行。

### 4.7 图像补丁只作人工建议

文件：B 类文档与图像夹具。责任人：主项目工作流 Agent。
期望状态：图像页上的诊断文本只形成人工建议，不形成自动补丁。
审计断言：`primary_mode=image` 的证据不得升级为补丁级证据。

### 4.8 运行路径用例（主项目责任）

样本仓库只提供数据、封套与真实文件引用。下列用例由主项目实现与验证：

| 用例 | 责任人 | 期望状态 |
| --- | --- | --- |
| 重复上传同一文档 | 主项目工作流 Agent | 幂等；不产生重复结果 |
| 模型输出格式失败 | 主项目工作流 Agent | 受控失败；不写半成品 |
| 重复修复候选 | 主项目工作流 Agent | 去重；不重复计分 |
| 预算耗尽 | 主项目工作流 Agent | 停止并报告；不伪造结果 |

## 5. 运行输入边界（QA28）

模型请求只含两项：

1. 中性候选 `fault_injection/candidates/inj-NNNN.json`；
2. 被测 `prepare` 模块从真实原件取得的 `PageContent`。

下列内容不进模型请求：`gold`、`text`、`bases`、`matrix`、评估封套、故障名称、期望路由。
审计断言：请求序列化中不得出现 `case_id`、`base_doc_id`、`fault_type`、`original`、
`expected_behavior`。

## 6. 统计口径

真实修复率与注入回归分开统计。

- 真实修复率的分母可为 N/A。注入用例不填补真实指标。
- 注入回归只报告：修复成功、重验通过、路由正确、结构错误未被调平。
- 报告必须分别列出两个口径。不得合并为一个比率。

## 7. 数量与隔离

- 50 case = 10 份 A 类 gold × 5 种故障。dev 15，test 35。
- 50 case 不等于 50 份新发票。注入 case 不计入主集 30 份分母。
- 注入 case 继承基文档的 `group_id` 与 `split`。不跨 split。不新增 vendor 或 source family。
- 原件与 gold 的 `sha256` 在注入前后不变。

## 8. Gate 2 待确认事项

1. `expected_behavior` 的四键为 `repairable`、`expected_route`、`requires_human_review`、
   `must_not_auto_accept`。键名按设计 §8.7 边界推导。请 Project Owner 确认。
2. 样本计划 §7 P6 示例的 `initial_candidate_file` 用 case_id 命名。任务页 AC 要求候选
   文件名不含故障类型。本套件按任务页 AC 执行：候选用中性名，映射写入封套。
3. 任务页允许范围未列 `generate.py` 与 `manifest.json`。为满足 AC7（注入产物写入
   manifest 并被 `--check` 覆盖），本任务做最小集成与受控再冻结。请确认。
