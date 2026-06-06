# CoreCoder 鏋舵瀯瀵归綈浼樺寲鎶ュ憡

> 鍙傝€?`CoreCoder-main` 鐨?7 绡?Claude Code 鏋舵瀯鏂囩珷锛屽 pepsicode锛圡iniCode Python锛?> 杩涜鐨勪竴杞牳蹇冩灦鏋勪紭鍖栥€傜洰鏍囷細**鏇翠綆鐨勬劅鐭ュ欢杩熴€佹洿鐪?token銆佹洿寮洪煣鎬с€佸彲鐢ㄧ殑瀛?Agent 濮旀淳**锛?> 鍚屾椂淇濇寔銆岄浂杩愯鏃朵緷璧栥€嶃€?
**缁撴灉**锛氬叏閮?4 涓柟鍚戣惤鍦板苟閫氳繃娴嬭瘯 鈥斺€?`166 passed, 2 skipped`锛堝師 144 + 鏂板 22 椤归拡瀵规€ф祴璇曪級銆?鍙︽湁涓€杞鎶楀紡浠ｇ爜瀹℃煡锛岀‘璁ゅ苟淇浜嗗疄鐜拌繃绋嬩腑寮曞叆鐨?6 涓湡瀹?bug銆?
---

## 鑳屾櫙

pepsicode 鏄竴涓豢 Claude Code 鐨勭粓绔紪绋?Agent銆備笌鍙傝€冭摑鏈?`CoreCoder`锛堢敤 ~1400 琛?Python
鎻愮偧 Claude Code 51 涓囪 TS 鐨勬牳蹇冩ā寮忥級閫愭潯姣斿鍚庯紝鍙戠幇瀹冨凡瀹炵幇浜嗕笉灏戞ā寮忥紙鍞竴鍖归厤 search-replace
缂栬緫銆佸嵄闄╁懡浠ゅ垎绾?+ 澶氱骇鏉冮檺銆佷細璇濇寔涔呭寲銆丮CP/skills銆侀€€閬?鎶栧姩閲嶈瘯銆乣<progress>/<final>` 寰幆锛夛紝
浣嗗湪 4 涓珮浠峰€肩淮搴︿笂钀藉悗浜?CoreCoder 鐨勬渶灏忓疄鐜般€傛湰娆′紭鍖栦慨澶嶈繖浜涘樊璺濄€?
---

## 瀹炵幇鐨勪紭鍖?
### 1. 骞惰宸ュ叿鎵ц锛堟枃绔?2 / 5锛?
**闂**锛氶€傞厤鍣ㄥ叏绋嬮樆濉烇紝宸ュ叿鍦?`agent_loop` 涓覆琛岄€愪釜鎵ц銆俙ToolCapability.CONCURRENCY_SAFE`
鏍囧織铏藉凡瀹氫箟鍗存槸姝讳唬鐮侊紝浠庢湭鎺ュ埌浠讳綍宸ュ叿涓娿€傝繛 CoreCoder 鏈€灏忕増閮界敤浜?ThreadPool銆?
**鏀瑰姩**锛?- `tooling.py`锛氱粰 `ToolDefinition` 澧炲姞 `concurrency_safe: bool = False` 瀛楁銆?- 鎶?7 涓彧璇诲伐鍏锋爣璁颁负瀹夊叏锛歚read_file`銆乣grep_files`銆乣list_files`銆乣file_tree`銆?  `find_symbols`銆乣find_references`銆乣get_ast_info`銆?- `agent_loop._execute_calls_in_order`锛氱敤 `ThreadPoolExecutor`锛堜笂闄?8锛夊苟琛屾墽琛?*杩炵画鐨?*
  鍙宸ュ叿鎵规锛涘啓/鎵ц绫诲伐鍏蜂粛鐙崰涓茶銆?*缁撴灉涓ユ牸鎸夊師濮嬭皟鐢ㄩ『搴忚繑鍥?*锛屾秷鎭巻鍙蹭繚鎸佺‘瀹氭€с€?
**鏀剁泭**锛氫竴娆¤繑鍥炲涓?`read_file`/`grep` 鏃讹紝浠庛€屼覆琛岀疮鍔犮€嶅彉涓恒€屽苟琛?鈮?max(鍗曚釜)銆嶃€?
---

### 2. HISTORY_SNIP 宸ュ叿杈撳嚭鎴柇锛堟枃绔?4 绗?1 灞傦級

**闂**锛歚grep_files`/`run_command` 鎶婂畬鏁磋緭鍑虹亴杩涗笂涓嬫枃锛沗max_result_size_chars`
瀹氫箟浜嗗嵈浠庝笉鐢熸晥銆傝繖鏄渶寤変环銆佹渶楂樹环鍊肩殑鍘嬬缉灞傦紙鏃犻渶 LLM 璋冪敤锛夈€?
**鏀瑰姩**锛?- `agent_loop._snip_tool_outputs`锛氭瘡娆℃ā鍨嬭皟鐢ㄥ墠锛岃鍓緝鏃х殑澶у潡 `tool_result`
  锛堜繚鐣欏ご 12 琛?+ 灏?8 琛岋紝涓棿鏇挎崲涓?`[snipped N lines]`锛夈€?- 淇濇姢鏈€杩?3 鏉＄粨鏋滐紙妯″瀷闇€瑕佺湅鍒板垰鍋氫簡浠€涔堬級銆佸箓绛夛紙宸叉埅鏂殑涓嶅啀澶勭悊锛夈€?*缁濅笉涓㈠純鏁存潯缁撴灉**
  锛堝彧缂╃煭 content锛屼繚璇?tool_use/tool_result 閰嶅涓嶈鐮村潖锛夈€?
---

### 3. 涓婁笅鏂囧帇缂?+ 鐪熷疄 token 鐢ㄩ噺锛堟枃绔?4 绗?2/3 灞傦級

**闂**锛歚compact_messages` 鍙細銆屼涪寮冦€嶆渶鏃ф秷鎭紝鏃?LLM 鎽樿锛岄暱浼氳瘽浼氫涪澶辨枃浠惰矾寰?鍐崇瓥/
鏈В鍐抽敊璇€備笂涓嬫枃缁熻鐢?`chars/4` 鍚彂寮忥紝涓㈠純浜?API 宸茶繑鍥炵殑鐪熷疄 `usage`銆?
**鏀瑰姩**锛?- `anthropic_adapter`锛氭崟鑾?API 杩斿洖鐨勭湡瀹?`usage`锛坄last_usage`锛夛紝鏂板 `summarize()` 鏂规硶
  锛堢敤 fallback 鎴栦富妯″瀷鎶婃棫瀵硅瘽鍘嬫垚鍏抽敭浜嬪疄锛夈€?- `context_manager`锛?  - `update_usage()` + `get_stats()` 浼樺厛浣跨敤鐪熷疄 input token 鏁般€?  - `compact_messages` 澧炲姞 LLM 鎽樿璺緞锛坄summarizer` 鍥炶皟锛夛紝淇濈暀**鏂囦欢璺緞 / 宸ュ叿浣跨敤 /
    鍐崇瓥 / 鏈В鍐抽敊璇?*锛涙棤妯″瀷鏃跺洖閫€鍒版鍒欏惎鍙戝紡 `_heuristic_summary`銆?- `main.py` / `tty_app.py`锛氭妸 `model.summarize` 鎺ュ埌 ContextManager 鐨?`summarizer`锛?  骞跺湪 TTY 涓讳氦浜掕矾寰勪篃涓茶仈浜?ContextManager锛堟鍓嶄粎闈炰氦浜掕矾寰勬湁锛夈€?
---

### 4. API 闊ф€э細婧㈠嚭閲嶈瘯 + 鍥為€€妯″瀷锛堟枃绔?2 缁嗚妭 3锛?
**闂**锛氶亣 400/413銆岃姹傝繃澶с€嶇洿鎺ヨ繑鍥炶嚧鍛介敊璇€岄潪鍘嬬缉閲嶈瘯锛?29 杩囪浇鏃舵棤 fallback 妯″瀷銆?
**鏀瑰姩**锛?- `anthropic_adapter`锛氭柊澧?`ContextOverflowError` / `ServiceOverloadError`銆?  - 400/413 鈫?鎶?`ContextOverflowError`銆?  - 529 鈫?鍒囨崲 `fallbackModel`锛堣嫢閰嶇疆锛夛紝浠嶅け璐ユ墠鎶?`ServiceOverloadError`銆?- `agent_loop`锛氭崟鑾?`ContextOverflowError` 鈫?寮哄埗鍘嬬缉骞堕噸璇曪紙鏈€澶?3 娆★級锛岃€岄潪閫€鍑烘湰杞€?- `config.py`锛氭柊澧?`fallbackModel` 閰嶇疆椤癸紙鐜鍙橀噺 `ANTHROPIC_FALLBACK_MODEL` /
  `PEPSI_CODE_FALLBACK_MODEL` / settings.json `fallbackModel`锛夈€?
---

### 棰濆锛氱郴缁熸彁绀?+ 瀛?Agent

**娌荤悊瑙勫垯鍧楁敼涓?opt-in**锛堟枃绔?2 缁嗚妭 1锛?- 姝ゅ墠姣忔璇锋眰閮藉己鍒舵敞鍏?~70 琛屻€孍ngineering Governance Rules銆嶏紙鎸囧悜澶栭儴璺緞銆佸惈闈炴硶 Python 绀轰緥锛夛紝
  璺ㄩ」鐩笉鍚堥€備笖姣忔鑰?token銆?- 鐜版敼涓哄彲閫夛細`governance` 閰嶇疆椤规垨 `PEPSI_CODE_GOVERNANCE=1` 鎵嶅惎鐢ㄣ€?- 绯荤粺鎻愮ず鏂板**鍔ㄦ€佺幆澧冩**锛歄S銆丳ython 鐗堟湰銆丆WD銆乬it 鍒嗘敮銆侀《灞傜洰褰曟潯鐩€?
**瀛?Agent 鏆撮湶涓?`task` 宸ュ叿**锛堟枃绔?6锛?- `sub_agents.py` 涓殑 Explore/Plan/General 姝ゅ墠鍙湪娴嬭瘯閲岃寮曠敤锛屾ā鍨嬫棤娉曡皟鐢ㄣ€?- 鏂板 `tools/task.py`锛歚task` 宸ュ叿锛屾ā鍨嬪彲濮旀淳闅旂涓婁笅鏂囩殑瀛愪换鍔★紝鍙洖浼犳憳瑕併€?  - 瀛?Agent 鐢ㄥ彈闄愬伐鍏烽泦锛堝彧璇?/ general 鍚啓鎵ц锛夛紝**绂佹閫掑綊**锛堝瓙娉ㄥ唽琛ㄤ笉鍚?task 宸ュ叿鏈韩锛夈€?
---

## 瀵规姉寮忓鏌ュ彂鐜板苟淇鐨?6 涓湡瀹?bug

瀹炵幇鍚庣敤涓€涓?review 鈫?verify 宸ヤ綔娴佸鏀瑰姩鍋氬鎶楀紡澶嶅锛岀‘璁ゅ苟鍏ㄩ儴淇锛?
| 涓ラ噸搴?| 闂 | 淇 |
|---|---|---|
| 楂?| `read_file._file_cache` 骞惰涓嬬殑瀛楀吀绔炴€侊紙杩唬鏃惰鏀?/ 閲嶅 del KeyError锛?| 鍔?`threading.Lock` 淇濇姢璇诲彇/娓呯悊/鍐欏叆 |
| 楂?| `compact_messages` 涓嶉噸缃檲鏃?`actual_input_tokens`锛屽帇缂╁悗浠嶅垽瀹氳秴闄?| 鍘嬬缉鍚?`actual_input_tokens = 0` |
| 涓?| 529 鍦?`_send` 鍐呰褰撴櫘閫?5xx 閲嶈瘯 5 娆℃墠璧?fallback | `_should_retry_status` 鎺掗櫎 529锛屼氦缁?`next()` |
| 涓?| 浣嶇疆閰嶅瀵艰嚧 `tool_result` 鎴愬鍎?鈫?瑙﹀彂鏂?400 | 鏀逛负鎸?`toolUseId` 閰嶅锛屼慨澶嶅鍎?|
| 浣?| 绌烘搷浣滃帇缂╀粛鎻掑叆绌?marker銆乣messages_removed` 涓鸿礋 | 鏃犲彉鏇存椂鍘熸牱杩斿洖銆佽鏁?clamp |
| 浣?| 鍘嬬缉 marker 璺ㄨ疆绱Н鎴愬亣 system prompt | marker 鍗曠嫭鏍囪锛屼笉娣峰叆鐪熷疄 system prompt |

---

## 娑夊強鐨勬枃浠?
**鏍稿績**
- `pepsicode/agent_loop.py` 鈥?骞惰鎵ц銆丠ISTORY_SNIP銆佹孩鍑洪噸璇曘€乽sage 鎹曡幏
- `pepsicode/anthropic_adapter.py` 鈥?婧㈠嚭/杩囪浇寮傚父銆乫allback 妯″瀷銆乣summarize()`銆乽sage
- `pepsicode/context_manager.py` 鈥?鐪熷疄 token銆丩LM/鍚彂寮忔憳瑕併€侀厤瀵逛慨澶嶃€乵arker 娌荤悊
- `pepsicode/config.py` 鈥?`fallbackModel`銆乣governance` 閰嶇疆椤?- `pepsicode/prompt.py` 鈥?娌荤悊鍧?opt-in銆佸姩鎬佺幆澧冩
- `pepsicode/tooling.py` 鈥?`concurrency_safe` 瀛楁
- `pepsicode/tools/task.py` 鈥?鏂板瀛?Agent 濮旀淳宸ュ叿

**宸ュ叿鏍囪**锛歚read_file.py`銆乣grep_files.py`銆乣list_files.py`銆乣file_tree.py`銆乣code_nav.py`

**鍏ュ彛涓茶仈**锛歚pepsicode/main.py`銆乣pepsicode/tty_app.py`銆乣pepsicode/tools/__init__.py`

**娴嬭瘯**锛歚tests/test_optimizations.py`锛堟柊澧?22 椤癸級

---

## 楠岃瘉

```bash
cd pepsicode
python -m pytest -q          # 166 passed, 2 skipped
```

鏂板娴嬭瘯瑕嗙洊锛氬苟琛屾墽琛?椤哄簭淇濇寔銆丠ISTORY_SNIP 骞傜瓑涓庝繚鎶ゃ€佹孩鍑哄帇缂╅噸璇曘€佺湡瀹?token 瑕嗙洊銆?LLM/鍚彂寮忔憳瑕併€佹不鐞?opt-in銆乀ask 宸ュ叿銆佷互鍙?6 涓?bug 鐨勫洖褰掓祴璇曘€?
---

## 閰嶇疆绀轰緥

`~/.pepsi-code/settings.json`锛?
```json
{
  "model": "claude-opus-4-20250514",
  "fallbackModel": "claude-sonnet-4-20250514",
  "governance": false,
  "env": {
    "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
    "ANTHROPIC_API_KEY": "<your-key>"
  }
}
```
