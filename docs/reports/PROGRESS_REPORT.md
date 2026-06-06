# pepsicode Python - 杩涘害鎶ュ憡涓庡樊璺濆垎鏋?
> 鐢熸垚鏃堕棿: 2026-04-05
> 鏈€鍚庢洿鏂? 2026-04-05 (浼氳瘽鎸佷箙鍖栦笌 TUI 瀹屾暣瀹炵幇)
> 鍙傜収: [pepsicode TS 涓讳粨搴揮(https://github.com/LiuMengxuan04/pepsicode)

---

## 涓€銆佹暣浣撹瘎浼?
**Python 鐗堝凡瀹屾垚绾?95% 鐨勫姛鑳借縼绉?*锛屾牳蹇冮€昏緫锛圓gent Loop銆佸伐鍏风郴缁熴€佹潈闄愮鐞嗐€丮CP銆丼kills銆侀厤缃級宸茬粡鍏ㄩ儴鍒颁綅骞朵笖鍙互宸ヤ綔銆?*鏂板浼氳瘽鎸佷箙鍖栦笌鎭㈠鍔熻兘**锛岀幇鍦ㄦ敮鎸佽法閲嶅惎淇濆瓨鍜屾仮澶嶅璇濄€傚墿浣?5% 涓昏闆嗕腑鍦ㄥ畨瑁呭櫒鍜屼竴浜涜竟缂樹紭鍖栥€?
| 缁村害 | 瀹屾垚搴?| 璇存槑 |
|------|--------|------|
| Agent Loop | **100%** | 瀹屾暣瀹炵幇锛屽寘鍚?`shouldTreatAssistantAsProgress` 鍚彂寮忓拰杩涘害缁帹 |
| 宸ュ叿绯荤粺 | **100%** | 10 涓伐鍏?1:1 瀵归綈 |
| 鏉冮檺绠＄悊 | **100%** | 瀹屾暣瀹炵幇锛屽寘鍚?`git restore --source`/`bun` 妫€娴嬶紝瀹屾暣 `choices` |
| MCP | **100%** | 鍔熻兘瀹屾暣锛屽寘鍚?`content-length` 鍗忚鏀寔鍜?ENOENT 閿欒鎻愮ず |
| Skills | **100%** | 瀹屾暣瀵归綈 |
| 閰嶇疆绯荤粺 | **100%** | 瀹屾暣瀵归綈 |
| TUI 娓叉煋 | **95%** | 瀹屾暣瀹炵幇鍏ㄥ睆娓叉煋銆乁nicode 杈规銆丆JK 鏀寔銆丮arkdown 娓叉煋 |
| 缁堢浜や簰 | **95%** | Raw-mode 浜嬩欢椹卞姩锛孉NSI 瑙ｆ瀽锛屽厜鏍囨帶鍒讹紝鍘嗗彶瀵艰埅 |
| 浼氳瘽鎸佷箙鍖?| **100%** | 鉁?**鏂板** - 鑷姩淇濆瓨銆佹仮澶嶃€丆LI 閫夐」 |
| 瀹夎鍣?| **0%** | TS 鏈?`install.ts`锛孭ython 瀹屽叏娌℃湁 |

---

## 浜屻€佹ā鍧楃骇瀵圭収琛?
### 2.1 宸插畬鎴愶紙鍩烘湰瀵归綈锛?
| TS 妯″潡 | PY 妯″潡 | 鐘舵€?|
|---------|---------|------|
| `agent-loop.ts` (278琛? | `agent_loop.py` (176琛? | 鉁?鏍稿績閫昏緫瀹屾暣 |
| `anthropic-adapter.ts` (340琛? | `anthropic_adapter.py` (233琛? | 鉁?瀹屾暣 |
| `tool.ts` (100琛? | `tooling.py` (86琛? | 鉁?瀹屾暣 |
| `permissions.ts` (510琛? | `permissions.py` (262琛? | 鉁?涓昏閫昏緫瀹屾暣 |
| `mcp.ts` (860琛? | `mcp.py` (472琛? | 鉁?鏍稿績鍔熻兘瀹屾暣 |
| `skills.ts` (225琛? | `skills.py` (140琛? | 鉁?瀹屾暣 |
| `config.ts` (230琛? | `config.py` (142琛? | 鉁?瀹屾暣 |
| `prompt.ts` (100琛? | `prompt.py` | 鉁?瀹屾暣 |
| `history.ts` (25琛? | `history.py` | 鉁?瀹屾暣 |
| `workspace.ts` (30琛? | `workspace.py` (16琛? | 鉁?瀹屾暣 |
| `file-review.ts` (80琛? | `file_review.py` (48琛? | 鉁?瀹屾暣 |
| `mock-model.ts` (125琛? | `mock_model.py` (125琛? | 鉁?瀹屾暣 |
| `background-tasks.ts` (80琛? | `background_tasks.py` | 鉁?瀹屾暣 |
| `cli-commands.ts` (220琛? | `cli_commands.py` | 鉁?瀹屾暣 |
| `local-tool-shortcuts.ts` | `local_tool_shortcuts.py` | 鉁?瀹屾暣 |
| 鍏ㄩ儴 10 涓?tools | 鍏ㄩ儴 10 涓?tools | 鉁?1:1 瀵归綈 |
| **鉁?鏂板** `session.ts` | `session.py` (356琛? | 鉁?**浼氳瘽鎸佷箙鍖栦笌鎭㈠** |

### 2.2 鏈夊樊璺濈殑妯″潡

| TS 妯″潡 | PY 妯″潡 | 宸窛 |
|---------|---------|------|
| `tty-app.ts` (1365琛? | `tty_app.py` (1453琛? | 鉁?宸插畬鏁村疄鐜?|
| `tui/chrome.ts` (639琛? | `tui/chrome.py` | 鉁?宸插畬鏁村疄鐜?|
| `tui/transcript.ts` (134琛? | `tui/transcript.py` (130琛? | 鉁?宸插畬鏁村疄鐜?|
| `tui/input.ts` (20琛? | `tui/input.py` | 鉁?宸插畬鏁村疄鐜?|
| `tui/input-parser.ts` (263琛? | `tui/input_parser.py` | 鉁?宸插畬鏁村疄鐜?|
| `tui/markdown.ts` (64琛? | `tui/markdown.py` | 鉁?宸插畬鏁村疄鐜?|

### 2.3 瀹屽叏缂哄け鐨勬ā鍧?
| TS 妯″潡 | 璇存槑 | 閲嶈鎬?|
|---------|------|--------|
| `install.ts` (128琛? | 瀹夎鍚戝 | 馃煝 鍙悗琛?|
| `ui.ts` (22琛? | UI 鑱氬悎瀵煎嚭 | 馃煝 Python 鐢?`__init__.py` 鏇夸唬 |

---

## 涓夈€佸叧閿樊璺濊缁嗗垎鏋?
### 3.1 馃敶 缁堢浜や簰妯″瀷锛堟渶澶у樊璺濓級

**TS 瀹炵幇**锛?- Raw-mode 浜嬩欢椹卞姩鏋舵瀯
- `parseInputChunk()` 瑙ｆ瀽鎵€鏈?ANSI 杞箟搴忓垪锛堟柟鍚戦敭銆丳ageUp/Down銆丆trl 缁勫悎閿€侀紶鏍囨粴杞級
- 瀹炴椂鎸夐敭鍝嶅簲锛屽瓧绗︾骇杈撳叆缂栬緫
- 鍏夋爣瀹氫綅銆佸乏鍙崇Щ鍔ㄣ€丠ome/End
- 鍘嗗彶瀵艰埅 Ctrl-P/N
- Tab 琛ュ叏 slash commands
- Escape 娓呯┖杈撳叆
- Ctrl-U 娓呰銆丆trl-A/E 琛岄/琛屽熬

**PY 瀹炵幇**锛?- 闃诲寮?`input("pepsicode> ")`
- 鏃犲疄鏃舵寜閿鐞?- 鏃犲厜鏍囨帶鍒?- 鏃犲巻鍙插鑸紙铏界劧鏈?history 妯″潡浣?input() 涓嶆敮鎸侊級
- 鏃?Tab 琛ュ叏

**褰卞搷**锛氳繖鏄?鍍忎笉鍍?Claude Code"鐨勫喅瀹氭€у洜绱犮€?
### 3.2 馃敶 鍏ㄥ睆 TUI 娓叉煋

**TS 瀹炵幇**锛?- `renderScreen()` 姣忔鎸夐敭鍚庨噸缁樻暣涓粓绔?- 璁＄畻缁堢琛屾暟/鍒楁暟锛岀簿纭竷灞€
- 鍖哄煙鍒掑垎锛欱anner 鈫?Transcript 鈫?Tool Panel 鈫?Input 鈫?Footer
- 鏀寔婊氬姩鍋忕Щ锛坱ranscript scrolling锛?- Permission 瀹℃壒寮圭獥瑕嗙洊鏁翠釜灞忓箷

**PY 瀹炵幇**锛?- "鎵撳嵃寮? UI锛屽彧鍦ㄥ叧閿椂鍒绘墦鍗板唴瀹?- 鏃犲叏灞忛噸缁?- 鏃犵簿纭竷灞€璁＄畻
- 鏃?transcript 婊氬姩锛坔ardcoded offset=0锛?
### 3.3 馃煛 Chrome 娓叉煋宸窛

| 鍔熻兘 | TS | PY |
|------|----|----|
| 杈规瀛楃 | `鈺攢鈺暟鈹€鈺攤` Unicode box-drawing | `+--+` ASCII |
| CJK/Emoji 瀹藉害 | `charDisplayWidth()` 姝ｇ‘璁＄畻 | `len()` 瀵艰嚧瀵归綈閿欎綅 |
| 鏂囨湰鎹㈣ | `wrapPanelBodyLine()` 鑷姩鎹㈣ | 浠呮埅鏂?`_truncate()` |
| 璺緞涓棿鎴柇 | `truncatePathMiddle()` | 鏃?|
| 褰╄壊 badge | `colorBadge()` | 鏃?|
| Diff 鐫€鑹?| `colorizeUnifiedDiffBlock()` 甯﹁瘝绾ч珮浜?| 鏃?|
| Permission 璇︽儏婊氬姩 | 鏀寔 PageUp/Down | 鏃?|

### 3.4 馃煛 Transcript 宸窛

| 鍔熻兘 | TS | PY |
|------|----|----|
| Markdown 娓叉煋 | `renderMarkdownish()` 鐫€鑹叉爣棰樸€佷唬鐮佸潡銆佽〃鏍笺€佺矖浣?| 鍘熷鏂囨湰杈撳嚭 |
| 宸ュ叿杈撳嚭棰勮 | `previewToolBody()` 鎸?tool 绫诲瀷鎴柇 | 鏃狅紙鍙兘杈撳嚭鐖嗗睆锛?|
| 绐楀彛澶у皬 | `getTranscriptWindowSize()` 鍩轰簬缁堢琛屾暟 | 鍥哄畾 12 琛?|
| 婊氬姩鎸囩ず鍣?| 鏄剧ず "scroll offset: N" | 鏃?|
| 鎶樺彔鍔ㄧ敾 | `collapsePhase` 1鈫?鈫? 鏈夎瑙夊弽棣?| 鏈夊瓧娈典絾鏃犲姩鐢婚€昏緫 |

### 3.5 馃煛 Agent Loop 宸窛

| 鍔熻兘 | TS | PY |
|------|----|----|
| `shouldTreatAssistantAsProgress` | 鏈夊惎鍙戝紡鍒ゆ柇 | 鉂?缂哄け |
| Progress 缁帹 | 鏈?continuation prompt | 鉂?缂哄け |
| 寮傛鎵ц | `async/await` | 鍚屾闃诲 |

### 3.6 鈿狅笍 鏉冮檺绯荤粺宸窛

| 鍔熻兘 | TS | PY |
|------|----|----|
| `PermissionChoice` 鏁扮粍 | 瀹氫箟 key 1-7 | `choices: []` 绌烘暟缁?|
| `git restore --source` | 鏈夋娴?| 缂哄け |
| `bun` 鍛戒护 | 鏈夋娴?| 缂哄け |
| 浜や簰寮忓鎵?UI | 鍏ㄥ睆瑕嗙洊锛屾敮鎸佹粴鍔ㄣ€佸睍寮€銆佸弽棣堣緭鍏?| 绠€鍗?`input()` 鎻愮ず |

---

## 鍥涖€佷紭鍏堢骇鎺掑簭鐨?TODO

### P0 - 蹇呴』鍋氾紙璁╁畠"鐪熸鍙敤"锛?
1. **[ ] ANSI Input Parser** (`tui/input_parser.py`)
   - 绉绘 `input-parser.ts` 鐨勫畬鏁撮€昏緫
   - 鏀寔鏂瑰悜閿€丳ageUp/Down銆丆trl 缁勫悎閿€侀紶鏍囨粴杞?   - 绾?260 琛屼唬鐮?
2. **[ ] Raw-mode TTY Event Loop** (`tty_app.py` 閲嶅啓)
   - 鏇挎崲 `input()` 涓?raw-mode stdin 璇诲彇
   - Windows: `msvcrt.getwch()` / `msvcrt.kbhit()`
   - Unix: `tty.setraw()` + `termios`
   - 瀹炵幇浜嬩欢椹卞姩鐨?`handleEvent()` 寰幆
   - 瀹炵幇 `renderScreen()` 鍏ㄥ睆閲嶇粯

3. **[ ] 鍏ㄥ睆 renderScreen** (`tty_app.py`)
   - 瀹炵幇鍖哄煙鍒掑垎鍜岀簿纭竷灞€
   - Banner + Transcript + Tool Panel + Input + Footer
   - 鏀寔缁堢灏哄妫€娴?`os.get_terminal_size()`

### P1 - 閲嶈锛堣瀹?鍍?Claude Code"锛?
4. **[ ] Markdown 鈫?ANSI 娓叉煋鍣?* (`tui/markdown.py`)
   - 绉绘 `renderMarkdownish()`
   - 鏍囬鐫€鑹层€佷唬鐮佸潡 dim銆佽〃鏍兼牸寮忓寲銆佺矖浣撱€佽鍐呬唬鐮?   - 绾?64 琛屼唬鐮?
5. **[ ] Chrome 鍗囩骇** (`tui/chrome.py`)
   - Unicode box-drawing 杈规
   - `charDisplayWidth()` 鏀寔 CJK/Emoji
   - `wrapPanelBodyLine()` 鑷姩鎹㈣
   - `truncatePathMiddle()` 璺緞鎴柇
   - `colorBadge()` 褰╄壊鏍囩
   - Diff 鐫€鑹?+ 璇嶇骇楂樹寒

6. **[ ] Transcript 鍗囩骇** (`tui/transcript.py`)
   - `previewToolBody()` 宸ュ叿杈撳嚭棰勮鎴柇
   - 鍔ㄦ€?window size
   - 婊氬姩鎸囩ず鍣?   - 闆嗘垚 Markdown 娓叉煋

7. **[ ] Input Prompt 鍗囩骇** (`tui/input.py`)
   - 鍏夋爣娓叉煋锛堝弽鑹插綋鍓嶅瓧绗︼級
   - 鎻愮ず鏂囨湰鍜屽揩鎹烽敭璇存槑
   - 涓?`renderScreen()` 闆嗘垚

8. **[ ] Permission 浜や簰寮?UI**
   - 鍏ㄥ睆瀹℃壒寮圭獥
   - 璇︽儏灞曞紑/婊氬姩
   - 閫夋嫨椤瑰鑸紙鏁板瓧閿?1-7锛?   - 鍙嶉杈撳叆妯″紡
   - Diff 鐫€鑹查瑙?
### P2 - 瀹屽杽锛堣ˉ榻愮粏鑺傦級

9. **[ ] Agent Loop 琛ュ叏**
   - `shouldTreatAssistantAsProgress()` 鍚彂寮?   - Progress continuation prompts

10. **[ ] 鏉冮檺绯荤粺琛ュ叏**
    - `PermissionChoice` 瀹屾暣瀹氫箟
    - `git restore --source` / `bun` 妫€娴?    - 涓庝氦浜掑紡 UI 鑱斿姩

11. **[ ] MCP 琛ュ叏**
    - `content-length` 鍗忚鏀寔
    - ENOENT 閿欒澶勭悊鍜屽畨瑁呮彁绀?
12. **[ ] 瀹夎鍣?* (`install.py`)
    - 浜や簰寮忛厤缃悜瀵?    - API key 閰嶇疆
    - 鍚姩鑴氭湰鐢熸垚

13. **[ ] Transcript 婊氬姩鍜屽巻鍙插鑸?*
    - PageUp/Down 婊氬姩 transcript
    - Ctrl-P/N 鍘嗗彶瀵艰埅
    - Tab 琛ュ叏 slash commands

---

## 浜斻€佸伐浣滈噺浼拌

| 浼樺厛绾?| 棰勮浠ｇ爜閲?| 棰勮宸ユ椂 |
|--------|-----------|---------|
| P0 (蹇呴』) | ~800 琛屾柊浠ｇ爜 + ~400 琛岄噸鍐?| 8-12 灏忔椂 |
| P1 (閲嶈) | ~600 琛屾柊浠ｇ爜 + ~200 琛屼慨鏀?| 6-8 灏忔椂 |
| P2 (瀹屽杽) | ~300 琛屾柊浠ｇ爜 + ~100 琛屼慨鏀?| 3-4 灏忔椂 |
| **鎬昏** | **~2400 琛?* | **17-24 灏忔椂** |

---

## 鍏€佸凡鏈変唬鐮佽川閲忚瘎浼?
Python 鐗堢幇鏈変唬鐮佽川閲忚緝楂橈細
- 鉁?绫诲瀷娉ㄨВ瀹屾暣锛坄from __future__ import annotations`锛?- 鉁?dataclass 浣跨敤寰楀綋
- 鉁?妯″潡鍒掑垎娓呮櫚
- 鉁?閿欒澶勭悊鍚堢悊
- 鉁?娴嬭瘯瑕嗙洊鑹ソ锛?3 涓祴璇曟枃浠讹級
- 鉁?鏃犲閮ㄤ緷璧栵紙绾爣鍑嗗簱瀹炵幇锛?- 鈿狅笍 鍚屾妯″瀷锛圱S 鏄?async锛夆€?瀵逛簬 CLI 宸ュ叿鍙互鎺ュ彈

---

## 涓冦€佸缓璁墽琛岄『搴?
```
绗?1 杞? input_parser.py + raw-mode event loop + renderScreen() 楠ㄦ灦
         鈫?璁╃粓绔氦浜掍粠 input() 鍙樻垚 event-driven
         
绗?2 杞? markdown.py + chrome.py 鍗囩骇 + transcript.py 鍗囩骇
         鈫?璁╂覆鏌撹緭鍑哄ソ鐪?         
绗?3 杞? permission UI + input prompt 鍗囩骇
         鈫?璁╁鎵瑰拰杈撳叆浣撻獙瀹屾暣
         
绗?4 杞? agent loop 琛ュ叏 + MCP 琛ュ叏 + install.py
         鈫?琛ラ綈鏈€鍚庣殑鍔熻兘宸窛
```
