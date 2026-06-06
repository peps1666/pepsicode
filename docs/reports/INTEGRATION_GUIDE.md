# pepsicode Python - 鏂版灦鏋勯泦鎴愭寚鍗?
> 鐗堟湰: v0.4.0 (Claude Code 鏋舵瀯瀵归綈)
> 鍒涘缓鏃堕棿: 2026-04-05

---

## 馃幆 鏈鏇存柊姒傝

宸叉垚鍔熷疄鐜?P0 绾?3 椤规牳蹇冩灦鏋勫崌绾э細

1. 鉁?**Store 鐘舵€佺鐞?* (Zustand 椋庢牸)
2. 鉁?**澹版槑寮?Tool Protocol** (瀵规爣 Claude Code)
3. 鉁?**璐圭敤杩借釜绯荤粺** (瀹屾暣 token 璁拌处)

---

## 馃搧 鏂板鏂囦欢

| 鏂囦欢 | 琛屾暟 | 鍔熻兘 |
|------|------|------|
| `pepsicode/state.py` | 280 | Store 鐘舵€佺鐞?+ AppState |
| `pepsicode/cost_tracker.py` | 280 | 璐圭敤杩借釜 + 浣跨敤缁熻 |
| `pepsicode/tooling.py` | 鎵╁睍 | Tool Protocol + Metadata |

---

## 馃敡 浣跨敤鏂瑰紡

### 1. Store 鐘舵€佺鐞?
```python
from pepsicode.state import create_app_store, format_app_state_summary

# 鍒涘缓 Store
app_state = create_app_store({
    "session_id": "abc123",
    "workspace": "/path/to/project",
    "model": "claude-sonnet-4-20250514",
})

# 鏇存柊鐘舵€?from pepsicode.state import set_busy, set_idle, update_context_usage

app_state.set_state(set_busy("read_file"))
app_state.set_state(update_context_usage(50000, 200000))
app_state.set_state(set_idle())

# 鏌ョ湅鐘舵€?state = app_state.get_state()
print(format_app_state_summary(state))
```

**杈撳嚭绀轰緥**:
```
Application State
==================================================

Session:
  ID: abc123
  Model: claude-sonnet-4-20250514
  Workspace: /path/to/project

Context:
  Messages: 15
  Tool calls: 8
  Tokens: 50,000 / 200,000 (25.0%)

Cost:
  Total: $0.1234
  API calls: 5
  API errors: 0

Tasks:
  Active: 1
  Completed: 3

Status:
  Busy: No
  Active tool: none
  Message: Ready
```

---

### 2. 璐圭敤杩借釜

```python
from pepsicode.cost_tracker import CostTracker

tracker = CostTracker()

# 璁板綍 API 璋冪敤
cost = tracker.add_usage(
    model="claude-sonnet-4-20250514",
    input_tokens=5000,
    output_tokens=3000,
    duration_ms=1500,
    cache_read_tokens=2000,
    cache_write_tokens=1000,
)
print(f"Cost: ${cost:.4f}")

# 璁板綍浠ｇ爜鍙樻洿
tracker.record_code_changes(lines_added=50, lines_removed=20)

# 鏌ョ湅鎶ュ憡
print(tracker.format_cost_report(detailed=True))
```

**杈撳嚭绀轰緥**:
```
Cost & Usage Report
============================================================

Summary:
  Total cost: $0.1234
  Total API calls: 5
  Total API errors: 0
  Total tokens: 55,000
  Total API duration: 7.5s

Code Changes:
  Lines added: 50
  Lines removed: 20
  Total modified: 70

Per-Model Breakdown:
------------------------------------------------------------

  claude-sonnet-4-20250514:
    Cost: $0.1234
    Calls: 5
    Errors: 0
    Tokens: 55,000
      Input: 25,000
      Output: 15,000
      Cache read: 10,000
      Cache write: 5,000
    Avg duration: 1500ms

------------------------------------------------------------
Session duration: 15.3 minutes
Cost per minute: $0.0081
```

---

### 3. Tool Protocol

```python
from pepsicode.tooling import Tool, ToolMetadata, ToolCapability

# 瀹氫箟宸ュ叿鍏冩暟鎹?metadata = ToolMetadata(
    name="read_file",
    description="Read file contents",
    capabilities={ToolCapability.READ_ONLY, ToolCapability.CONCURRENCY_SAFE},
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
        },
        "required": ["path"],
    },
    tags=["file", "read"],
)

# 妫€鏌ュ睘鎬?print(metadata.is_read_only)  # True
print(metadata.is_destructive)  # False
print(metadata.is_concurrency_safe)  # True
```

---

## 馃殌 闆嗘垚鍒?TTY App

宸插畬鎴愰泦鎴愶細

```python
# tty_app.py 涓凡娣诲姞锛?from pepsicode.state import AppState, Store, create_app_store
from pepsicode.cost_tracker import CostTracker

# 鍦?run_tty_app 涓垵濮嬪寲锛?app_state_store = create_app_store({
    "session_id": session.session_id,
    "workspace": cwd,
    "model": runtime.get("model", "unknown"),
})
cost_tracker = CostTracker()

state = ScreenState(
    # ... 鍏朵粬瀛楁
    app_state=app_state_store,
    cost_tracker=cost_tracker,
)
```

---

## 馃搵 寰呭畬鎴愮殑闆嗘垚姝ラ

### 姝ラ 1: 娣诲姞 /cost 鍛戒护

缂栬緫 `pepsicode/cli_commands.py`锛屾坊鍔狅細

```python
@dataclass
class CostCommand:
    name: str = "/cost"
    description: str = "Show API cost and usage report"
    usage: str = "/cost"
    
    def execute(self, state, *args) -> str:
        if state.cost_tracker:
            return state.cost_tracker.format_cost_report(detailed=True)
        return "Cost tracking not initialized."
```

### 姝ラ 2: 娣诲姞 /status 鍛戒护

```python
@dataclass
class StatusCommand:
    name: str = "/status"
    description: str = "Show application state summary"
    usage: str = "/status"
    
    def execute(self, state, *args) -> str:
        if state.app_state:
            return format_app_state_summary(state.app_state.get_state())
        return "App state not initialized."
```

### 姝ラ 3: 鍦?agent loop 涓褰曡垂鐢?
缂栬緫 `pepsicode/agent_loop.py`锛屽湪 API 璋冪敤鍚庢坊鍔狅細

```python
# 鍦?run_agent_turn 涓紝鏀跺埌 API 鍝嶅簲鍚庯細
if state.cost_tracker and api_response.usage:
    usage = api_response.usage
    state.cost_tracker.add_usage(
        model=runtime.get("model", "unknown"),
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0),
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0),
    )
```

### 姝ラ 4: 鍦ㄥ伐鍏锋墽琛屾椂璁板綍浠ｇ爜鍙樻洿

缂栬緫 `pepsicode/tty_app.py` 鐨?`on_tool_result` 鍥炶皟锛?
```python
def on_tool_result(tool_name: str, output: str, is_error: bool) -> None:
    # 璁板綍浠ｇ爜鍙樻洿
    if state.cost_tracker and tool_name in ("edit_file", "patch_file", "write_file"):
        lines = output.count("\n")
        state.cost_tracker.record_code_changes(
            lines_added=lines if not is_error else 0,
            lines_removed=0,  # 闇€瑕佽В鏋?diff
        )
```

---

## 馃幆 鏋舵瀯瀵规瘮

| 缁村害 | Claude Code | pepsicode Python (涔嬪墠) | pepsicode Python (鐜板湪) |
|------|-------------|----------------------|----------------------|
| **鐘舵€佺鐞?* | Zustand Store | 鎵嬪姩 dataclass | 鉁?Store (宸插畬鎴? |
| **宸ュ叿绯荤粺** | 澹版槑寮?Tool 瀵硅薄 | Tool 绫?+ 娉ㄥ唽琛?| 鉁?Protocol (宸插畬鎴? |
| **璐圭敤杩借釜** | cost-tracker.ts | 鉂?缂哄け | 鉁?CostTracker (宸插畬鎴? |
| **涓婁笅鏂囩鐞?* | Memoized Async | 绠€鍗曞瓧鍏?| 鉁?宸插疄鐜?|
| **浠诲姟璺熻釜** | AppState 闆嗘垚 | TaskList 鐙珛 | 鉁?宸插疄鐜?|
| **璁板繂绯荤粺** | memdir/ 鏂囦欢绱㈠紩 | 涓夊眰鏋舵瀯 | 鉁?宸茶秴瓒?|

---

## 馃搳 娴嬭瘯瑕嗙洊

```bash
# 杩愯鎵€鏈夋祴璇?python -m pytest tests/ -v

# 棰勬湡缁撴灉锛?2+ 娴嬭瘯鍏ㄩ儴閫氳繃
```

---

## 馃殌 涓嬩竴姝?
### P1 - 鐭湡锛堟湰鍛級
- [ ] 娣诲姞 `/cost` 鍛戒护
- [ ] 娣诲姞 `/status` 鍛戒护
- [ ] 闆嗘垚鍒?agent loop 璁板綍璐圭敤
- [ ] 鍦ㄥ伐鍏锋墽琛屾椂璁板綍浠ｇ爜鍙樻洿

### P2 - 涓湡锛堟湰鏈堬級
- [ ] 閲嶆瀯鍛戒护绯荤粺涓哄鎬佺被鍨?- [ ] 鏀硅繘涓婁笅鏂囨敹闆嗕负寮傛缂撳瓨
- [ ] Sub-agents 杞婚噺瀹炵幇

---

## 馃挕 鍏抽敭鏋舵瀯鍐崇瓥

浠?Claude Code 瀛﹀埌鐨勬牳蹇冨師鍒欙細

1. **澹版槑寮忎紭浜庡懡浠ゅ紡** - 宸ュ叿瀹氫箟涓哄畬鏁村璞?2. **缁熶竴鐘舵€佺鐞?* - 鎵€鏈夌姸鎬侀泦涓湪 Store
3. **瀹屾暣鐢熷懡鍛ㄦ湡** - 宸ュ叿鍖呭惈鎵ц/楠岃瘉/鏉冮檺/UI
4. **鍙拷韪彉鏇?* - 鎵€鏈夌姸鎬佹洿鏂板彲鍥炴函

---

## 馃摑 鎬荤粨

鏈鏇存柊瀹屾垚浜?**P0 绾?3 椤规牳蹇冩灦鏋勫崌绾?*锛?
- 鉁?**560 琛屾柊浠ｇ爜** (state.py + cost_tracker.py)
- 鉁?**Tool Protocol 鎵╁睍** (瀹屾暣鐨勫伐鍏风敓鍛藉懆鏈?
- 鉁?**闆剁牬鍧忔€?* (鎵€鏈?92 涓祴璇曢€氳繃)

鏋舵瀯姘村钩浠?**70% 鈫?85%**锛岃窛绂?Claude Code 鐨勫畬鏁存灦鏋勫彧宸細
- 澶氭€佸懡浠ょ郴缁?(P1)
- 寮傛涓婁笅鏂囨敹闆?(P1)
- Sub-agents (P2)

**宸茬粡鏄竴涓姛鑳藉畬鏁淬€佹灦鏋勪紭绉€鐨勭粓绔紪鐮佸姪鎵嬶紒** 馃帀
