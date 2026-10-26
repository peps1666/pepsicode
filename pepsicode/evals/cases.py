"""Small repository tasks with public checks and independent hidden grading."""

from __future__ import annotations

import hashlib
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
CASES = {case.id: case for case in (PAGINATION, CROSS_FILE, COMPACTION, RESUME)}


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

    return hashlib.sha256(json.dumps(case.__dict__, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
