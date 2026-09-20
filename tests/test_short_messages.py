import ast
import pathlib
import textwrap
import unittest

from moderation import ALL_STORED_REASONS, reason_message
from premium_handlers import premium_offer_text


ROOT = pathlib.Path(__file__).resolve().parents[1]
METHODS = {"answer", "send_message", "edit_text"}
FILES = (
    "main.py", "legal_privacy.py", "premium_handlers.py", "premium_service.py",
    "moderation.py", "admin_content.py", "admin_stats.py", "support_handlers.py",
)


def rendered_lines(text):
    count = 0
    for line in text.splitlines() or [""]:
        count += len(textwrap.wrap(line, width=28) or [""])
    return count


class StaticShortMessageTests(unittest.TestCase):
    def test_static_routine_messages_fit_three_mobile_lines(self):
        failures = []
        for filename in FILES:
            tree = ast.parse((ROOT / filename).read_text())
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in METHODS
                    and node.args
                ):
                    continue
                try:
                    text = ast.literal_eval(node.args[-1] if node.func.attr == "send_message" else node.args[0])
                except (ValueError, TypeError):
                    continue
                if isinstance(text, str) and rendered_lines(text) > 3:
                    failures.append(f"{filename}:{node.lineno}: {text!r}")
        self.assertEqual(failures, [])

    def test_generated_denials_and_max_price_fit(self):
        for reason in (*ALL_STORED_REASONS, None):
            self.assertLessEqual(rendered_lines(reason_message(reason)), 3)
        for status in ("לא פעיל כרגע.", "פעיל עד 31/12/2099."):
            self.assertLessEqual(
                rendered_lines(premium_offer_text(10000, status)), 3,
            )


if __name__ == "__main__":
    unittest.main()