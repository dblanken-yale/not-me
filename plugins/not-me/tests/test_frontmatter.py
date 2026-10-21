from __future__ import annotations

import unittest

from notme import frontmatter as fm
from notme.frontmatter import FrontmatterError
from tests import PLUGIN_ROOT

DOCS = ("project", "standards", "verify", "review", "prove")


def doc(fm_text: str, body: str = "body\n") -> str:
    return "---\n" + fm_text + "---\n" + body


class SplitTests(unittest.TestCase):
    def test_split_with_frontmatter(self):
        front, body = fm.split("---\na: 1\n---\nhello\n")
        self.assertEqual(front, "a: 1\n")
        self.assertEqual(body, "hello\n")

    def test_split_without_frontmatter(self):
        self.assertEqual(fm.split("# Title\n"), ("", "# Title\n"))

    def test_unclosed_frontmatter_raises(self):
        with self.assertRaises(FrontmatterError) as ctx:
            fm.parse("---\na: 1\n", source="x.md")
        self.assertEqual(ctx.exception.line, 1)
        self.assertEqual(ctx.exception.source, "x.md")


class ScalarTests(unittest.TestCase):
    def test_scalars(self):
        meta, _ = fm.parse(doc(
            "s: hello world\nd: \"a # b\"\nq: 'it''s'\ni: 3\nn: -2\nf: 1.5\n"
            "t: true\nfa: false\nnu: null\nti: ~\non: on\n"
        ), source="t")
        self.assertEqual(meta, {
            "s": "hello world", "d": "a # b", "q": "it's", "i": 3, "n": -2, "f": 1.5,
            "t": True, "fa": False, "nu": None, "ti": None, "on": "on",
        })

    def test_comments_stripped_outside_quotes(self):
        meta, _ = fm.parse(doc("# leading comment\na: x  # trailing\nb: 'y # z'\nc: it's fine\n"), source="t")
        self.assertEqual(meta, {"a": "x", "b": "y # z", "c": "it's fine"})

    def test_double_quote_escapes(self):
        meta, _ = fm.parse(doc('a: "x\\"y\\\\z\\n"\n'), source="t")
        self.assertEqual(meta["a"], 'x"y\\z\n')

    def test_empty_frontmatter(self):
        meta, body = fm.parse("---\n---\nbody\n", source="t")
        self.assertEqual(meta, {})
        self.assertEqual(body, "body\n")


class CollectionTests(unittest.TestCase):
    def test_flow_list_and_map(self):
        meta, _ = fm.parse(doc('a: [x, "c d", 3, []]\nb: {k: 1, l: [y], m: {n: null}}\n'), source="t")
        self.assertEqual(meta["a"], ["x", "c d", 3, []])
        self.assertEqual(meta["b"], {"k": 1, "l": ["y"], "m": {"n": None}})

    def test_block_list_and_inline_map_items(self):
        meta, _ = fm.parse(doc("repos:\n  - {name: acme/app, role: primary}\n  - acme/app-ui\nother:\n- a\n- b\n"), source="t")
        self.assertEqual(meta["repos"], [{"name": "acme/app", "role": "primary"}, "acme/app-ui"])
        self.assertEqual(meta["other"], ["a", "b"])

    def test_nested_maps_three_levels(self):
        meta, _ = fm.parse(doc("a:\n  b:\n    c: 1\n    d: [x]\n  e: 2\nf:\n"), source="t")
        self.assertEqual(meta, {"a": {"b": {"c": 1, "d": ["x"]}, "e": 2}, "f": None})

    def test_lines_reported_for_nested_keys(self):
        _, _, lines = fm.parse_with_lines(doc("a:\n  b: 1\n  c: {d: 2}\n"), source="t")
        self.assertEqual(lines[("a",)], 2)
        self.assertEqual(lines[("a", "b")], 3)
        self.assertEqual(lines[("a", "c", "d")], 4)


class ErrorTests(unittest.TestCase):
    CASES = [
        ("odd indent", "a:\n   b: 1\n", 3),
        ("four level nesting", "a:\n  b:\n    c:\n      d: 1\n", 5),
        ("unclosed flow list", "a: 1\nb: [x, y\n", 3),
        ("unclosed flow map", "b: {x: 1\n", 2),
        ("anchor", "a: &x 1\n", 2),
        ("block scalar", "a: |\n", 2),
        ("bad key", "just text\n", 2),
        ("duplicate key", "a: 1\na: 2\n", 3),
        ("block map in list item", "a:\n  - name: x\n", 3),
        ("tab indent", "a:\n\tb: 1\n", 3),
        ("unclosed quote", "a: \"x\n", 2),
        ("junk after quote", "a: \"x\" y\n", 2),
        ("colon in plain value", "a: b: c\n", 2),
        ("top-level list", "- a\n", 2),
    ]

    def test_errors_carry_line_numbers(self):
        for label, text, line in self.CASES:
            with self.subTest(label):
                with self.assertRaises(FrontmatterError) as ctx:
                    fm.parse(doc(text), source="f.md")
                self.assertEqual(ctx.exception.line, line, str(ctx.exception))
                self.assertIn("f.md:%d" % line, str(ctx.exception))


class DumpTests(unittest.TestCase):
    def test_round_trip_defaults(self):
        for name in DOCS:
            with self.subTest(name):
                text = (PLUGIN_ROOT / "defaults" / f"{name}.md").read_text(encoding="utf-8")
                meta, body = fm.parse(text, source=name)
                self.assertIn("schema", meta)
                again, body2 = fm.parse(fm.render(meta, body), source=name)
                self.assertEqual(again, meta)
                self.assertEqual(body2, body)

    def test_round_trip_awkward_strings(self):
        meta = {"a": "x: y", "b": "#hash", "c": "", "d": "true", "e": "1", "f": ["!rel", "a,b", "q'\"s"],
                "g": {"h": {"i": {"j": 1}}}, "k": {}, "l": None, "m": 2.5, "n": " pad "}
        again, _ = fm.parse(fm.render(meta, ""), source="t")
        self.assertEqual(again, meta)


if __name__ == "__main__":
    unittest.main()
