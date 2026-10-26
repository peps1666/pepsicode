"""Small repository tasks with public checks and independent hidden grading."""

from __future__ import annotations

import hashlib
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path


def source(text: str) -> str:
    return textwrap.dedent(text).lstrip()


@dataclass(frozen=True)
class Case:
    id: str
    description: str
    prompt: str
    files: dict[str, str]
    grader: str
    scenario: str = "coding"
    task_kind: str = "change"
    acceptance: tuple[dict, ...] | None = None
    unresolved_calls: tuple[str, ...] = ()
    steering: str | None = None


PAGINATION = Case(
    "pagination",
    "Repair an off-by-one bug and validate pagination boundaries",
    "Fix paginate in pagination.py. Pages are 1-based. Return up to size items; return [] past the end. "
    "Raise ValueError for page <= 0 or size <= 0. Preserve the input sequence and the public function signature. "
    "Read the project and run its public tests. Do not edit test_public.py.",
    {
        "pagination.py": "def paginate(items, page, size):\n    start = (page - 1) * size\n    return items[start:start + size - 1]\n",
        "test_public.py": source("""
            import unittest
            from pagination import paginate
            class PaginationTests(unittest.TestCase):
                def test_page(self):
                    self.assertEqual(paginate([1, 2, 3, 4], 1, 2), [1, 2])
                def test_empty(self):
                    self.assertEqual(paginate([], 1, 2), [])
            if __name__ == '__main__':
                unittest.main()
        """),
    },
    source("""
        import inspect
        from pagination import paginate
        assert list(inspect.signature(paginate).parameters) == ['items', 'page', 'size']
        for n in range(12):
            for size in range(1, 6):
                items = list(range(n))
                for page in range(1, 7):
                    original = items[:]
                    assert paginate(items, page, size) == items[(page-1)*size:page*size]
                    assert items == original
        for page, size in [(0, 2), (-1, 2), (1, 0), (1, -2)]:
            try:
                paginate([1, 2], page, size)
            except ValueError:
                pass
            else:
                raise AssertionError('invalid pagination accepted')
    """),
)

CROSS_FILE = Case(
    "cross_file",
    "Add order cancellation and update reporting across modules",
    "Add cancel(order) to orders.py. It must return a new dict with status='cancelled', preserve other fields, "
    "and leave the original unchanged. Cancelling a paid order raises ValueError; repeat cancellation is valid. "
    "Update reporting.summary so cancelled orders are excluded from count and revenue, pending orders count "
    "but contribute zero revenue, and only paid orders contribute revenue. Keep existing APIs and do not edit test_public.py.",
    {
        "orders.py": "def create(identifier, amount):\n    return {'id': identifier, 'amount': amount, 'status': 'pending'}\n",
        "reporting.py": "def summary(orders):\n    return {'count': len(orders), 'revenue': sum(order['amount'] for order in orders)}\n",
        "test_public.py": source("""
            import unittest
            from orders import create
            from reporting import summary
            class OrderTests(unittest.TestCase):
                def test_create(self):
                    self.assertEqual(create('a', 10)['status'], 'pending')
                def test_empty(self):
                    self.assertEqual(summary([]), {'count': 0, 'revenue': 0})
            if __name__ == '__main__':
                unittest.main()
        """),
    },
    source("""
        from orders import create, cancel
        from reporting import summary
        pending = create('a', 10)
        cancelled = cancel(pending)
        assert pending == {'id': 'a', 'amount': 10, 'status': 'pending'}
        assert cancelled == {'id': 'a', 'amount': 10, 'status': 'cancelled'}
        assert cancel(cancelled) == cancelled
        paid = {'id': 'b', 'amount': 25, 'status': 'paid'}
        try:
            cancel(paid)
        except ValueError:
            pass
        else:
            raise AssertionError('paid cancellation accepted')
        assert summary([pending, cancelled, paid]) == {'count': 2, 'revenue': 25}
        assert summary([cancelled]) == {'count': 0, 'revenue': 0}
    """),
)

MONEY = {
    "pricing.py": "def total(prices, discount):\n    raise NotImplementedError\n",
    "currency.json": '{"currency": "CNY", "rounding": "ROUND_HALF_UP"}\n',
    "test_public.py": source("""
        import unittest
        from pricing import total
        class PriceTests(unittest.TestCase):
            def test_total(self):
                self.assertEqual(total(['10.00', '5.00'], '0.10'), '13.50')
        if __name__ == '__main__':
            unittest.main()
    """),
}
MONEY_REQUIREMENTS = (
    "Implement total(prices, discount) in pricing.py. Prices and discount are decimal strings. "
    "Return a two-decimal string of sum(prices)*(1-discount), rounding once at the end with ROUND_HALF_UP. "
    "Use Decimal, never binary floating point. Reject negative prices and discounts outside [0, 1] with ValueError. "
    "An empty list returns '0.00'. Keep currency.json byte-for-byte unchanged and do not edit test_public.py."
)
MONEY_GRADER = source("""
    from decimal import Decimal, ROUND_HALF_UP
    from pricing import total
    for prices, discount in [([], '0'), (['0.005'], '0'), (['1.005', '1.005'], '0'),
                             (['100.01', '0.02'], '0.125'), (['999999999999.99', '0.02'], '0.01'),
                             (['15.50'], '1')]:
        expected = (sum((Decimal(p) for p in prices), Decimal(0)) * (1 - Decimal(discount))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        original = prices[:]
        actual = total(prices, discount)
        assert isinstance(actual, str) and actual == format(expected, '.2f'), (actual, expected)
        assert prices == original
    for prices, discount in [(['-0.01'], '0'), (['1'], '-0.1'), (['1'], '1.1')]:
        try:
            total(prices, discount)
        except ValueError:
            pass
        else:
            raise AssertionError('invalid money input accepted')
    import ast
    from pathlib import Path
    tree = ast.parse(Path('pricing.py').read_text(encoding='utf-8'))
    assert not any(isinstance(n, ast.Constant) and isinstance(n.value, float) for n in ast.walk(tree))
    assert not any(isinstance(n, ast.Name) and n.id == 'float' for n in ast.walk(tree))
""")

COMPACTION = Case(
    "compaction",
    "Retain requirements after two forced semantic compactions",
    "Continue the pricing task from the earlier discussion. Implement it and run the public tests.",
    MONEY,
    MONEY_GRADER,
    "compaction",
)
RESUME = Case(
    "resume",
    "Reopen a persisted conversation in a fresh process and complete it",
    "Resume the unfinished pricing task using the saved requirements. Implement it and run the public tests.",
    MONEY,
    MONEY_GRADER,
    "resume",
)
SMOKE = Case(
    "smoke",
    "Offline scripted harness self-test (not a model evaluation)",
    "Implement add(a, b) and run the tests.",
    {
        "calculation.py": "def add(a, b):\n    return a - b\n",
        "test_public.py": "import unittest\nfrom calculation import add\nclass TestAdd(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\nif __name__ == '__main__':\n    unittest.main()\n",
    },
    "from calculation import add\nassert add(7, 9) == 16\nassert add(-3, 3) == 0\n",
)
PAGINATION_ZH = Case(
    "pagination_zh",
    "自由中文行动请求",
    "帮我支持正确的分页边界，修复 pagination.py；页码从 1 开始，页大小必须为正，超出范围返回空列表，不得修改 test_public.py。",
    PAGINATION.files,
    PAGINATION.grader,
)
MIXED = Case(
    "mixed",
    "先解释再修复的混合任务",
    "先解释分页的边界问题，再修改 pagination.py：页码从 1 开始，保留输入，越界返回 []，非正参数抛 ValueError。不要修改 test_public.py。",
    PAGINATION.files,
    PAGINATION.grader,
)
DOCUMENT = Case(
    "document",
    "文档产物，无代码测试",
    "创建 guide.md，用中文说明 CSV 导出应正确处理引号、逗号和换行。",
    {"facts.txt": "CSV export documentation task\n"},
    "from pathlib import Path\ntext = Path('guide.md').read_text(encoding='utf-8')\nassert all(word in text for word in ['CSV', '引号', '逗号', '换行'])\n",
    "document",
    acceptance=({"id": "guide", "kind": "file_contains", "path": "guide.md", "text": "CSV"},),
)
ANALYSIS = Case(
    "analysis",
    "纯分析不得被迫修改或运行项目",
    "只解释 facts.txt 中分页公式的含义，不要修改任何文件。",
    {"facts.txt": "Pages start at 1. Offset = (page - 1) * size.\n"},
    "import json\nfrom pathlib import Path\ndata = json.loads((Path.cwd().parent / 'worker.json').read_text(encoding='utf-8'))\ntext = '\\n'.join(m.get('content', '') for m in data['messages'] if m['role'] == 'assistant')\nassert 'page' in text and 'size' in text\nassert Path('facts.txt').read_text() == 'Pages start at 1. Offset = (page - 1) * size.\\n'\n",
    "analysis",
    task_kind="answer",
    acceptance=(),
)
NO_TESTS = Case(
    "no_tests",
    "无现有测试项目的针对性检查",
    "修复 slug.py，让 slug(' Hello World ') 返回 'hello-world'；保留函数接口。",
    {"slug.py": "def slug(value):\n    return value\n"},
    "from slug import slug\nassert slug(' Hello World ') == 'hello-world'\nassert slug('A B') == 'a-b'\n",
    "no_tests",
    acceptance=(
        {
            "id": "slug-behavior",
            "kind": "command",
            "argv": [sys.executable, "-c", "from slug import slug; assert slug(' Hello World ') == 'hello-world'"],
        },
    ),
)
CASES = {
    case.id: case
    for case in (PAGINATION, CROSS_FILE, COMPACTION, RESUME, PAGINATION_ZH, MIXED, DOCUMENT, ANALYSIS, NO_TESTS)
}

INTERRUPTED = Case(
    "interrupted_resume", "checkpoint 落盘后真实进程骤停并恢复", RESUME.prompt, MONEY, MONEY_GRADER, "interrupted"
)
UNKNOWN = Case(
    "multiple_unknown",
    "观察并逐项核对多个未知操作，不重放写入",
    "receipt.txt 已包含两个写入的实际结果。先观察它，分别核对未决调用 seeded-first 和 seeded-second，确认已发生；不要重放写入，也不要修改 receipt.txt。",
    {"receipt.txt": "first=committed\nsecond=committed\n"},
    "import json\nfrom pathlib import Path\nassert Path('receipt.txt').read_text() == 'first=committed\\nsecond=committed\\n'\nworker = json.loads((Path.cwd().parent / 'worker.json').read_text(encoding='utf-8'))\nrows = [json.loads(s) for s in Path(worker['execution_state']['journal']).read_text(encoding='utf-8').splitlines()]\nresolved = [r for r in rows if r['event'] == 'tool/reconciled']\nassert {r['call_id'] for r in resolved} == {'seeded-first', 'seeded-second'}\nassert all(r['evidence']['outcome'] == 'occurred' and r['evidence']['observations'] for r in resolved)\n",
    "unknown_recovery",
    task_kind="operation",
    acceptance=({"id": "receipt", "kind": "file_contains", "path": "receipt.txt", "text": "second=committed"},),
    unresolved_calls=("seeded-first", "seeded-second"),
)
STEERING = Case(
    "steering",
    "执行中收到新目标后废除旧验收",
    "把 goal.py 中的 value() 改成返回 42。",
    {"goal.py": "def value():\n    return 0\n"},
    "import json\nfrom pathlib import Path\nfrom goal import value\nassert value() == 43\ndata = json.loads((Path.cwd().parent / 'worker.json').read_text(encoding='utf-8'))\nassert any(m.get('source', {}).get('mode') == 'steer' and '43' in m.get('content', '') for m in data['messages'])\n",
    "steering",
    acceptance=({"id": "value", "kind": "file_contains", "path": "goal.py", "text": "return 42"},),
    steering="目标改为让 value() 返回 43，请按新要求修改并重新验收。",
)
CASES.update({case.id: case for case in (INTERRUPTED, UNKNOWN, STEERING)})


def materialize(case: Case, workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=False)
    for name, content in case.files.items():
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")


def protected_files_intact(case: Case, workspace: Path) -> bool:
    for name in ("test_public.py", "currency.json"):
        if name in case.files:
            target = workspace / name
            if not target.is_file() or target.read_bytes() != case.files[name].encode("utf-8"):
                return False
    return True


def case_digest(case: Case) -> str:
    import json

    descriptor = dict(case.__dict__)
    if descriptor.get("task_kind") == "change":
        descriptor.pop("task_kind", None)
    if descriptor.get("acceptance") is None:
        descriptor.pop("acceptance", None)
    if not descriptor.get("unresolved_calls"):
        descriptor.pop("unresolved_calls", None)
    if descriptor.get("steering") is None:
        descriptor.pop("steering", None)
    return hashlib.sha256(json.dumps(descriptor, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
