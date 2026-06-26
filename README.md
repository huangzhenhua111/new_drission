# new_drission

`new_drission` 是一个基于 DrissionPage 的 Web 自动化脚本生成 Agent。它的目标是把用户的一句自然语言需求，转成一份经过真实浏览器执行、失败诊断、修复验证后的独立 `.py` 脚本。

这个项目对齐面试作业里的 AgentBrowser 三模块要求：

- Generation：理解任务、逐步观察页面、执行并记录成功动作，最后生成 DrissionPage 脚本。
- Debugging：自动运行脚本，失败后收集异常、页面状态、截图和日志，判断是机械脚本问题还是流程状态问题，并修复或回退到 Generation。
- Resilience：为 Generation 和最终脚本提供选择器降级、智能等待、重试、异常上下文、语义上传等通用容错能力。

最终生成的脚本是纯 DrissionPage 代码，只依赖标准库和 `DrissionPage`，不导入本项目代码，也不调用任何 LLM API。

## Quick Start

```bash
cd new_drission
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填入 MULTIMODAL_API_KEY
```

一行命令跑默认示例：

```bash
bash scripts/run_agent.sh
```

默认示例会打开 123Apps 视频编辑器，上传 `manual_files/sample.mp4`，完成剪辑、调速、加标题、调透明度、导出并下载视频。运行结束后，验收成果是：

```text
outputs/demo_123apps/generated_script.py
```

这个脚本就是最终交付物：纯 DrissionPage，可独立重复运行，不依赖 LLM。

导出下载的视频通常会出现在浏览器默认下载目录：

```text
~/Downloads
```

可以用下面的命令查看最近下载的视频：

```bash
bash scripts/show_downloads.sh
```

如果要跑自己的任务，直接在命令里替换网站、资源路径和动作描述：

```bash
ENTRY_URL="https://example.com/" \
RESOURCE="/path/to/file.mp4" \
TASK="打开网站，上传 {RESOURCE}，完成你要做的网页操作，最后点击提交或导出。" \
OUTPUT_DIR="outputs/my_task" \
bash scripts/run_agent.sh
```

自定义任务跑完后，最终脚本在：

```text
outputs/my_task/generated_script.py
```

## 1. 当前能力概览

这版项目不是先让 LLM 一次性编完整网页流程，而是采用“多模态滚动决策”：

```text
自然语言任务
  ↓
TaskParser 生成 task_state.json
  ↓
Generation 循环：
  task_state + 当前 URL + DOM candidates + 截图 + recent_trace
  ↓
多模态 StepDecider 输出单步或低风险 batch 动作
  ↓
Resilience Executor 执行动作、等待、重试、记录 trace
  ↓
StateUpdater 更新 task_state
  ↓
成功则继续，失败/异常/兜圈则进入 Debugger
  ↓
根据成功 trace 生成独立 DrissionPage 脚本
  ↓
自动运行脚本验证；必要时 ScriptDebugger 修复或交回流程 Debugger
```

这样做的原因是：真实网站经常有弹窗、上传进度、异步编辑器、隐藏 input、动态 DOM。提前把路径写死很容易在新网站上崩掉；边看页面边决策更慢一点，但成功率更高。

## 2. 环境要求

- Python 3.11+
- Google Chrome / Chromium
- DrissionPage 4.1.1.4+
- 一个 OpenAI-compatible 的多模态模型接口

当前默认模型：

```env
MULTIMODAL_MODEL=qwen3.7-plus
MULTIMODAL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

项目会优先读取项目根目录下的 `.env`。

## 3. 安装

```bash
cd new_drission
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`.env` 示例：

```env
MULTIMODAL_MODEL=qwen3.7-plus
MULTIMODAL_API_KEY=你的_key
MULTIMODAL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1

# 可选。默认 false，建议真实调试时保持 headed，方便处理登录/验证码/网站异常。
BROWSER_HEADLESS=false
BROWSER_USER_DATA_DIR=.browser_profile

# 可选。文本模型不配置时默认复用多模态模型。
TEXT_MODEL=qwen3.7-plus
TEXT_API_KEY=你的_key
TEXT_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

如果 Chrome UI 太大，可以用系统缩放或 Chrome 自身缩放调整；自动化脚本本身不依赖固定屏幕分辨率。

## 4. 代码结构

```text
app/
  generation/
    task_parser.py       # 自然语言 -> task_state.json
    step_decider.py      # 多模态滚动决策，输出 tool-call 风格动作
    state_updater.py     # 执行后更新全局任务状态
    agent_loop.py        # Generation 主循环

  runtime/
    drission.py          # DrissionPage 真实浏览器运行时
    static.py            # 单元测试用静态运行时

  resilience/
    executor.py          # 动作执行、选择器校验、重试、登录/异常容错

  recovery/
    outcome_checker.py   # 高风险结果、异常语义、兜圈信号检测

  debugger/
    rollback.py          # 流程级 Debugger：refresh / restart / rollback / state 修正

  script/
    recorder.py          # trace -> 独立 DrissionPage 脚本
    runner.py            # 自动运行生成脚本并收集日志/截图
    debugger.py          # 脚本级 Debugger：修脚本或交回流程 Debugger

  trace/
    recorder.py          # 记录动作、状态、截图路径和失败上下文

tests/                   # 三模块的单元测试和策略测试
outputs/                 # 真实任务运行产物
```

## 5. 三个核心模块设计

### 5.1 Generation 模块

入口文件：

- `app/generation/task_parser.py`
- `app/generation/step_decider.py`
- `app/generation/state_updater.py`
- `app/generation/agent_loop.py`
- `app/script/recorder.py`

Generation 做四件事：

1. 把用户需求整理成 `task_state.json`，包括网站入口、资源路径、目标、milestones、当前上下文。
2. 每轮读取当前 DOM candidates、截图、URL、页面文本、最近 trace，而不是提前写死完整点击路径。
3. 让多模态模型输出严格动作 JSON，例如 `goto/click/input/upload/wait/scroll/hotkey/finish`。
4. 执行成功后把动作写入 trace，并由 `recorder.py` 生成独立 DrissionPage 脚本。

动作支持单步和低风险 batch。中高风险动作必须单步，例如上传、导出、登录、删除、支付、页面跳转。低风险 batch 只用于同一面板内的连续小操作，例如“点输入框 -> 输入 50”。

### 5.2 Debugging 模块

入口文件：

- `app/debugger/rollback.py`
- `app/script/debugger.py`
- `app/script/runner.py`

Debug 分两层：

第一层是流程 Debugger。Generation 运行时如果出现应用内部失败、连续兜圈、状态不一致、刷新/重启后状态过期，就把 trace、截图、当前 `task_state` 交给 Debugger。Debugger 会选择：

- 回退到最近可信 URL；
- 刷新当前页面；
- 重启浏览器；
- 修改 `task_state`，把某些已完成动作退回未完成；
- 标记某些步骤必须单步执行；
- 带着恢复指令继续 Generation。

第二层是脚本 Debugger。生成的独立脚本会被自动运行。如果脚本失败，`ScriptDebugger` 会先判断失败类型：

- `mechanical_script_error`：选择器失效、等待不够、输入方式不稳，直接 patch 脚本；
- `missing_prerequisite_state`：前置状态没有真的满足，例如“上传报告成功但时间线没有素材”，交回流程 Debugger；
- `page_internal_failure`：网站自身处理失败，例如编码失败，进入流程恢复；
- `stale_browser_session`：浏览器会话脏了，刷新或重启后恢复；
- `task_unsatisfied_or_ambiguous`：任务本身不可满足或需要人工决策，停止并记录。

这样避免了一个常见问题：如果脚本错在“前面流程状态就不对”，单纯改 selector 没用，必须回到 Generation 重新做对。

### 5.3 Resilience 模块

入口文件：

- `app/resilience/executor.py`
- `app/recovery/outcome_checker.py`
- `app/script/recorder.py` 生成脚本里的 replay helpers

容错能力同时存在于 Agent 执行期和最终脚本中：

- 选择器降级：优先稳定 selector，失败后尝试 text、aria、属性、语义 fallback。
- 禁止模型编造 Playwright selector：执行器只接受当前 DOM candidate 或 Drission/通用 CSS 可执行 selector。
- 智能等待：等待元素可见、页面处理完成、上传进入编辑器，不用纯 `sleep` 硬等。
- 重试和截图：关键操作失败时重试，并保存 failure screenshot/html/state。
- 语义上传：最终脚本不会只依赖录制时的脆弱 CSS 路径，会优先尝试 `input[type=file]`、accept、上下文、Add files/Upload/Choose file 等通用上传语义。
- 语义文本 fallback：当录制的动态 CSS 不稳定时，最终脚本可按可见文本定位 Trim、Opacity、Export、Save 等按钮。
- 异常处理：生成脚本用 `execute_with_retry()` 包住每个动作，失败时输出动作编号、动作类型、selector 列表、最后错误和截图路径。

## 6. 使用方式补充

默认示例也可以用专门的 demo 脚本运行：

```bash
cd new_drission
source .venv/bin/activate

bash scripts/run_123apps_demo.sh manual_files/sample.mp4
```

这条命令会完成：

- 启动真实 Chrome；
- 运行多模态滚动 Generation；
- 记录 trace 和截图；
- 生成最终独立 DrissionPage 脚本。

如果要换视频或输出目录：

```bash
bash scripts/run_123apps_demo.sh /path/to/video.mp4 outputs/my_run
```

### 验收成果：最终独立脚本

运行完成后，最重要的交付物是：

```text
outputs/demo_123apps/generated_script.py
```

这就是最终验收用的脚本：它是独立 DrissionPage 脚本，不依赖 LLM，也不依赖本 Agent 项目代码。

其他辅助产物：

- `outputs/demo_123apps/task_state.json`：滚动任务状态；
- `outputs/demo_123apps/trace.json`：Generation 成功动作记录；
- `outputs/demo_123apps/screenshots/`：执行过程截图；
- `outputs/demo_123apps/replay_run/`：脚本生成阶段的回放辅助目录。

### 验证最终脚本

```bash
python -m app.cli verify-script \
  --script outputs/demo_123apps/generated_script.py \
  --output-dir outputs/demo_123apps/script_verify \
  --timeout-seconds 1200
```

成功时会输出：

```text
Script success: True
Exit code: 0
```

### 录屏演示

如果系统已安装 `ffmpeg`，可以用下面命令录制当前 Linux/WSL 图形桌面：

```bash
bash scripts/record_screen.sh outputs/demo_recording.mp4
```

停止录制时，在录屏命令所在终端按 `q`。如果提示没有安装 `ffmpeg`：

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg
```

### 可选：从已有 trace 重新生成脚本

如果已经有 `trace.json`，可以单独重新生成脚本：

```bash
python -m app.cli generate-script \
  --trace outputs/demo_123apps/trace.json \
  --output outputs/demo_123apps/generated_script.py
```

### 可选：调试失败脚本

```bash
python -m app.cli debug-script \
  --script outputs/real_clideo_no_trim_manual/generated_script.py \
  --task-state outputs/real_clideo_no_trim_manual/task_state.json \
  --output-dir outputs/real_clideo_no_trim_manual/script_debug_auto_resume \
  --max-attempts 3 \
  --timeout-seconds 1200
```

如果脚本 Debugger 判断是流程状态错误，它会自动生成 handoff，上交给流程 Debugger，再恢复 Generation，产出新的独立脚本。

## 7. 端到端示例

### 示例一：123Apps 复杂视频编辑任务

自然语言任务：

```text
打开 123Apps 视频编辑器，上传本地文件 sample.mp4，确认视频素材已经进入时间线；
然后剪掉视频开头前 2 秒，把视频速度调整为 1.5 倍，
添加标题文字“短视频测试”，再把视频不透明度调低一点，最后点击导出按钮。
```

已验证产物：

- trace：`outputs/real_123apps_retest_20260626_175918_fallback_init/trace.json`
- 最终脚本：`outputs/real_123apps_retest_20260626_175918_fallback_init/generated_script.py`
- 脚本验证结果：`outputs/real_123apps_retest_20260626_175918_fallback_init/script_verify_after_text_semantic/script_run.json`
- 成功截图目录：`outputs/real_123apps_retest_20260626_175918_fallback_init/script_verify_after_text_semantic/browser/screenshots/`

验证结果：

```text
Script success: True
Exit code: 0
Elapsed: 87.005s
```

关键回放日志节选：

```text
[04/17] upload sample.mp4 -> selector css:input[type="file"]
[05/17] click Trim -> selector semantic:text=Trim
[06/17] input Trim start seconds -> value 02
[07/17] input Speed -> value 1.5
[10/17] input title -> value 短视频测试
[11/17] click Opacity -> selector semantic:text=Opacity
[12/17] input Opacity -> value 50
[16/17] click Export -> selector semantic:text=Export
[17/17] click Save -> selector semantic:text=Save
Replay finished.
```

覆盖交互类型：

- 页面导航：打开 123Apps；
- 点击：Video Editor、Create Project、Trim、Text、Export；
- 文件上传：上传 `sample.mp4`；
- 文本输入：Trim 起始秒数、Speed、标题文字、Opacity；
- 等待/重试：上传进入编辑器、导出完成、元素可见；
- 下载触发：Save。

这轮也暴露并修复了一个真实鲁棒性问题：Generation 的成功 trace 里保存过部分动态 CSS，但独立脚本回放时 DOM 结构变化导致找不到。现在脚本生成器会把上传和可见文本操作提升为通用语义 fallback，而不是只照抄脆弱路径。

### 示例二：Clideo 视频编辑任务，脚本失败后自动回到 Generation

自然语言任务：

```text
打开 Clideo 视频编辑器，上传 sample.mp4，把速度调整为 1.5 倍，
添加标题文字“短视频测试”，调低不透明度，然后点击 Export。
```

已验证产物：

- 初版 trace：`outputs/real_clideo_no_trim_manual/trace.json`
- 初版脚本：`outputs/real_clideo_no_trim_manual/generated_script.py`
- 脚本 Debugger handoff：`outputs/real_clideo_no_trim_manual/script_debug_auto_resume/script_failure_handoff_01.json`
- 自动恢复后的脚本：`outputs/real_clideo_no_trim_manual/script_debug_auto_resume/resumed_generation/generated_script.py`
- 恢复脚本验证结果：`outputs/real_clideo_no_trim_manual/script_debug_auto_resume/resumed_script_verify_after_upload_wait/script_run.json`
- 成功截图目录：`outputs/real_clideo_no_trim_manual/script_debug_auto_resume/resumed_script_verify_after_upload_wait/browser/screenshots/`

Debug 过程：

```text
初版脚本第 3 步 upload 报告成功；
第 4 步点击 Speed 失败；
截图显示页面仍在 “Click to upload / Add media to timeline” 状态；
ScriptDebugger 判断为 missing_prerequisite_state，而不是 selector 机械错误；
流程 Debugger 回退到 editor URL；
Generation 重新上传并继续后续动作；
生成恢复后的独立脚本；
恢复脚本验证成功。
```

验证结果：

```text
Script success: True
Exit code: 0
Elapsed: 29.289s
```

关键回放日志节选：

```text
[01/12] goto recovered editor URL
[02/12] upload sample.mp4
[03/12] click Speed tab
[04/12] click 1.5x
[05/12] click Text
[08/12] input 短视频测试
[10/12] click opacity slider
[11/12] click Export
[12/12] click Continue
Replay finished.
```

这个例子主要证明 Debugger 不只会“换 selector”，还会判断流程前置状态是否真的满足，并在必要时交回 Generation 重新生成后半段流程。

## 8. 单元测试

```bash
cd new_drission
source .venv/bin/activate
pytest -q
```

当前测试覆盖：

- StepDecider JSON 解析、batch 策略和风险限制；
- TaskParser fallback；
- StateUpdater 轻量更新和 LLM 调用门控；
- Resilience Executor selector 校验、动作执行策略；
- OutcomeChecker 异常语义、兜圈和高风险结果信号；
- 流程 Debugger recovery strategy；
- ScriptRecorder 生成独立脚本、语义上传 fallback、文本 fallback；
- ScriptDebugger 机械修复与流程 handoff。

最近一次本地结果：

```text
42 passed
```

## 9. 最终脚本的独立性

生成脚本示例：

- `outputs/real_123apps_retest_20260626_175918_fallback_init/generated_script.py`
- `outputs/real_clideo_no_trim_manual/script_debug_auto_resume/resumed_generation/generated_script.py`

脚本顶部只包含标准库和 DrissionPage：

```python
from DrissionPage import ChromiumOptions, ChromiumPage
```

脚本不会导入：

```text
app.*
OpenAI client
DashScope client
任何 LLM SDK
```

也就是说，LLM 只参与生成、调试和修复阶段；交付给最终用户的 `.py` 可以离线重复运行。

## 10. 当前限制和下一步

当前系统已经能完成真实复杂网页上的生成、验证和脚本回放，但还有几个后续增强点：

1. `init-task` 的 LLM 输出还需要更严格 schema 归一化；现在真实复杂任务常用 `--init-no-llm` 保证初始 JSON 稳定。
2. 拖拽、复杂 canvas 坐标操作、时间轴精确拖动还没有完全抽象成稳定 Action。
3. 最终脚本目前主要依赖 replay helpers 做通用容错，后续可以把更多语义上下文直接写入 trace，使脚本更短、更可解释。
4. 目前多模态模型每轮读取 DOM + 截图，成功率优先，速度还可以继续通过低风险 batch、状态缓存和轻量 state update 优化。

## 11. 对 AgentBrowser 架构的落地说明

作业要求借鉴 AgentBrowser 的 Generation / Debugging / Resilience。这个项目的对应关系是：

```text
AgentBrowser Generation
  -> TaskParser + StepDecider + AgentLoop + ScriptRecorder
  -> 自然语言到浏览器动作，再到独立 DrissionPage 脚本

AgentBrowser Debugging
  -> ScriptRunner + ScriptDebugger + Flow Debugger
  -> 自动运行脚本、收集失败、LLM 诊断、patch 或回退继续 Generation

AgentBrowser Resilience
  -> Resilience Executor + OutcomeChecker + generated replay helpers
  -> selector fallback、智能等待、重试、上传语义、异常上下文、截图日志
```

和传统“让模型直接写完整脚本”相比，这里更强调真实浏览器中的逐步 grounding：模型每次都看到当前页面截图和 DOM，不确定就重新观察或交给 Debugger，而不是闭眼编按钮名字。这个设计在视频编辑器这类动态网页里更稳，也更符合 DrissionPage 的实际使用方式。
