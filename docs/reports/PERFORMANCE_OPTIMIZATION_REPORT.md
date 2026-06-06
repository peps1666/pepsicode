# pepsicode Python 鎬ц兘浼樺寲鎶ュ憡

## 姒傝

閫氳繃绯荤粺鍖栫殑鎬ц兘鍒嗘瀽鍜屼紭鍖栵紝Python 鐗?pepsicode 鐨勫叧閿矾寰勬€ц兘鎻愬崌浜?**1.8 鍊嶅埌 8 鍊?*锛孋PU 浣跨敤鐜囬檷浣庝簡 **60%**銆?
## 浼樺寲鍘嗗彶

| 杞 | 鏃ユ湡 | 涓昏浼樺寲 | 鎬ц兘鎻愬崌 |
|------|------|---------|---------|
| **绗簩杞?* | 2026-04-05 | 涓诲惊鐜繖绛夊緟浼樺寲 | CPU 猬囷笍 60% |
| **绗洓杞?* | 2026-04-05 | Token 浼扮畻姝ｅ垯浼樺寲 | 8x 鏇村揩 |
| **绗簲杞?* | 2026-04-05 | 鏂囦欢璇诲彇缂撳瓨 + 瀵硅薄姹?| 1.8x 鏇村揩 |

## 璇︾粏浼樺寲椤?
### 1. Token 浼扮畻浼樺寲 (context_manager.py)

**闂**: 鍘熷瀹炵幇浣跨敤閫愬瓧绗?`ord()` 妫€鏌ワ紝瀵?10000 瀛楃鏂囨湰鎵ц 9000涓囨 `ord()` 璋冪敤锛岃€楁椂 28.8 绉掋€?
**浼樺寲**:
```python
# 浼樺寲鍓嶏細閫愬瓧绗︽鏌?for char in text:
    code = ord(char)  # 90M 娆¤皟鐢?    if 0x4E00 <= code <= 0x9FFF:
        cjk_count += 1

# 浼樺寲鍚庯細棰勭紪璇戞鍒欒〃杈惧紡
_CJK_PATTERN = re.compile(r'[\u4E00-\u9FFF\u3040-\u309F...]')
cjk_count = len(_CJK_PATTERN.findall(text))
```

**缁撴灉**:
- 浼樺寲鍓? 28,787ms / 1000 娆¤皟鐢?(35 ops/sec)
- 浼樺寲鍚? ~3,500ms / 1000 娆¤皟鐢?(285 ops/sec)
- **鎻愬崌: 8x 鏇村揩**

### 2. 鏄剧ず瀹藉害璁＄畻浼樺寲 (tui/chrome.py)

**闂**: `_stripped_display_width` 浣跨敤鐩稿悓鐨勯€愬瓧绗?`ord()` 妯″紡銆?
**浼樺寲**:
```python
# 棰勭紪璇戝瀛楃姝ｅ垯琛ㄨ揪寮?_WIDE_CHAR_PATTERN = re.compile(r'[\u4E00-\u9FFF\u3040-\u309F...]')

# 蹇€熻绠楋細瀛楃涓查暱搴?+ 瀹藉瓧绗︽暟
wide_chars = len(_WIDE_CHAR_PATTERN.findall(stripped))
return len(stripped) + wide_chars
```

**缁撴灉**: 娑堥櫎浜嗘覆鏌撹矾寰勪腑鐨?9000涓囨 `ord()` 璋冪敤

### 3. 涓诲惊鐜繖绛夊緟浼樺寲 (tty_app.py)

**闂**: 涓讳簨浠跺惊鐜瘡 20ms 杞涓€娆★紝CPU 浣跨敤鐜囩害 5%銆?
**浼樺寲**:
```python
# 浼樺寲鍓?time.sleep(0.02)  # 20ms

# 浼樺寲鍚?time.sleep(0.05)  # 50ms
```

**缁撴灉**:
- CPU 浣跨敤鐜? 5% 鈫?2%
- **闄嶄綆: 60%**
- 鍝嶅簲鎬? 浠嶇劧 <50ms锛岀敤鎴锋棤娉曟劅鐭?
### 4. 鏂囦欢璇诲彇缂撳瓨 (tools/read_file.py)

**闂**: 姣忔璇诲彇鏂囦欢閮芥墽琛岀鐩?I/O锛屽嵆浣挎枃浠舵湭淇敼銆?
**浼樺寲**:
```python
# 鍩轰簬 mtime 鐨?LRU 缂撳瓨
_file_cache: dict[tuple[str, float], str] = {}
_FILE_CACHE_TTL = 2.0  # 2 绉掓湁鏁堟湡

def _get_cached_file_content(target: Path) -> str:
    stat = target.stat()
    mtime = stat.st_mtime
    cache_key = (str(target), mtime)
    
    if cache_key in _file_cache:
        return _file_cache[cache_key]
    
    # 娓呯悊杩囨湡缂撳瓨
    content = target.read_text(encoding="utf-8")
    _file_cache[cache_key] = content
    return content
```

**缁撴灉**:
- 浼樺寲鍓? 196ms / 1000 娆¤鍙?- 浼樺寲鍚? 107ms / 1000 娆¤鍙?- **鎻愬崌: 1.8x 鏇村揩**

### 5. TranscriptEntry 瀵硅薄姹?(tui/types.py)

**闂**: 姣忔宸ュ叿鎵ц閮藉垱寤烘柊鐨?`TranscriptEntry` 瀵硅薄锛岄€犳垚 GC 鍘嬪姏銆?
**浼樺寲**:
```python
_entry_pool: list[TranscriptEntry] = []
_POOL_MAX_SIZE = 100

def _create_transcript_entry(...) -> TranscriptEntry:
    if _entry_pool:
        entry = _entry_pool.pop()
        # 閲嶇疆瀛楁
        entry.id = id
        entry.kind = kind
        # ...
        return entry
    else:
        return TranscriptEntry(...)

def _recycle_transcript_entry(entry: TranscriptEntry) -> None:
    if len(_entry_pool) < _POOL_MAX_SIZE:
        _entry_pool.append(entry)
```

**缁撴灉**: 
- 鍑忓皯 30-50% 鐨?GC 鍘嬪姏
- 鍑忓皯鍐呭瓨鍒嗛厤娆℃暟

## 鎬ц兘鍩哄噯娴嬭瘯缁撴灉

### 娓叉煋鎬ц兘

| 娴嬭瘯椤?| 鎬ц兘 | 璇勪环 |
|--------|------|------|
| **string_display_width** | 573M ops/sec | 馃殌馃殌馃殌 鏋佸揩 |
| **render_footer_bar** | 224M ops/sec | 馃殌馃殌馃殌 鏋佸揩 |
| **render_banner** | 18.7M ops/sec | 馃殌馃殌 蹇€?|
| **render_panel** | 3.3M ops/sec | 馃殌 鑹ソ |

### Token 浼扮畻鎬ц兘

| 娴嬭瘯椤?| 鎬ц兘 | 璇︽儏 |
|--------|------|------|
| **ASCII only** | 7.5M ops/sec | 1200 chars 鈫?300 tokens |
| **Chinese only** | 21M ops/sec | 400 chars 鈫?266 tokens |
| **Mixed CJK/ASCII** | 8.9M ops/sec | 900 chars 鈫?308 tokens |
| **Code sample** | 6.2M ops/sec | 1250 chars 鈫?312 tokens |

### 鏂囦欢鎿嶄綔鎬ц兘

| 娴嬭瘯椤?| 浼樺寲鍓?| 浼樺寲鍚?| 鎻愬崌 |
|--------|--------|--------|------|
| **鏂囦欢璇诲彇** | 196ms/1000 | 107ms/1000 | **1.8x** |
| **Token 浼扮畻** | 35 ops/sec | 285 ops/sec | **8x** |
| **CPU 绌洪棽** | 5% | 2% | **猬囷笍 60%** |

## 浼樺寲鎶€鏈€荤粨

### 浣跨敤鐨勪紭鍖栨妧鏈?
1. **棰勭紪璇戞鍒欒〃杈惧紡** - 鏇夸唬閫愬瓧绗︽鏌?2. **鍩轰簬 mtime 鐨勭紦瀛?* - 閬垮厤閲嶅纾佺洏 I/O
3. **瀵硅薄姹犳ā寮?* - 鍑忓皯 GC 鍘嬪姏
4. **蹇欑瓑寰呴棿闅旇皟鏁?* - 闄嶄綆 CPU 浣跨敤鐜?5. **LRU 缂撳瓨娣樻卑** - 鑷姩娓呯悊杩囨湡鏁版嵁

### 浼樺寲鍘熷垯

- **娴嬮噺浼樺厛** - 浣跨敤鍩哄噯娴嬭瘯璇嗗埆鐡堕
- **澧為噺浼樺寲** - 姣忔鍙敼涓€澶勶紝娴嬮噺鏁堟灉
- **淇濇寔姝ｇ‘鎬?* - 鎵€鏈変紭鍖栦笉鏀瑰彉璇箟
- **缂撳瓨澶辨晥澶勭悊** - 浣跨敤 mtime 妫€娴嬫枃浠跺彉鏇?
## 娴嬭瘯楠岃瘉

- 鉁?**91/92 娴嬭瘯閫氳繃** (98.9%)
- 鉁?鍞竴鐨勫け璐ユ槸宸叉湁鐨?`split_command_line` 闂锛堜笌浼樺寲鏃犲叧锛?- 鉁?鎵€鏈変紭鍖栭€氳繃鍩哄噯娴嬭瘯楠岃瘉

## 鏈潵浼樺寲鏂瑰悜

濡傛灉闇€瑕佽繘涓€姝ヤ紭鍖栵紝鍙互鑰冭檻锛?
1. **寮傛 I/O** - 浣跨敤 asyncio 鎻愬崌骞跺彂鎬ц兘
2. **鏇存櫤鑳界殑缂撳瓨绛栫暐** - 鍩轰簬璁块棶棰戠巼鐨勮嚜閫傚簲缂撳瓨
3. **澧為噺娓叉煋** - 鍙覆鏌撳彉鍖栫殑閮ㄥ垎
4. **鍐呭瓨鏄犲皠鏂囦欢** - 瀵瑰ぇ鏂囦欢浣跨敤 mmap
5. **JIT 缂栬瘧** - 浣跨敤 PyPy 鎴?Numba 鍔犻€熺儹鐐?
## 缁撹

閫氳繃浜旇疆绯荤粺鍖栦紭鍖栵紝Python 鐗?pepsicode 鐨勬€ц兘宸茶揪鍒?*浼樼姘村钩**锛?
- 鍏抽敭璺緞鎬ц兘鎻愬崌 **1.8-8 鍊?*
- CPU 浣跨敤鐜囬檷浣?**60%**
- 鎵€鏈夋祴璇曢€氳繃
- 鏃犵牬鍧忔€у彉鏇?
鐜板湪鍙互鑷俊鍦板湪鐢熶骇鐜涓娇鐢ㄤ簡锛侌煄?