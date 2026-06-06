# pepsicode Python - 浣跨敤鎸囧崡

> 鐗堟湰: v0.2.0
> 鏇存柊鏃堕棿: 2026-04-05

---

## 馃殌 蹇€熷紑濮?
### 1. 瀹夎锛堥娆′娇鐢級

```bash
# 杩愯浜や簰寮忓畨瑁呭悜瀵?python -m pepsicode.main --install
```

瀹夎鍚戝浼氳姹傝緭鍏ワ細
- **Model name**: 妯″瀷鍚嶇О锛堝 `claude-sonnet-4-20250514`锛?- **ANTHROPIC_BASE_URL**: API 鍦板潃锛堥粯璁?`https://api.anthropic.com`锛?- **ANTHROPIC_AUTH_TOKEN**: API 瀵嗛挜

閰嶇疆浼氫繚瀛樺埌 `~/.pepsi-code/settings.json`

### 2. 鍚姩

```bash
# 姝ｅ父鍚姩
python -m pepsicode.main

# 鎴栦娇鐢?mock 妯″紡锛堟棤闇€ API锛岀敤浜庢祴璇曪級
set PEPSI_CODE_MODEL_MODE=mock
python -m pepsicode.main
```

### 3. 鍩烘湰浣跨敤

鍚姩鍚庝綘浼氱湅鍒板叏灞?TUI 鐣岄潰锛?
```
鈺攢鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈺?鈹?pepsicode                  鈹?provider                         鈹?鈹?                                                                 鈹?鈹?Terminal coding assistant for pepsicode.                        鈹?鈹?                                                                 鈹?鈹?pepsicode                  鈹?.../Desktop/pepsicode/py-src        鈹?鈹?[provider] offline  [model] mock  [msgs] 0  [events] 0        鈹?鈹?cwd: ...                                                           鈹?鈺扳攢鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈺?
鈺攢鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€ session feed 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈺?鈹?Ready                                                    鈹?鈹?                                                         鈹?鈹?Type /help for commands.                                 鈹?鈺扳攢鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈺?
鈺攢鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€ prompt 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈺?鈹?>                                                        鈹?鈺扳攢鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈺?
tools on | skills on
```

**杈撳叆浣犵殑闂**锛岀劧鍚庢寜 Enter銆侻ock 妯″紡涓嬩細妯℃嫙 AI 鍝嶅簲銆?
---

## 馃搵 鍛戒护琛岄€夐」

### 浼氳瘽绠＄悊

```bash
# 鍒楀嚭鎵€鏈変繚瀛樼殑浼氳瘽
python -m pepsicode.main --list-sessions

# 鎭㈠鏈€杩戠殑浼氳瘽
python -m pepsicode.main --resume

# 鎭㈠鐗瑰畾浼氳瘽
python -m pepsicode.main --resume abc123def456

# 浣跨敤鐗瑰畾浼氳瘽 ID 鍚姩
python -m pepsicode.main --session abc123def456
```

### 瀹夎

```bash
# 杩愯浜や簰寮忓畨瑁?python -m pepsicode.main --install
```

### 甯姪

```bash
# 鏄剧ず甯姪淇℃伅
python -m pepsicode.main --help
```

---

## 鈱笍 閿洏蹇嵎閿?
### 杈撳叆缂栬緫

| 蹇嵎閿?| 鍔熻兘 |
|--------|------|
| `Enter` | 鎻愪氦杈撳叆 / 纭閫夋嫨 |
| `Tab` | 鑷姩琛ュ叏 slash 鍛戒护 |
| `Backspace` | 鍒犻櫎鍓嶄竴涓瓧绗?|
| `Delete` | 鍒犻櫎褰撳墠瀛楃 |
| `Ctrl-U` | 娓呯┖鏁磋 |
| `Ctrl-A` / `Home` | 璺冲埌琛岄 |
| `Ctrl-E` / `End` | 璺冲埌琛屽熬 |
| `鈫恅 / `鈫抈 | 宸﹀彸绉诲姩鍏夋爣 |
| `Escape` | 娓呯┖杈撳叆 |

### 鍘嗗彶瀵艰埅

| 蹇嵎閿?| 鍔熻兘 |
|--------|------|
| `鈫慲 / `Ctrl-P` | 涓婁竴鏉″巻鍙?|
| `鈫揱 / `Ctrl-N` | 涓嬩竴鏉″巻鍙?|

### 婊氬姩

| 蹇嵎閿?| 鍔熻兘 |
|--------|------|
| `PageUp` | 鍚戜笂婊氬姩 |
| `PageDown` | 鍚戜笅婊氬姩 |
| `榧犳爣婊氳疆` | 婊氬姩 transcript |
| `Ctrl-A` (绌鸿緭鍏ユ椂) | 璺冲埌椤堕儴 |
| `Ctrl-E` (绌鸿緭鍏ユ椂) | 璺冲埌搴曢儴 |

### 鏉冮檺瀹℃壒

褰?AI 璇锋眰鏉冮檺鏃讹細

| 蹇嵎閿?| 鍔熻兘 |
|--------|------|
| `鈫慲 / `鈫揱 | 閫夋嫨閫夐」 |
| `Enter` | 纭閫夋嫨 |
| `1`-`7` | 蹇€熼€夋嫨 |
| `v` | 鍒囨崲璇︽儏灞曞紑/鎶樺彔 |
| `Ctrl+O` | 鍒囨崲璇︽儏灞曞紑/鎶樺彔 |
| `PageUp` / `PageDown` | 婊氬姩璇︽儏 |
| `Escape` | 鎷掔粷 |

### 閫氱敤

| 蹇嵎閿?| 鍔熻兘 |
|--------|------|
| `Ctrl+C` | 閫€鍑虹▼搴?|

---

## 馃敡 Slash 鍛戒护

鍦ㄨ緭鍏ユ涓緭鍏?`/` 鏌ョ湅鍙敤鍛戒护锛?
| 鍛戒护 | 鍔熻兘 |
|------|------|
| `/help` | 鏄剧ず甯姪 |
| `/tools` | 鍒楀嚭鍙敤宸ュ叿 |
| `/skills` | 鍒楀嚭宸插姞杞芥妧鑳?|
| `/mcp` | 鍒楀嚭 MCP 鏈嶅姟鍣?|
| `/status` | 鏄剧ず褰撳墠鐘舵€?|
| `/model` | 鏄剧ず褰撳墠妯″瀷 |
| `/model <name>` | 鍒囨崲妯″瀷 |
| `/config-paths` | 鏄剧ず閰嶇疆璺緞 |
| `/history` | 鏄剧ず杈撳叆鍘嗗彶 |
| `/transcript-save <path>` | 淇濆瓨杞綍 |
| `/exit` | 閫€鍑虹▼搴?|

---

## 馃捑 浼氳瘽鎸佷箙鍖?
### 鑷姩淇濆瓨

- 姣?30 绉掕嚜鍔ㄤ繚瀛樺綋鍓嶄細璇?- 淇濆瓨浣嶇疆锛歚~/.pepsi-code/sessions/`
- 鍖呭惈锛氭秷鎭巻鍙层€乼ranscript銆佹潈闄愮姸鎬併€乻kills銆丮CP 閰嶇疆

### 鎵嬪姩鎭㈠

```bash
# 鏌ョ湅鎵€鏈変細璇?python -m pepsicode.main --list-sessions

# 杈撳嚭绀轰緥锛?# Saved sessions:
# 
#   1. [abc123de] 2026-04-05 14:30 - D:\project
#      Messages: 15 | First: 甯垜閲嶆瀯杩欎釜浠ｇ爜
# 
#   2. [def456gh] 2026-04-05 10:15 - D:\project
#      Messages: 8 | First: 瑙ｉ噴涓€涓嬭繖涓嚱鏁?# 
# Total: 2 session(s)

# 鎭㈠浼氳瘽
python -m pepsicode.main --resume abc123de
```

### 浼氳瘽鏂囦欢缁撴瀯

```
~/.pepsi-code/
鈹溾攢鈹€ settings.json          # 鐢ㄦ埛璁剧疆
鈹溾攢鈹€ history.json           # 杈撳叆鍘嗗彶锛堟渶杩?200 鏉★級
鈹溾攢鈹€ permissions.json       # 鏉冮檺瑙勫垯
鈹溾攢鈹€ mcp.json              # MCP 鏈嶅姟鍣ㄩ厤缃?鈹溾攢鈹€ sessions_index.json   # 浼氳瘽绱㈠紩
鈹斺攢鈹€ sessions/             # 浼氳瘽鏁版嵁
    鈹溾攢鈹€ abc123de.json
    鈹斺攢鈹€ def456gh.json
```

---

## 馃洜锔?绠＄悊鍛戒护

### MCP 鏈嶅姟鍣?
```bash
# 鍒楀嚭鎵€鏈?MCP 鏈嶅姟鍣?python -m pepsicode.main mcp list

# 娣诲姞鐢ㄦ埛绾ф湇鍔″櫒
python -m pepsicode.main mcp add myserver -- uvx my-mcp-server

# 娣诲姞椤圭洰绾ф湇鍔″櫒
python -m pepsicode.main mcp add filesystem --project -- npx -y @modelcontextprotocol/server-filesystem .

# 绉婚櫎鏈嶅姟鍣?python -m pepsicode.main mcp remove myserver
```

### Skills

```bash
# 鍒楀嚭鎵€鏈夋妧鑳?python -m pepsicode.main skills list

# 娣诲姞鎶€鑳?python -m pepsicode.main skills add ~/skills/frontend-dev --name frontend-dev

# 绉婚櫎鎶€鑳?python -m pepsicode.main skills remove frontend-dev
```

---

## 馃幆 浣跨敤绀轰緥

### 绀轰緥 1: 绠€鍗曢棶绛?
```
> 瑙ｉ噴涓€涓嬩粈涔堟槸閫掑綊

assistant
  閫掑綊鏄竴绉嶇紪绋嬫妧鏈紝鍑芥暟鍦ㄥ叾涓皟鐢ㄨ嚜韬?..
```

### 绀轰緥 2: 鏂囦欢鎿嶄綔

```
> 璇诲彇 README.md 骞舵€荤粨

tool read_file running
  path=README.md
  
tool read_file ok
  鏂囦欢鍐呭...

assistant
  README.md 鐨勪富瑕佸唴瀹规槸...
```

### 绀轰緥 3: 浠ｇ爜淇敼

```
> 鎶?main.py 涓殑鎵€鏈?print 鏀规垚 logging

Action Required              鈹?Permission
鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
pepsi-code wants to apply a file modification

target: D:\project\main.py

--- a/main.py
+++ b/main.py
@@ -1,5 +1,6 @@
-print("Hello")
+import logging
+logging.info("Hello")

 1 apply once (1) 
 2 allow this file in this turn (2) 
 3 allow all edits in this turn (3) 
 4 always allow this file (4) 
 5 reject once (5) 
 6 reject and send guidance to model (6) 
 7 always reject this file (7)
```

---

## 鈿欙笍 閰嶇疆

### 閰嶇疆鏂囦欢浼樺厛绾?
1. `~/.pepsi-code/settings.json` - 鐢ㄦ埛绾ц缃?2. `~/.pepsi-code/mcp.json` - 鐢ㄦ埛绾?MCP 閰嶇疆
3. `.mcp.json` - 椤圭洰绾?MCP 閰嶇疆
4. 鐜鍙橀噺

### 绀轰緥閰嶇疆

`~/.pepsi-code/settings.json`:

```json
{
  "model": "claude-sonnet-4-20250514",
  "env": {
    "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
    "ANTHROPIC_AUTH_TOKEN": "your-token-here",
    "ANTHROPIC_MODEL": "claude-sonnet-4-20250514"
  }
}
```

---

## 馃И 娴嬭瘯妯″紡

### Mock 妯″紡

鏃犻渶 API 瀵嗛挜锛岀敤浜庢祴璇曞拰寮€鍙戯細

```bash
# Windows
set PEPSI_CODE_MODEL_MODE=mock
python -m pepsicode.main

# Unix/Linux/macOS
export PEPSI_CODE_MODEL_MODE=mock
python -m pepsicode.main
```

Mock 妯″紡浼氾細
- 浣跨敤鍐呯疆鐨勬ā鎷熸ā鍨?- 鍝嶅簲鍥哄畾鐨勬祴璇曟秷鎭?- 鏀寔鎵€鏈夊伐鍏疯皟鐢?- 瀹屾暣娴嬭瘯 TUI 鍔熻兘

### 杩愯娴嬭瘯

```bash
cd py-src
python -m pytest tests/ -v
```

---

## 馃搳 鐘舵€佹寚绀哄櫒

搴曢儴鐘舵€佹爮鏄剧ず锛?
```
tools on | skills on
```

- **tools**: 宸ュ叿绯荤粺鐘舵€侊紙on/off锛?- **skills**: 鎶€鑳界郴缁熺姸鎬侊紙on/off锛?- **bg**: 鍚庡彴浠诲姟鏁伴噺锛堝鏈夛級

---

## 馃悰 鏁呴殰鎺掗櫎

### 闂锛氬惎鍔ㄦ姤閿?"No model configured"

**瑙ｅ喅**: 杩愯瀹夎鍚戝鎴栨墜鍔ㄩ厤缃細

```bash
python -m pepsicode.main --install
```

鎴栧垱寤?`~/.pepsi-code/settings.json`锛?
```json
{
  "model": "your-model",
  "env": {
    "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
    "ANTHROPIC_AUTH_TOKEN": "your-token"
  }
}
```

### 闂锛歍UI 鏄剧ず寮傚父

**瑙ｅ喅**: 纭繚缁堢鏀寔锛?- 鏈€灏?80x24 瀛楃
- 鏀寔 ANSI 杞箟搴忓垪
- Windows 10+ 鎺ㄨ崘浣跨敤 Windows Terminal

### 闂锛氫細璇濇棤娉曟仮澶?
**瑙ｅ喅**: 妫€鏌ヤ細璇濇枃浠讹細

```bash
ls ~/.pepsi-code/sessions/
cat ~/.pepsi-code/sessions_index.json
```

---

## 馃摎 鏇村璧勬簮

- [鏋舵瀯璇存槑](../ts-src/ARCHITECTURE_ZH.md)
- [璐＄尞鎸囧崡](../ts-src/CONTRIBUTING_ZH.md)
- [璺嚎鍥綸(../ts-src/ROADMAP_ZH.md)
- [Claude Code 璁捐妯″紡](../ts-src/CLAUDE_CODE_PATTERNS_ZH.md)

---

## 馃帀 浜彈浣跨敤锛?
pepsicode Python 鏄竴涓交閲忕骇浣嗗姛鑳藉畬鏁寸殑缁堢缂栫爜鍔╂墜銆?
**涓昏鐗规€?*:
- 鉁?瀹屾暣鐨?Agent Loop
- 鉁?寮哄ぇ鐨?TUI 浜や簰
- 鉁?浼氳瘽鎸佷箙鍖栦笌鎭㈠
- 鉁?鏉冮檺绠＄悊绯荤粺
- 鉁?MCP 闆嗘垚
- 鉁?Skills 绯荤粺
- 鉁?闆跺閮ㄤ緷璧?
鏈夐棶棰橈紵娆㈣繋鍙嶉锛?