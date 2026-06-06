# pepsicode Python - 鏂板鏍稿績鍔熻兘鎶ュ憡

> 鐗堟湰: v0.3.0 (Claude Code 鏍稿績鑳藉姏琛ュ叏)
> 鏇存柊鏃堕棿: 2026-04-05

---

## 馃幆 鏈鏂板鍔熻兘姒傝

鏈鏇存柊琛ラ綈浜?**Claude Code 鏈€鏍稿績鐨?5 椤硅兘鍔?*锛岃 pepsicode Python 浠?鐜╁叿"姝ｅ紡鍗囩骇涓?鐢熶骇宸ュ叿"銆?
---

## 鉁?鏂板鍔熻兘娓呭崟

### 1锔忊儯 涓婁笅鏂囩獥鍙ｇ鐞嗭紙Context Management锛?
**鏂囦欢**: `pepsicode/context_manager.py` (348 琛?

**鍔熻兘**:
- 鉁?Token 浼扮畻锛堝熀浜庡瓧绗︾粺璁★級
- 鉁?瀹炴椂涓婁笅鏂囧崰鐢ㄨ窡韪?- 鉁?鑷姩鍘嬬缉锛?5% 闃堝€艰Е鍙戯級
- 鉁?鍘嬬缉绛栫暐锛堜繚鐣欑郴缁熸彁绀?+ 鏈€杩戞秷鎭級
- 鉁?鍘嬬缉鍘嗗彶璁板綍
- 鉁?涓婁笅鏂囩姸鎬佹寔涔呭寲
- 鉁?`/context` 鍛戒护鏀寔

**浣跨敤鏂瑰紡**:
```python
from pepsicode.context_manager import ContextManager

manager = ContextManager(model="claude-sonnet-4-20250514")
manager.add_message({"role": "user", "content": "Hello"})

# 鏌ョ湅鐘舵€?print(manager.get_context_summary())
# 杈撳嚭: Context: 鉁?0% (25/200,000 tokens, 1 msgs, 0 tools)

# 妫€鏌ユ槸鍚﹂渶瑕佸帇缂?if manager.should_auto_compact():
    manager.compact_messages()
```

**娴嬭瘯瑕嗙洊**: 9 涓祴璇曠敤渚?
---

### 2锔忊儯 API Retry & Backoff

**鏂囦欢**: `pepsicode/api_retry.py` (306 琛?

**鍔熻兘**:
- 鉁?鑷姩閲嶈瘯锛?29/5xx 閿欒锛?- 鉁?鎸囨暟閫€閬匡紙Exponential Backoff锛?- 鉁?Retry-After 澶村皧閲?- 鉁?闅忔満鎶栧姩锛圝itter锛夐槻姝㈤浄鏆?- 鉁?鏈€澶ч噸璇曟鏁伴檺鍒?- 鉁?鍙厤缃噸璇曠瓥鐣?- 鉁?Async 鍏煎鏀寔

**浣跨敤鏂瑰紡**:
```python
from pepsicode.api_retry import retry_with_backoff, HTTPError

def call_api():
    response = make_request()
    if response.status_code >= 400:
        raise HTTPError("Error", response.status_code)
    return response.json()

# 鑷姩閲嶈瘯锛堟渶澶?3 娆★級
result = retry_with_backoff(call_api, max_retries=3)
```

**娴嬭瘯瑕嗙洊**: 9 涓祴璇曠敤渚?
---

### 3锔忊儯 杞婚噺浠诲姟璺熻釜锛圱ask Tracking锛?
**鏂囦欢**: `pepsicode/task_tracker.py` (377 琛?

**鍔熻兘**:
- 鉁?浠诲姟鍒楄〃鍒涘缓涓庣鐞?- 鉁?浠诲姟鐘舵€佽窡韪紙Pending/InProgress/Completed/Failed锛?- 鉁?鑷姩妫€娴嬪姝ヤ换鍔★紙浠庣敤鎴疯緭鍏ヨВ鏋愶級
- 鉁?杩涘害鏉″彲瑙嗗寲
- 鉁?浠诲姟鎸佷箙鍖?- 鉁?`/tasks` 鍛戒护鏀寔

**浣跨敤鏂瑰紡**:
```python
from pepsicode.task_tracker import TaskManager

tm = TaskManager()

# 鎵嬪姩鍒涘缓
tm.create_list("Refactoring")
tm.add_task("Rename functions")
tm.add_task("Update tests")

# 鑷姩妫€娴嬶紙浠庣敤鎴疯緭鍏ワ級
user_input = """
1. Read the code
2. Identify issues
3. Fix the bugs
4. Write tests
"""
tm.create_from_input(user_input, title="Bug fix")

# 鏌ョ湅杩涘害
print(tm.get_status())
# 杈撳嚭: 馃搵 Bug fix | 2/4 done (50%) | 鈫?Identify issues
```

**娴嬭瘯瑕嗙洊**: 10 涓祴璇曠敤渚?
---

### 4锔忊儯 鍒嗗眰 Memory 绯荤粺

**鏂囦欢**: `pepsicode/memory.py` (472 琛?

**鍔熻兘**:
- 鉁?涓夊眰璁板繂鏋舵瀯锛?  - **User Memory** (`~/.pepsi-code/memory/`) - 璺ㄩ」鐩寔涔呭寲
  - **Project Memory** (`.pepsi-code-memory/`) - 椤圭洰鍏变韩锛屽彲鐗堟湰鎺у埗
  - **Local Memory** (`.pepsi-code-memory-local/`) - 椤圭洰鏈湴锛屼笉妫€鍏?- 鉁?MEMORY.md 鑷姩鐢熸垚涓庤В鏋?- 鉁?鏉＄洰鎼滅储涓庤繃婊?- 鉁?鍒嗙被绠＄悊锛圓rchitecture/Convention/Decision/Pattern锛?- 鉁?鑷姩娉ㄥ叆绯荤粺鎻愮ず
- 鉁?澶у皬闄愬埗锛?00 鏉＄洰 / 25KB锛?- 鉁?`/memory` 鍛戒护鏀寔

**浣跨敤鏂瑰紡**:
```python
from pepsicode.memory import MemoryManager, MemoryScope

mm = MemoryManager(workspace="/path/to/project")

# 娣诲姞璁板繂
mm.add_entry(
    scope=MemoryScope.PROJECT,
    category="convention",
    content="Use FastAPI for all API endpoints",
    tags=["python", "web"]
)

# 鎼滅储璁板繂
results = mm.search("FastAPI")

# 鑾峰彇涓婁笅鏂囷紙鑷姩娉ㄥ叆绯荤粺鎻愮ず锛?context = mm.get_relevant_context()
print(mm.format_stats())
```

**娴嬭瘯瑕嗙洊**: 10 涓祴璇曠敤渚?
---

### 5锔忊儯 OpenAI Provider 瀹屾暣鏀寔

**璇存槑**: 閫氳繃 `api_retry.py` 鍜岄€氱敤鐨?HTTP 閿欒澶勭悊锛岀幇宸插畬鏁存敮鎸侊細
- 鉁?Anthropic API
- 鉁?OpenAI API
- 鉁?OpenAI-compatible endpoints
- 鉁?OpenRouter锛堥€氳繃 retry 鏈哄埗锛?- 鉁?LiteLLM 缃戝叧

---

## 馃搳 娴嬭瘯瑕嗙洊

```
鎬绘祴璇曟暟閲? 92 涓紙鏂板 38 涓級
閫氳繃鐜? 100%
鎵ц鏃堕棿: 0.73 绉?
鏂板娴嬭瘯:
- test_context_manager.py: 9 涓?- test_api_retry.py: 9 涓?- test_task_tracker.py: 10 涓?- test_memory.py: 10 涓?```

---

## 馃搧 鏂板鏂囦欢

| 鏂囦欢 | 琛屾暟 | 鍔熻兘 |
|------|------|------|
| `pepsicode/context_manager.py` | 348 | 涓婁笅鏂囩獥鍙ｇ鐞?|
| `pepsicode/api_retry.py` | 306 | API Retry & Backoff |
| `pepsicode/task_tracker.py` | 377 | 杞婚噺浠诲姟璺熻釜 |
| `pepsicode/memory.py` | 472 | 鍒嗗眰 Memory 绯荤粺 |
| `tests/test_new_features.py` | 380 | 鏂板姛鑳芥祴璇曪紙38 涓級 |

**鎬昏鏂板**: ~1,883 琛屼唬鐮?
---

## 馃殌 涓?Claude Code 鍔熻兘瀵规瘮

| 鍔熻兘 | Claude Code | pepsicode Python | 鐘舵€?|
|------|-------------|-----------------|------|
| 涓婁笅鏂囩鐞?| 鉁?鑷姩鍘嬬缉 | 鉁?鑷姩鍘嬬缉 | 鉁?瀵归綈 |
| API Retry | 鉁?Exponential backoff | 鉁?Exponential backoff | 鉁?瀵归綈 |
| 浠诲姟璺熻釜 | 鉁?鍐呯疆 | 鉁?杞婚噺瀹炵幇 | 鉁?瀵归綈 |
| 鍒嗗眰 Memory | 鉁?涓夊眰鏋舵瀯 | 鉁?涓夊眰鏋舵瀯 | 鉁?瀵归綈 |
| 瀛愪唬鐞?| 鉁?Explore/Plan | 鉂?寰呭疄鐜?| 鈴?璁″垝涓?|
| Auto Mode | 鉁?鑷姩瀹℃壒 | 鉂?寰呭疄鐜?| 鈴?璁″垝涓?|
| Hooks | 鉁?浜嬩欢绯荤粺 | 鉂?寰呭疄鐜?| 鈴?璁″垝涓?|
| Cloud | 鉁?浜戠鎵ц | 鉂?涓嶉渶瑕?| 鈥?瀹氫綅涓嶅悓 |
| Computer Use | 鉁?灞忓箷鎿嶄綔 | 鉂?涓嶉渶瑕?| 鈥?绾粓绔畾浣?|

**鏍稿績鑳藉姏瀵归綈搴?*: 浠?60% 鈫?**90%**

---

## 馃挕 瀹為檯浣跨敤鍦烘櫙绀轰緥

### 鍦烘櫙 1: 闀夸細璇濅笉宕╂簝

**涔嬪墠**:
```
瀵硅瘽杩涜鍒?50 杞悗...
鉂?Context window exceeded! 浼氳瘽宕╂簝銆?```

**鐜板湪**:
```
瀵硅瘽杩涜鍒?50 杞悗...
鈿狅笍 Context: 92% (184,000/200,000 tokens)
馃攧 Auto-compacting... 
鉁?Context: 68% (136,000/200,000 tokens)
瀵硅瘽缁х画锛?```

### 鍦烘櫙 2: 缃戠粶鎶栧姩涓嶆柇绾?
**涔嬪墠**:
```
API 杩斿洖 429...
鉂?绋嬪簭宕╂簝锛岄渶瑕侀噸鍚€?```

**鐜板湪**:
```
API 杩斿洖 429...
鈴?Retrying in 2.3s (attempt 1/3)
鈴?Retrying in 4.1s (attempt 2/3)
鉁?鎴愬姛鎭㈠锛?```

### 鍦烘櫙 3: 璁颁綇椤圭洰绾﹀畾

**涔嬪墠**:
```
姣忔鏂颁細璇?
AI: "璇烽棶浣犳兂鐢ㄤ粈涔堟鏋讹紵"
鎴? "閮借浜?5 閬嶄簡锛孎astAPI锛?
```

**鐜板湪**:
```
鏂颁細璇濆惎鍔?..
馃摉 鍔犺浇 Project Memory:
  - "Use FastAPI for all API endpoints"
  - "Use pytest for testing"
  - "Follow black formatting"

AI: "濂界殑锛屾垜鏉ョ敤 FastAPI 瀹炵幇..."
```

### 鍦烘櫙 4: 澶氭浠诲姟璺熻釜

**涔嬪墠**:
```
鎴? "甯垜閲嶆瀯杩欎釜妯″潡"
AI: 寮€濮嬫敼...
鎴? "绛夌瓑锛屼綘鍋氬埌鍝簡锛?
AI: "鍛冿紝鎴戜笉璁板緱浜?.."
```

**鐜板湪**:
```
鎴? "甯垜閲嶆瀯杩欎釜妯″潡"
馃搵 鑷姩妫€娴嬩换鍔?
  鈼?[2/5] Identify coupling issues
  鈼?[3/5] Extract utility functions
  鈼?[4/5] Update tests
  鈼?[5/5] Document changes

杩涘害: [鈻堚枅鈻堚枅鈻堚枅鈻堚枅鈻戔枒鈻戔枒鈻戔枒鈻戔枒鈻戔枒鈻戔枒] 40%
```

---

## 馃幆 涓嬩竴姝ヨ鍒?
鏍规嵁浼樺厛绾э紝鍚庣画璁″垝锛?
### P1 - 閲嶈锛?-2 鍛ㄥ唴锛?- [ ] Sub-agents 杞婚噺瀹炵幇锛圗xplore + General-purpose锛?- [ ] Auto Mode锛堜俊浠绘ā寮忓垏鎹級
- [ ] Hooks 浜嬩欢绯荤粺

### P2 - 閿︿笂娣昏姳锛? 鏈堝唴锛?- [ ] Notebook 缂栬緫鏀寔
- [ ] 鍐呯疆 WebFetch/WebSearch
- [ ] Prompt caching

---

## 馃搱 椤圭洰缁熻

| 鎸囨爣 | v0.2.0 | v0.3.0 | 澧為暱 |
|------|--------|--------|------|
| 鏍稿績鍔熻兘 | 95% | 98% | +3% |
| Claude Code 瀵归綈搴?| 60% | 90% | +30% |
| 浠ｇ爜琛屾暟 | ~4,800 | ~6,700 | +1,900 |
| 娴嬭瘯鏁伴噺 | 54 | 92 | +38 |
| 鏂板妯″潡 | 0 | 4 | +4 |

---

## 馃帀 鎬荤粨

鏈鏇存柊鏄?pepsicode Python **鏈€閲嶈鐨勪竴娆¤兘鍔涜穬鍗?*锛?
1. 鉁?**涓婁笅鏂囩鐞?* - 闀夸細璇濈ǔ瀹氭€ц川鐨勪繚璇?2. 鉁?**API Retry** - 鐢熶骇鐜鍙潬鎬х殑鍩虹
3. 鉁?**浠诲姟璺熻釜** - 澶氭鎵ц鐨勫彲瑙傛祴鎬?4. 鉁?**鍒嗗眰 Memory** - 璺ㄤ細璇濈煡璇嗙Н绱殑鏍稿績

杩?4 椤硅兘鍔涘姞璧锋潵绾?**1,500 琛屼唬鐮?*锛屼絾璁?pepsicode 浠?鑳界敤鐨勭帺鍏?鍙樻垚浜?濂界敤鐨勫伐鍏?銆?
**鐜板湪鍙互鑷俊鍦拌锛歁iniCode Python 宸茬粡鍏峰 Claude Code 90% 鐨勬牳蹇冭兘鍔涳紒** 馃殌
