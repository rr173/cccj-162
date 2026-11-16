# rowseq — 行序列查询算子（批式）

从**有限事件表**中按**模式定义**提取"一次进入、若干检查、最终通过或退出"式的整段记录。
纯批式数据整备：不维护实时水位、告警或固定设备状态机。核心匹配为自研的迭代回溯
虚拟机实现，**未调用任何现成 MATCH_RECOGNIZE / CEP 引擎**。

```
rowseq/
  pattern.py     模式协议：解析、可空性校验、编译为指令程序、活跃变量分析
  conditions.py  结构化条件：校验、类型系统、三值逻辑求值
  engine.py      匹配引擎：迭代回溯 VM（无 Python 递归）、可靠的状态合并
  exhaustive.py  独立的简单穷举解释器（小表对照用，递归实现）
  api.py         run_query() 查询 API、cross_check() 对照检查
  cli.py         命令行：run / check / demo
  demos.py       演示套件（D1–D11，全部带断言）
tests/test_rowseq.py   单元测试 + 随机模糊对照（250 个随机模式）
examples/              示例模式 JSON + 演示库构建脚本
```

## 数据模型

* 输入是一张有限事件表。按 `partition_key` 分区隔离，分区内按
  `(order_value, row_id)` 构成全序；`row_id` 全局唯一（执行前强制校验，
  重复即报错）。相同 `order_value`（如同一时间戳）的行仍是不同事件，按
  `row_id` 决出先后。
* 每消耗一行**恰好分配给一个变量**；行严格按序消耗，不能跳过不匹配行后
  偷偷拼接。每个起点从该行开始尝试，找不到完整模式才移到下一行。
* 同一变量出现在模式多个位置时，捕获按消耗顺序合并。

## 模式协议（JSON）

```jsonc
{"var": "NAME", "cond": CONDITION}   // 叶子：消耗一行并入 NAME；cond 可省（恒真）
{"seq": [P, ...]}                    // 串接
{"alt": [P, ...]}                    // 备选（左支优先）
{"rep": {"of": P, "min": 0, "max": null, "mode": "greedy"}}
                                     // 有界/无界重复；min 默认 0，max 默认 null（无界），
                                     // mode ∈ greedy | reluctant，默认 greedy
{"seq": []}                          // 空分支（匹配零行）
```

**执行前拒绝的配置**（`PatternError`）：

* 整个模式可匹配空（否则每个位置都会无限地产出空匹配）；
* 任何 `rep` 节点的被重复子式可匹配空（否则非空整体中会出现无消耗重复循环）；
* 有界重复按展开编译，编译产物超过 `max_program_nodes`（默认 100 000 条指令）。

## 条件协议（JSON）

```jsonc
{"const": true}
{"is_null": OPERAND}
{"not": COND}  {"and": [COND, ...]}  {"or": [COND, ...]}
{"cmp": {"op": "eq|ne|lt|le|gt|ge", "left": OPERAND, "right": OPERAND}}

OPERAND:
{"lit":   null|true|false|int64|"text"}   // 字面量
{"field": "col"}                          // 当前行字段
{"first": {"var": "V", "field": "col"}}   // V 首个捕获行的字段
{"last":  {"var": "V", "field": "col"}}   // V 末个捕获行的字段
{"count": {"var": "V"}}                   // V 当前已捕获行数
```

语义：

* 类型为 int64 / text / bool / null。比较要求**同类型**；任一操作数为 null、
  类型不同、对 bool 做有序比较、或值是不受支持的类型（如 REAL/BLOB），
  比较结果都是 UNKNOWN。
* 三值（Kleene）逻辑：条件求值为 true / false / UNKNOWN，**只有 true 通过**。
* 引用尚无捕获的变量（FIRST/LAST/COUNT）结果为 **null**——包括"未来变量"
  （引用合法，只是当前为 null，绝不偷看后面的行）。
* 当前行在条件通过**之后**才加入捕获，因此自引用（`LAST(自身)`、`COUNT(自身)`）
  只能看到之前的捕获。注意 `COUNT` 在变量尚无捕获时按规格为 null，限量写法如
  `{"or": [{"is_null": {"count": {"var": "B"}}},
            {"cmp": {"op": "lt", "left": {"count": {"var": "B"}}, "right": {"lit": 3}}}]}。
* SQLite 没有原生布尔列：库中 0/1 是 int64；bool 类型比较面向 API 侧的行数据。

## 匹配语义

* **优先级由模式语法决定**：备选左支优先；greedy 先尝试继续重复再退出，
  reluctant 先退出再继续；串接按这些局部选择形成深度优先完整匹配顺序，
  **只有能把整个后缀走完的路径才有资格胜出**（不会先选最长前缀再忽略后缀失败）。
  找到某起点的首个完整匹配即返回，不返回所有回溯路径。
* **skip 策略**（每次匹配后下一起点）：
  * `past_last`：终点后一行；
  * `next_row`：起点后一行（可产生重叠匹配）；
  * `to_first:VAR`：VAR 首次捕获行。VAR 未被捕获、或目标不大于当前起点时，
    **本次查询失败并解释**（`QueryError`），不死循环、不擅自改策略。
* 找不到完整模式时总是移到下一行（与 skip 策略无关）。

## 候选状态共享（可靠性说明）

引擎允许合并候选状态（对失败状态做记忆化），但合并键为
`(程序计数器, 行号, 该处活跃变量的捕获)`——只有捕获历史对**所有未来条件**
都等价时才可合并；优先级是结构性的，不依赖捕获。`naive_state_merge=True`
（仅诊断用）演示按"当前模式状态 + 行号"合并会如何丢匹配（见演示 D3）。

## 输出

每个匹配（`Match`）包含：

* `partition`、`start_row_id`、`end_row_id`（首末消耗行）；
* `captures`：各变量按消耗顺序的 row_id 列表（未捕获变量为空列表）；
* `measures`：各变量 `count / first_row_id / last_row_id`；
* `next_start_row_id`：按 skip 策略算出的下一起点（分区耗尽为 null）；
* `rows`：该匹配所用的原行（`include_rows=False` 可关闭）。

## Python API

```python
import sqlite3
from rowseq import run_query, cross_check

conn = sqlite3.connect("examples/demo.db")
res = run_query(
    conn,
    table="events",
    pattern={...},                      # 模式协议 dict
    partition_key="lot", order_value="ts", row_id="id",
    skip="past_last",                   # 或 "next_row" / "to_first:C" / ("to_first","C")
    use_memo=True,                      # 失败状态共享（可靠等价键）
    max_steps=None,                     # 每个起点的步数预算（超出抛 StepLimitError）
)
for m in res.matches:
    print(m.start_row_id, m.end_row_id, m.captures, m.measures, m.next_start_row_id)
print(res.stats)                        # steps / backtracks / max_choice_points / memo_*

report = cross_check(conn, table="events", pattern={...},
                     partition_key="lot", order_value="ts", row_id="id")
assert report["ok"]                     # 引擎 vs 穷举解释器逐项一致
```

异常：`PatternError`（模式配置非法）、`QueryError`（表/列/skip 运行期失败）、
`StepLimitError`（步数预算耗尽）。

## 命令

```bash
cd /workspace

# 构建示例库（质检流：enter / check* / pass|exit，含同刻排序与不完结批次）
python3 examples/build_demo_db.py            # 生成 examples/demo.db

# 运行查询（文本输出）
python3 -m rowseq.cli run --db examples/demo.db --table events \
    --pattern-file examples/pattern_qc_episode.json \
    --partition-key lot --order-value ts --row-id id

# JSON 输出、换 skip 策略、关闭状态共享、限制步数
python3 -m rowseq.cli run --db examples/demo.db --table events \
    --pattern-file examples/pattern_ab_star_c.json \
    --partition-key lot --order-value ts --row-id id \
    --format json --skip next_row --no-memo --max-steps 1000000

# 小表对照：引擎 vs 独立穷举解释器（逐项）
python3 -m rowseq.cli check --db examples/demo.db --table events \
    --pattern-file examples/pattern_qc_episode.json \
    --partition-key lot --order-value ts --row-id id

# 演示套件（D1–D11，全部带断言）
python3 -m rowseq.cli demo

# 测试（单元 + 250 个随机模式的模糊对照）
python3 -m unittest discover -s tests -v
```

## 演示清单（`rowseq/demos.py`）

| # | 内容 |
|---|------|
| D1 | `A B* C`：C 行也满足 B 条件时，greedy 回退留出最后的 C（B=[2,3]） |
| D2 | `(A B)\|(A C)`：左支只有完整成功才优先（pa 走右支、pb 走左支） |
| D3 | 两条历史到达同一(状态,行号)但 LAST(B) 不同，后缀条件只放行一条；引擎不过度合并（naive 合并对照组丢匹配） |
| D4 | reluctant vs greedy 捕获不同（B=[] vs B=[2,3]） |
| D5 | `next_row` 重叠匹配 (1,2)、(2,3)；同名变量捕获按序合并 |
| D6 | `to_first` 正常前进；变量未捕获 / 目标不前进 → 解释性失败 |
| D7 | 空模式、可空重复体 → 执行前拒绝 |
| D8 | 未来变量为 null、自引用 COUNT 限量、NULL 字段三值逻辑 |
| D9 | 分区隔离 + 同刻按 (ts,id) 全序 |
| D10 | 小表穷举解释器按公开优先级列出全部路径，逐项核对捕获与下一起点 |
| D11 | 规模：2 万行长重复（迭代引擎无递归）、晚失败后缀保持线性、病态备选下可靠记忆化把指数搜索压成线性（报告步数/状态数/内存） |

## 设计要点与限制

* **迭代 VM**：模式编译为 `consume / split / jmp / accept` 指令；选择点放显式
  堆栈，捕获用 trail（撤销日志），单个选择点 O(1)。无界重复不会递归到栈溢出，
  内存与最长同时存活路径成线性，不预先展开所有可能路径。
* **失败状态记忆化**：键含活跃变量捕获签名，可靠（见上节）；无捕获依赖的模式
  键退化为 `(pc, 行号)`，病态情形（如 `(B|X)* Z`）从指数降为线性。
* **有界重复按展开编译**，超大上界受 `max_program_nodes` 限制。
* 穷举解释器仅供小表对照（递归、枚举全部路径）。
* 分区键/排序键为 NULL 时遵循 SQLite `ORDER BY` 语义（NULL 排前），同键相邻
  归为一区。
