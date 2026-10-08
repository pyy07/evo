# 自演进投资系统

## 1. 产品定位

本系统不是一个 ETF 投资系统，也不是一个单纯的量化交易系统。

它的核心是构建一个：

> **能够持续运行、持续观察、持续决策、持续复盘、持续发现能力缺口，并在人工监督下不断扩展自身能力的自演进投资系统。**

系统的最终目标不是“帮助 Agent 执行几次投资操作”，而是形成一个完整的**投资认知与行动闭环**：

```text
市场
  ↓
观察
  ↓
理解
  ↓
形成判断
  ↓
制定投资计划
  ↓
执行
  ↓
承担结果
  ↓
复盘
  ↓
发现错误 / 能力缺口
  ↓
提出改进
  ↓
人工评估
  ↓
系统能力升级
  ↓
下一轮投资
```

随着运行时间增加，系统应该越来越懂自己的能力边界、越来越清楚什么决策有效、什么决策无效，并不断形成新的工具、规则、分析能力和工作流。

---

# 2. 核心理念

系统围绕以下几个原则设计。

### 2.1 投资系统，而不是投资工具集合

系统不是简单把：

- 行情
- 新闻
- 财务数据
- 策略
- 回测
- 交易
- AI Chat

堆在一起。

这些能力必须围绕一个共同的核心：

> **投资决策生命周期。**

所有数据、工具、Agent、交易、复盘和系统演进，都应该能够关联到投资决策。

---

### 2.2 Agent 是使用者，不是系统本身

Agent 是系统的高级使用者。

它可以：

```text
观察市场
↓
调用系统能力
↓
形成投资判断
↓
提出投资行动
↓
请求系统执行
↓
观察结果
↓
复盘
```

但 Agent 不能绕过系统直接：

- 修改数据库
- 修改账户
- 修改持仓
- 修改风险规则
- 修改系统代码
- 绕过审计执行交易

所有行为都必须经过 Investment System。

因此：

> **Agent 驱动系统，但 Agent 不拥有系统。**

---

### 2.3 人类是系统演进的最终监督者

系统允许 Agent 发现自身能力不足，但 Agent 不应该直接修改系统。

例如 Agent 发现：

> “我经常需要判断某类资产的相对强弱，但当前系统没有这个能力。”

Agent 可以创建：

```text
System Change Request

问题：
缺少相对强弱分析能力

发现原因：
连续多次投资决策需要该信息

证据：
最近 20 次 Agent Run 中有 8 次需要该数据

建议：
增加 Relative Strength Analysis Tool

预期收益：
提高趋势判断能力
```

然后：

```text
Agent 提出
   ↓
Human Review
   ↓
Approve
   ↓
Developer Implementation
   ↓
System Capability
   ↓
Agent / Human 都可以使用
```

这才是系统真正的**自演进机制**。

---

# 3. 系统最重要的核心：能力（Capability）

整个系统应该围绕 Capability 构建。

Capability 不只是一个 API。

它代表：

> **系统当前能够为投资者完成什么事情。**

例如：

```text
获取市场行情
获取历史数据
分析趋势
分析资金流
分析基本面
分析新闻
计算风险
构建投资组合
模拟交易
执行交易
计算收益
分析回撤
复盘投资决策
评价投资判断
生成研究报告
```

未来还可能不断增加：

```text
行业轮动分析
市场状态识别
异常行为识别
因子分析
事件影响分析
跨市场分析
投资组合归因
策略自动评价
决策质量评价
```

因此系统应该有一个：

## Capability Registry

记录：

```text
Capability
├── capability_id
├── name
├── description
├── category
├── version
├── status
├── input_schema
├── output_schema
├── permission
├── implementation
├── created_at
└── updated_at
```

Agent 不应该假设系统“应该有什么能力”。

它应该首先知道：

> **系统现在到底有什么能力。**

---

# 4. 自演进的核心闭环

系统应该形成：

```text
使用
 ↓
发现问题
 ↓
判断问题类型
 ↓
提出改进
 ↓
人工评估
 ↓
实现
 ↓
验证
 ↓
能力沉淀
 ↓
再次使用
```

其中必须区分：

### 投资判断错误

例如：

> Agent 判断市场会上涨，但市场下跌。

这属于：

```text
Decision Error
```

应该进入投资复盘。

---

### 数据不足

例如：

> Agent 想分析某个指标，但历史数据缺失。

属于：

```text
Data Gap
```

可能需要增加数据源或数据采集能力。

---

### 系统能力不足

例如：

> 系统根本没有计算这个指标的能力。

属于：

```text
Capability Gap
```

应该产生 Change Request。

---

### Agent 使用错误

例如：

> 系统已经提供某个工具，但 Agent 没有正确使用。

属于：

```text
Tool Usage Error
```

应该优化 Agent，而不是修改系统。

---

这种分类非常重要，因为：

> **不是所有问题都应该通过“修改系统”解决。**

---

# 5. 投资决策是系统的主线

系统中的核心对象不应该是某一种资产，而应该是：

> **Investment Decision**

一次完整投资行为应该形成：

```text
Market Observation
        ↓
Investment Thesis
        ↓
Investment Decision
        ↓
Action
        ↓
Order
        ↓
Trade
        ↓
Position Change
        ↓
Investment Outcome
        ↓
Review
```

例如：

```text
Decision #1024

观察：
市场风险偏好持续下降

判断：
当前环境不适合高风险暴露

假设：
降低风险敞口能够减少组合回撤

行动：
降低组合风险暴露

结果：
未来 5 个交易日组合回撤低于基准

复盘：
判断基本正确

经验：
市场状态识别可能具有价值
```

这些信息最终应该成为系统长期运行的数据资产。

---

# 6. 系统的“成长”来自经验沉淀

系统运行时间越长，应该积累的不只是交易记录。

而是：

```text
市场经验
投资决策经验
策略经验
风险经验
Agent 使用经验
系统能力经验
```

因此建议将 Memory 分成：

### Investment Memory

投资经验：

```text
什么情况下做过什么判断
结果如何
哪些判断经常失败
哪些信号可能有效
```

### Agent Memory

Agent 自身运行经验：

```text
哪些工具经常使用
哪些工具容易误用
哪些任务经常失败
哪些信息经常缺失
```

### System Memory

系统自身的能力认知：

```text
系统有什么能力
系统缺什么能力
哪些能力稳定
哪些能力经常失败
哪些能力正在升级
```

最终形成：

```text
Experience
    ↓
Pattern
    ↓
Knowledge
    ↓
Capability
    ↓
System Evolution
```

---

# 7. 投资对象只是系统的一种应用场景

系统底层应该保持通用：

```text
Investment System
│
├── Market
├── Research
├── Decision
├── Portfolio
├── Risk
├── Execution
├── Accounting
├── Review
├── Agent
├── Memory
└── Evolution
```

至于当前使用：

```text
A股 ETF
```

只是第一阶段 Agent 的投资范围。

它应该通过：

```text
Agent Universe
```

进行约束，而不是把整个系统设计成 ETF 系统。

未来可以出现：

```text
ETF Agent
Stock Agent
Macro Agent
Multi-Asset Agent
Quant Agent
Research Agent
Portfolio Agent
```

但它们共享同一个 Investment System。

---

# 8. 最终产品形态

最终系统不是：

> “一个 AI 帮你炒股的网站。”

而应该更接近：

> **一个不断积累投资经验、不断扩展投资能力、由 Agent 持续运行并由人类监督演进的数字化投资组织。**

其结构可以理解为：

```text
                  Human
                    │
             Supervision / Approval
                    │
                    ▼
┌───────────────────────────────────────┐
│         Self-Evolving Investment      │
│              System                   │
│                                       │
│  ┌─────────┐    ┌─────────┐          │
│  │ Market  │───▶│Research │          │
│  └─────────┘    └────┬────┘          │
│                       ↓                │
│                 ┌──────────┐          │
│                 │ Decision │          │
│                 └────┬─────┘          │
│                      ↓                │
│                 ┌──────────┐          │
│                 │Execution │          │
│                 └────┬─────┘          │
│                      ↓                │
│                 ┌──────────┐          │
│                 │ Outcome  │          │
│                 └────┬─────┘          │
│                      ↓                │
│                 ┌──────────┐          │
│                 │ Review   │          │
│                 └────┬─────┘          │
│                      ↓                │
│              ┌───────────────┐        │
│              │  Evolution    │        │
│              └───────┬───────┘        │
│                      ↓                │
│              New Capabilities         │
│                                       │
└───────────────────────────────────────┘
                    ▲
                    │
                 AI Agent
                    │
          Continuous Operation
```

## 9. 一个最重要的产品判断

开发过程中始终遵循：

> **不要围绕“现在 Agent 要买什么”设计系统，而要围绕“这个系统如何越来越有能力做好投资”设计系统。**

因此：

- ETF 不是核心
- 股票不是核心
- 行情不是核心
- AI Chat 不是核心
- 交易也不是核心
- 回测也不是核心

真正的核心是：

> **观察 → 判断 → 行动 → 结果 → 复盘 → 学习 → 能力升级 → 再次投资**

这条闭环才是整个产品的“主干”。

所有其他模块，都应该回答一个问题：

> **它是否让这个闭环变得更完整、更可靠、更可验证、更可进化？**
