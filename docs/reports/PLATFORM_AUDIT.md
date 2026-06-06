# pepsicode Python 鐗?鈥?Linux / macOS 骞冲彴閫傞厤瀹¤

> 瀹¤鏃ユ湡: 2026-04-06
> 瀹¤鑼冨洿: `pepsicode/` 涓嬫墍鏈?`.py` 鏂囦欢鐨勮法骞冲彴鍏煎鎬?
## 鎬荤粨

**缁撹锛氫唬鐮佸凡鍏峰鑹ソ鐨勮法骞冲彴妗嗘灦锛岀粷澶ч儴鍒嗗钩鍙板垎鏀凡姝ｇ‘瀹炵幇銆?*
鍙戠幇 **3 涓湡姝ｇ殑 Bug**銆?*4 涓綔鍦ㄩ棶棰?* 鍜?**2 涓寮哄缓璁?*銆?
---

## 馃敶 鐪熸鐨?Bug锛堝繀椤讳慨澶嶏級

### Bug 1: Unix raw mode 涓?`sys.stdin.read(1)` 闃诲闂

**鏂囦欢**: `tty_app.py:1366-1382`

```python
# 褰撳墠浠ｇ爜:
ready, _, _ = select.select([sys.stdin], [], [], 0.05)
if not ready:
    throttled.flush()
    continue
chunk = ""
while True:
    ready2, _, _ = select.select([sys.stdin], [], [], 0)
    if not ready2:
        break
    ch = sys.stdin.read(1)  # 鈫?闂鍦ㄨ繖閲?```

**闂**: `tty.setraw()` 鎶婄粓绔涓?raw mode锛屼絾 `sys.stdin` 鐨?Python 灞傜紦鍐蹭粛鐒舵槸琛岀紦鍐诧紙鎴?8KB 鍧楃紦鍐诧級銆俙sys.stdin.read(1)` 鍙兘鍦?Python 鐨?`BufferedReader` 鍐呴儴鎵ц涓€娆″ぇ鐨?`read(8192)`锛岀劧鍚庡彧杩斿洖 1 涓瓧鑺傘€傚湪 raw mode 涓嬭繖涓簳灞?`read(8192)` 浼氶樆濉炵洿鍒版湁閭ｄ箞澶氬瓧鑺傚彲鐢紙鎴栬€?EOF锛夆€斺€斿疄闄呬笂涓嶄細锛屽洜涓?raw mode 涓?read syscall 鍦ㄦ湁浠讳綍瀛楄妭鏃跺氨杩斿洖锛屼絾鍏抽敭鏄?**Python 鐨?`io.TextIOWrapper` 灞備細鍦?decode 鏃跺皾璇曡鍙栧畬鏁寸殑 UTF-8 澶氬瓧鑺傚簭鍒?*銆傚鏋滅敤鎴疯緭鍏ヤ腑鏂?Emoji锛堥渶瑕?3-4 瀛楄妭鐨?UTF-8锛夛紝绗竴涓瓧鑺傚埌杈惧悗 TextIOWrapper 鍙兘灏濊瘯璇诲彇鍚庣画瀛楄妭锛屽鏋滃悗缁瓧鑺傚洜涓烘椂搴忓師鍥犵◢鏈夊欢杩燂紝灏卞彲鑳界煭鏆傞樆濉炪€?
鏇存牴鏈殑闂锛?*`sys.stdin` 鍦?raw mode 涓嬪簲璇ヤ娇鐢?`sys.stdin.buffer.read(1)` 璇诲彇鍘熷瀛楄妭锛岀劧鍚庤嚜琛屾嫾鎺?UTF-8**銆?
**淇鏂规**:
```python
# 鐢?os.read() 璇诲彇鍘熷瀛楄妭锛岀劧鍚庢墜鍔?decode
fd = sys.stdin.fileno()
chunk_bytes = os.read(fd, 4096)  # 闈為樆濉烇紙raw mode涓嬫湁鏁版嵁灏辫繑鍥烇級
chunk = chunk_bytes.decode("utf-8", errors="replace")
```

### Bug 2: Unix raw mode 涓?stdout 鏃犳硶姝ｅ父杈撳嚭

**鏂囦欢**: `tty_app.py` 鍏ㄥ眬

**闂**: `tty.setraw(sys.stdin.fileno())` 鎶?stdin 鎵€鍦ㄧ殑 tty 璁剧疆涓?raw mode锛岃繖鍚屾椂褰卞搷浜?stdout 鐨勮涓衡€斺€?*raw mode 浼氱鐢?output postprocessing锛坄OPOST`锛?*锛屽鑷?`\n` 涓嶅啀琚嚜鍔ㄧ炕璇戜负 `\r\n`銆傜粨鏋滄槸鎵€鏈?`print()` / `sys.stdout.write()` 杈撳嚭鐨勬崲琛屼細鍙樻垚鍙湁 LF 鑰屾病鏈?CR锛屾枃鏈細"闃舵寮?鍋忕Щ銆?
**淇鏂规**: 鐢?`tty.setcbreak()` 鏇夸唬 `tty.setraw()`锛屾垨鑰呮墜鍔ㄨ缃?termios 灞炴€э紝淇濈暀 `OPOST`:

```python
def __enter__(self) -> _RawModeContext:
    if sys.platform == "win32":
        ...
    else:
        import termios
        import tty

        fd = sys.stdin.fileno()
        self._old_settings = termios.tcgetattr(fd)
        # 浣跨敤 setcbreak 鑰岄潪 setraw:
        # setcbreak 绂佺敤琛岀紦鍐插拰 echo锛屼絾淇濈暀 output processing (OPOST)
        # 杩欐牱 \n 鈫?\r\n 鐨勭炕璇戜粛鐒剁敓鏁?        tty.setcbreak(fd)
    return self
```

鎴栬€呭鏋滈渶瑕佹洿绮剧粏鐨勬帶鍒讹紙鏌愪簺鐗规畩閿彧鏈?raw mode 鎵嶈兘鎹曡幏锛?

```python
import termios
fd = sys.stdin.fileno()
self._old_settings = termios.tcgetattr(fd)
new = termios.tcgetattr(fd)
# iflag: 鍏抽棴 ICRNL (CR鈫扤L), IXON (flow control)
new[0] &= ~(termios.ICRNL | termios.IXON)
# lflag: 鍏抽棴 ECHO, ICANON (canonical mode), ISIG (signals from keys)
new[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
# oflag: 淇濈暀 OPOST (output processing, \n 鈫?\r\n)
# new[1] 涓嶅姩 鈫?杩欐槸鍏抽敭锛乻etraw() 浼氭竻鎺?OPOST
# cc: VMIN=1, VTIME=0 (鑷冲皯璇?瀛楄妭灏辫繑鍥?
new[6][termios.VMIN] = 1
new[6][termios.VTIME] = 0
termios.tcsetattr(fd, termios.TCSAFLUSH, new)
```

### Bug 3: `_read_raw_char()` / `_read_raw_chunk()` 鍦?Unix 涓嬩娇鐢ㄩ珮灞?`sys.stdin.read(1)` 鑰岄潪搴曞眰璇诲彇

**鏂囦欢**: `tty_app.py:749-784`

**闂**: 涓?Bug 1 鍚屾簮銆俙sys.stdin.read(1)` 缁忚繃 Python 鐨?TextIOWrapper 鍜?BufferedReader 灞傦紝鍦?raw mode 缁堢涓嬭涓轰笉鍙潬銆傜壒鍒槸锛?- `select()` 鎶ュ憡 fd 鍙锛屼絾 `sys.stdin.read(1)` 鍙兘鍦?TextIOWrapper 鍐呴儴闃诲
- 澶氬瓧鑺?UTF-8 瀛楃鍙兘琚埅鏂?- `_read_raw_chunk()` 鐨?while 寰幆涓?`select(..., 0)` 妫€娴嬪埌鏃犳暟鎹氨 break锛屼絾姝ゆ椂 Python 鍐呴儴缂撳啿鍖哄彲鑳借繕鏈夋暟鎹?
**淇鏂规**: 缁熶竴浣跨敤 `os.read(fd, N)` 璇诲師濮嬪瓧鑺?

```python
def _read_raw_chunk() -> str:
    if sys.platform == "win32":
        ...  # 淇濇寔涓嶅彉
    else:
        fd = sys.stdin.fileno()
        import select
        ready, _, _ = select.select([fd], [], [], 0.05)
        if not ready:
            return ""
        data = os.read(fd, 4096)
        if not data:
            return ""
        return data.decode("utf-8", errors="replace")
```

---

## 馃煛 娼滃湪闂锛堝缓璁慨澶嶏級

### 闂 1: `SIGWINCH` 淇″彿澶勭悊鍙兘涓庣嚎绋嬪啿绐?
**鏂囦欢**: `tty_app.py:1315-1327`

```python
if sys.platform != "win32":
    import signal as _signal
    def _on_sigwinch(_signum: int, _frame: Any) -> None:
        invalidate_terminal_size_cache()
        throttled.request()
    _prev_sigwinch = _signal.signal(_signal.SIGWINCH, _on_sigwinch)
```

**闂**: Python 鐨勪俊鍙峰鐞嗗嚱鏁板彧鑳藉湪涓荤嚎绋嬩腑璁剧疆銆傚鏋?`run_tty_app()` 涓嶅湪涓荤嚎绋嬩腑璋冪敤锛堣櫧鐒堕€氬父涓嶄細锛夛紝`signal.signal()` 浼氭姏鍑?`ValueError: signal only works in main thread`銆?
**寤鸿**: 鍔犱竴涓畨鍏ㄦ鏌?

```python
if sys.platform != "win32" and threading.current_thread() is threading.main_thread():
    ...
```

### 闂 2: macOS 涓?`os.get_terminal_size()` 鍦ㄦ煇浜涚粓绔ā鎷熷櫒涓彲鑳借繑鍥?(0, 0)

**鏂囦欢**: `tui/chrome.py:86`

**闂**: 鍦ㄦ煇浜?macOS 缁堢锛堝閫氳繃 SSH 杩炴帴銆佹垨鍦?tmux 鍐?pane 鍒氬垱寤烘椂锛夛紝`os.get_terminal_size()` 鍙兘杩斿洖 `(0, 0)`銆傚綋鍓嶇殑 fallback `(100, 40)` 鍙湪寮傚父鏃惰Е鍙戯紝涓嶈鐩?`(0, 0)` 鐨勬儏鍐点€?
**寤鸿**:
```python
ts = os.get_terminal_size()
cols, rows = ts.columns, ts.lines
if cols <= 0 or rows <= 0:
    _ts_cache = (100, 40)
else:
    _ts_cache = (cols, rows)
```

### 闂 3: shell 鍛戒护鏋勫缓 鈥?macOS 榛樿 shell 鏄?zsh 涓嶆槸 bash

**鏂囦欢**: `tools/run_command.py:154`

```python
return "bash", ["-lc", shell_command]
```

**闂**: macOS 浠?Catalina (10.15) 璧烽粯璁?shell 鏄?zsh銆傝櫧鐒?bash 浠嶇劧棰勮锛屼絾鐢?`bash -lc` 鎰忓懗鐫€锛?1. 濡傛灉鐢ㄦ埛鐨?`.bashrc` / `.bash_profile` 鏈厤缃紙鍥犱负鐢ㄦ埛鐢?zsh锛夛紝鏌愪簺鐜鍙橀噺鍙兘缂哄け
2. 濡傛灉绯荤粺鏈畨瑁?bash锛堟瀬绔儏鍐碉紝濡傚鍣級锛屼細鐩存帴鎶ラ敊

**寤鸿**: 浣跨敤 `$SHELL` 鎴?`/bin/sh`:
```python
shell = os.environ.get("SHELL", "/bin/sh")
return shell, ["-lc", shell_command]
```

鎴栬€呮洿淇濆畧鍦扮敤 `/bin/sh`锛圥OSIX 鍏煎锛?
```python
return "/bin/sh", ["-c", shell_command]
```

娉ㄦ剰 `-l` (login shell) 鍦?`/bin/sh` 涓婁篃鏈夋晥锛屼絾琛屼负鍥犲钩鍙拌€屽紓銆?
### 闂 4: MCP `allowed_system_dirs` 缂哄皯甯歌 Linux 璺緞

**鏂囦欢**: `mcp.py:65-75`

```python
allowed_system_dirs = [
    '/usr/bin', '/usr/local/bin', '/usr/sbin', '/opt',
    '/opt/homebrew/bin', '/opt/homebrew/sbin',  # macOS Homebrew (Apple Silicon)
    '/usr/local/Cellar',  # macOS Homebrew (Intel)
]
```

**缂哄皯**:
- `/snap/bin` 鈥?Ubuntu Snap 鍖?- `/home/linuxbrew/.linuxbrew/bin` 鈥?Linux Homebrew
- `/usr/local/sbin` 鈥?甯歌 sbin 璺緞
- `~/.local/bin` 鈥?pip install --user / pipx 瀹夎璺緞
- `~/.cargo/bin` 鈥?Rust 宸ュ叿閾?- `~/.nvm/` 鈥?Node.js via nvm (鍙樼璺緞)

**寤鸿**: 鎵╁睍鍒楄〃锛屾垨鑰呮敼涓烘洿瀹芥澗鐨勭瓥鐣ワ紙鍙姝㈠凡鐭ュ嵄闄╃殑 shell锛屼笉闄愬埗鍙墽琛屾枃浠惰矾寰勶級銆?
---

## 馃煝 宸叉纭鐞嗙殑璺ㄥ钩鍙板垎鏀?
| 妯″潡 | 骞冲彴鍒嗘敮 | 鐘舵€?|
|---|---|---|
| `tui/screen.py` | Windows VT processing 鍚敤 | 鉁?姝ｇ‘锛岄潪 Windows 璺宠繃 |
| `tty_app.py` | `_RawModeContext` Windows/Unix 鍒嗘敮 | 鉁?缁撴瀯姝ｇ‘锛堜絾鏈?Bug 2锛?|
| `tty_app.py` | `_win_read_one_key()` Windows 涓撶敤 | 鉁?姝ｇ‘闅旂 |
| `tty_app.py` | `SIGWINCH` 浠?Unix | 鉁?姝ｇ‘鍒ゆ柇 |
| `background_tasks.py` | `_is_process_alive()` Windows ctypes / Unix kill(0) | 鉁?姝ｇ‘ |
| `mcp.py` | `CREATE_NO_WINDOW` 浠?Windows | 鉁?姝ｇ‘ |
| `mcp.py` | `close()` Windows taskkill / Unix SIGTERM+SIGKILL | 鉁?姝ｇ‘ |
| `tools/run_command.py` | `split_command_line()` posix=True/False | 鉁?姝ｇ‘ |
| `tools/run_command.py` | `_build_execution_command()` cmd/bash 鍒嗘敮 | 鉁?姝ｇ‘锛堜絾瑙侀棶棰?3锛?|
| `tools/run_command.py` | background process isolation flags | 鉁?姝ｇ‘ |
| `install.py` | 涓夊钩鍙?launcher script | 鉁?姝ｇ‘ |
| `install.py` | PATH 娣诲姞鎸囧紩 (zshrc/bashrc/sysdm) | 鉁?姝ｇ‘ |
| `config.py` | `Path.home()` 璺ㄥ钩鍙?| 鉁?姝ｇ‘ |
| `workspace.py` | `Path.resolve()` 璺ㄥ钩鍙?| 鉁?姝ｇ‘ |
| `tui/input_parser.py` | 绾?ANSI 瑙ｆ瀽锛屽钩鍙版棤鍏?| 鉁?姝ｇ‘ |

---

## 馃挕 澧炲己寤鸿

### 寤鸿 1: 娣诲姞 `TERM` 鐜鍙橀噺妫€娴?
鍦?`enter_alternate_screen()` 涔嬪墠妫€娴?`$TERM`銆傛煇浜涚粓绔紙濡?`dumb`銆乣linux` console锛変笉鏀寔 alternate screen 鎴栭紶鏍囪拷韪紝寮哄埗鍚敤浼氬鑷翠贡鐮?

```python
def _term_supports_alt_screen() -> bool:
    term = os.environ.get("TERM", "")
    return term not in ("dumb", "linux", "")
```

### 寤鸿 2: macOS 涓?Homebrew Python 璺緞澶勭悊

`install.py:60` 涓?`sys.executable` 鍦?macOS Homebrew 瀹夎鐨?Python 涓嬪彲鑳借繑鍥?symlink 璺緞濡?`/opt/homebrew/bin/python3`銆傝繖娌℃湁閿欙紝浣嗗鏋滅敤鎴烽€氳繃 pyenv / asdf 绠＄悊 Python 鐗堟湰锛宍sys.executable` 鍙兘鎸囧悜 shim 鑰岄潪鐪熷疄璺緞銆傚缓璁湪 launcher script 涓娇鐢?`$(command -v python3)` 鑰岄潪纭紪鐮佽矾寰勩€?
---

## 淇浼樺厛绾?
| 浼樺厛绾?| 椤圭洰 | 褰卞搷 |
|---|---|---|
| **P0** | Bug 2: raw mode 绂佺敤 OPOST 鈫?杈撳嚭闃舵寮?| Linux/macOS 涓婂畬鍏ㄦ棤娉曟甯镐娇鐢?|
| **P0** | Bug 1 & 3: stdin.read(1) 鏇挎崲涓?os.read() | 澶氬瓧鑺傝緭鍏ュ彲鑳藉崱姝?|
| **P1** | 闂 3: bash 鈫?$SHELL or /bin/sh | macOS 鐢ㄦ埛鐜鍙橀噺缂哄け |
| **P2** | 闂 2: terminal size (0,0) 妫€娴?| 杈圭紭 case |
| **P2** | 闂 4: MCP 鍏佽璺緞鎵╁睍 | 瀹夊叏绛栫暐鏉剧揣搴?|
| **P3** | 闂 1: SIGWINCH 绾跨▼瀹夊叏 | 鏋佺 case |
| **P3** | 寤鸿 1 & 2 | 浣撻獙浼樺寲 |
