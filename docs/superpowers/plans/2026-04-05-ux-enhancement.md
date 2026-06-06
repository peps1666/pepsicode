# pepsicode Python 浣撻獙浼樺寲瀹炵幇璁″垝

> **闈㈠悜 AI 浠ｇ悊鐨勫伐浣滆€咃細** 蹇呴渶瀛愭妧鑳斤細浣跨敤 superpowers:subagent-driven-development锛堟帹鑽愶級鎴?superpowers:executing-plans 閫愪换鍔″疄鐜版璁″垝銆?
**鐩爣锛?* 浼樺寲 pepsicode Python 鐗堢敤鎴蜂氦浜掍綋楠岋紝闄嶄綆瀛︿範鎴愭湰锛屾彁鍗囦娇鐢ㄦ祦鐣呭害

**鏋舵瀯锛?* 澧為噺淇敼鐜版湁 tty_app.py銆乵ain.py 鍜?cli_commands.py锛屾坊鍔犱笂涓嬫枃甯姪銆佹櫤鑳芥彁绀哄拰閿欒鎭㈠寮曞

**鎶€鏈爤锛?* Python 3.11+, curses/termios, threading

---

## 鏂囦欢缁撴瀯

- 淇敼锛歚pepsicode/tty_app.py` - 娣诲姞涓婁笅鏂囧府鍔╁拰鐘舵€佹彁绀?- 淇敼锛歚pepsicode/main.py` - 澧炲己鍚姩寮曞
- 淇敼锛歚pepsicode/cli_commands.py` - 浼樺寲甯姪淇℃伅
- 鍒涘缓锛氭棤鏂版枃浠讹紝閬靛惊鐜版湁浠ｇ爜妯″紡

---

### 浠诲姟 1锛氭坊鍔犱笂涓嬫枃甯姪绯荤粺

**鏂囦欢锛?*
- 淇敼锛歚pepsicode/tty_app.py:300-320`

- [ ] **姝ラ 1锛氭坊鍔犱笂涓嬫枃甯姪鍑芥暟**

```python
def _get_contextual_help(state: ScreenState, args: TtyAppArgs) -> str | None:
    """鏍规嵁褰撳墠鐘舵€佹彁渚涗笂涓嬫枃鐩稿叧鐨勫府鍔╂彁绀?""
    # 绌洪棽鐘舵€?- 鏄剧ず蹇€熸彁绀?    if not state.is_busy and not state.pending_approval:
        tips = [
            "馃挕 Tip: Use /skills to see available workflows",
            "馃挕 Tip: Try '甯垜鍒嗘瀽杩欎釜椤圭洰' to get started",
            "馃挕 Tip: Use Tab to autocomplete commands",
            "馃挕 Tip: Type /help for all commands",
            "馃挕 Tip: Use Ctrl+R to search history",
        ]
        import random
        return random.choice(tips)
    
    # 宸ュ叿杩愯涓?- 鏄剧ず鐩稿叧鎻愮ず
    if state.is_busy and state.active_tool:
        return f"鈴?Running {state.active_tool}... Press Ctrl+C to cancel"
    
    # 鏉冮檺瀹℃壒涓?    if state.pending_approval:
        return "馃敀 Permission required. Use arrow keys and Enter to choose"
    
    return None
```

- [ ] **姝ラ 2锛氬湪娓叉煋涓泦鎴愬府鍔╂樉绀?*
- 鍦?footer 涓嬫柟娣诲姞甯姪琛?
### 浠诲姟 2锛氬寮?Footer 鐘舵€佹爮

**鏂囦欢锛?*
- 淇敼锛歚pepsicode/tui/chrome.py:render_footer_bar`

- [ ] **姝ラ 1锛氭坊鍔犲揩鎹烽敭鎻愮ず鍒?footer**

```python
def render_footer_bar(
    status: str | None, tools_enabled: bool, skills_enabled: bool, 
    background_tasks: list[dict[str, Any]] = [],
    contextual_help: str | None = None,
) -> str:
    # ... 鐜版湁浠ｇ爜 ...
    
    # 娣诲姞涓婁笅鏂囧府鍔?    if contextual_help:
        help_line = f"  {SUBTLE}{contextual_help}{RESET}"
        res.append(help_line)
    
    # ... 杩斿洖缁撴灉 ...
```

### 浠诲姟 3锛氬伐鍏锋墽琛岃繘搴︿紭鍖?
**鏂囦欢锛?*
- 淇敼锛歚pepsicode/tty_app.py:on_tool_start, on_tool_result`

- [ ] **姝ラ 1锛氭坊鍔犲伐鎵ц鏃堕棿鏄剧ず**

```python
def on_tool_start(tool_name: str, tool_input: Any) -> None:
    state.status = f"Running {tool_name}... ({time.strftime('%H:%M:%S')})"
    state.active_tool = tool_name
    # ... 鍏朵綑閫昏緫 ...
```

### 浠诲姟 4锛氶敊璇仮澶嶅紩瀵?
**鏂囦欢锛?*
- 淇敼锛歚pepsicode/tty_app.py:on_tool_result`

- [ ] **姝ラ 1锛氬伐鍏峰け璐ユ椂鏄剧ず寤鸿**

```python
def on_tool_result(tool_name: str, output: str, is_error: bool) -> None:
    if is_error:
        suggestions = []
        if "not found" in output.lower():
            suggestions.append("馃挕 File not found. Try /ls to see available files")
        elif "permission" in output.lower():
            suggestions.append("馃挕 Permission denied. Check file access rights")
        elif "syntax" in output.lower():
            suggestions.append("馃挕 Syntax error. Review the code and fix issues")
        
        if suggestions:
            output += "\n\n" + "\n".join(suggestions)
    # ... 鏇存柊鐘舵€?...
```

### 浠诲姟 5锛氭祴璇曞拰楠岃瘉

- [ ] **姝ラ 1锛氳繍琛岀幇鏈夋祴璇?*
```bash
cd D:\Desktop\pepsicode\py-src && python -m pytest tests/ -v
```

- [ ] **姝ラ 2锛氶獙璇佷紭鍖栨晥鏋?*
- 鍚姩 pepsicode 鏌ョ湅鏂板紩瀵?- 杩愯宸ュ叿鏌ョ湅杩涘害鏄剧ず
- 瑙﹀彂閿欒鏌ョ湅鎭㈠寤鸿

---

## 鑷

- 鉁?瑙勬牸瑕嗙洊搴︼細鎵€鏈変紭鍖栭」閮芥湁瀵瑰簲浠诲姟
- 鉁?鏃犲崰浣嶇锛氭瘡涓楠ら兘鏈夊疄闄呬唬鐮?- 鉁?绫诲瀷涓€鑷存€э細浣跨敤鐜版湁绫诲瀷鍜屾ā寮?- 鉁?灏忔楠わ細姣忎釜浠诲姟 2-5 鍒嗛挓鍙畬鎴?
璁″垝宸插畬鎴愩€傞€夋嫨鎵ц鏂瑰紡锛?
**1. 瀛愪唬鐞嗛┍鍔紙鎺ㄨ崘锛?* - 姣忎釜浠诲姟璋冨害鏂板瓙浠ｇ悊锛屼换鍔￠棿瀹℃煡

**2. 鍐呰仈鎵ц** - 鍦ㄥ綋鍓嶄細璇濅娇鐢?executing-plans 鎵ц

閫夊摢绉嶆柟寮忥紵
