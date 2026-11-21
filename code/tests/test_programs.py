"""Boundary and semantic tests for candidate execution, without paid API calls."""
import unittest

from funpack.programs import (
    BF_CODE, FF_CODE, MAX_COLLECTION, Program, ProgramError, extract_response,
)


class ProgramTests(unittest.TestCase):
    def test_baselines_and_determinism(self):
        bins = [10, 8, 6]
        first, best = Program(FF_CODE), Program(BF_CODE)
        self.assertEqual(first(4, bins), [0, 0, 0])
        self.assertEqual(best(4, bins), [-6, -4, -2])
        self.assertEqual(best(4, bins), best(4, bins))
        self.assertEqual(bins, [10, 8, 6])
        self.assertEqual(best(4, []), [])

    def test_assignments_conditionals_helpers(self):
        code = """def priority(item, bins):
    left = [r - item for r in bins]
    average = sum(left) / max(1, len(left))
    if item > 2 and len(left) > 0:
        return [-abs(x) + (1 if x == 0 else 0) for x in left]
    else:
        return [round(sqrt(x * x), 2) for x in left]
"""
        self.assertEqual(Program(code)(4, [4, 6, 10]), [1, -2, -6])
        self.assertEqual(Program(code)(1, [1, 6]), [0, 5])

    def test_comprehension_scope_and_short_circuit(self):
        code = """def priority(item, bins):
    r = 7
    other = [x for x in bins if x >= item]
    score = (0 and 1 / 0) or r
    return [score + other[0] for r in bins]
"""
        self.assertEqual(Program(code)(2, [4, 6]), [11, 11])

    def test_chained_compare_math_and_negative_index(self):
        code = """def priority(item, bins):
    return [log(exp(1)) + bins[-1] if 0 < r <= 10 else 0 for r in bins]
"""
        self.assertEqual(Program(code)(2, [4, 6]), [7, 7])

    def test_comprehension_binding_is_local_even_when_nested(self):
        code = """def priority(item, bins):
    r = 9
    values = [sum([r for r in bins]) for r in bins]
    return [r + sum(values) for x in bins]
"""
        self.assertEqual(Program(code)(2, [4, 6]), [29, 29])
        no_leak = """def priority(item, bins):
    values = [r for r in bins]
    return [r for s in bins]
"""
        with self.assertRaisesRegex(ProgramError, "undefined name: r"):
            Program(no_leak)(2, [4, 6])

    def test_cannot_shadow_inputs_or_helper_names(self):
        names = ("item", "bins", "priority", "abs", "min", "max", "sum", "len", "round", "sqrt", "log", "exp")
        for name in names:
            for code in (
                "def priority(item, bins):\n    " + name + " = 1\n    return bins",
                "def priority(item, bins):\n    return [0 for " + name + " in bins]",
            ):
                with self.subTest(name=name, code=code), self.assertRaisesRegex(ProgramError, "cannot rebind"):
                    Program(code)

    def test_rejects_mutation_even_through_list_aliases(self):
        operations = ("alias[0] = 1", "alias.append(1)", "alias += bins", "del alias[0]")
        for operation in operations:
            code = "def priority(item, bins):\n    alias = bins\n    " + operation + "\n    return bins"
            with self.subTest(operation=operation), self.assertRaises(ProgramError):
                Program(code)
        bins = [4, 6]
        alias = Program("def priority(item, bins):\n    alias = bins\n    return alias")
        output = alias(2, bins)
        output[0] = 100
        self.assertEqual(bins, [4, 6])
        self.assertEqual(alias(2, bins), [4, 6])

    def test_text_identity_does_not_normalize_equivalent_programs(self):
        first = Program(BF_CODE)
        second = Program(BF_CODE + "# A comment changes text identity.\n")
        self.assertNotEqual(first.code_hash, second.code_hash)
        self.assertEqual(first(2, [4, 6]), second(2, [4, 6]))
        self.assertEqual(first.code, BF_CODE)

    def test_parse_and_signature_errors(self):
        bad_sources = [
            "def priority(:", "x = 3", "\ud800", "def bad(item, bins):\n    return bins",
            "def priority(item, bins, extra=0):\n    return bins",
            "@something\ndef priority(item, bins):\n    return bins",
            "def priority(item: int, bins):\n    return bins",
            "def priority(item, bins):\n    item = 1\n    return bins",
        ]
        for code in bad_sources:
            with self.subTest(code=code), self.assertRaises(ProgramError):
                Program(code)

    def test_rejects_ambient_authority_and_arbitrary_syntax(self):
        expressions = [
            "open('secret')", "__import__('os')", "bins.__class__", "priority(item, bins)",
            "(lambda: 1)()", "[x for x in range(3)]", "{1: 2}", "[x for x in bins for y in bins]",
            "sum(x for x in bins)", "min(bins, default=0)", "bins[0:1]",
        ]
        for expression in expressions:
            with self.subTest(expression=expression), self.assertRaises(ProgramError):
                Program("def priority(item, bins):\n    return " + expression)
        for statement in ("import os", "while True:\n        return bins", "for x in bins:\n        return bins"):
            with self.subTest(statement=statement), self.assertRaises(ProgramError):
                Program("def priority(item, bins):\n    " + statement)

    def test_numeric_errors_are_clear_and_finite(self):
        expressions = ["1 / 0", "1e309", "1e12 * 2", "2 ** 1000000000", "1e7 ** 2",
                       "(-1) ** 0.5", "log(0)", "sqrt(-1)", "exp(100)", "round(1, 999)"]
        for expression in expressions:
            with self.subTest(expression=expression), self.assertRaises(ProgramError):
                Program("def priority(item, bins):\n    return [" + expression + " for r in bins]")(2, [4])

    def test_sum_intermediates_and_tiny_divisor_are_guarded(self):
        for expression in ("sum([1e12, 1e12, -1e12])", "1 / 1e-300", "round(1, 2.5)"):
            with self.subTest(expression=expression), self.assertRaises(ProgramError):
                Program("def priority(item, bins):\n    return [" + expression + " for r in bins]")(2, [4])

    def test_list_multiplication_and_shape_errors(self):
        for expression in ("bins * 1000000000", "bins + bins", "[1]", "2", "[bins for r in bins]"):
            with self.subTest(expression=expression), self.assertRaises(ProgramError):
                Program("def priority(item, bins):\n    return " + expression)(2, [4, 6])

    def test_undefined_name_and_no_return(self):
        with self.assertRaisesRegex(ProgramError, "undefined name"):
            Program("def priority(item, bins):\n    return [missing for r in bins]")(2, [4])
        with self.assertRaisesRegex(ProgramError, "without returning"):
            Program("def priority(item, bins):\n    x = 1")(2, [4])

    def test_resource_limits(self):
        with self.assertRaisesRegex(ProgramError, "source byte"):
            Program(FF_CODE + "#" + "a" * 13_000)
        with self.assertRaisesRegex(ProgramError, "AST node"):
            Program("def priority(item, bins):\n    return [" + ",".join(["0"] * 1100) + "]")
        with self.assertRaisesRegex(ProgramError, "AST depth"):
            Program("def priority(item, bins):\n    return [" + "+".join(["r"] * 60) + " for r in bins]")
        with self.assertRaisesRegex(ProgramError, "collection length"):
            Program(BF_CODE)(2, [4] * (MAX_COLLECTION + 1))
        with self.assertRaisesRegex(ProgramError, "step limit"):
            Program("def priority(item, bins):\n    return [sum([s for s in bins]) for r in bins]")(2, [4] * 300)
        with self.assertRaisesRegex(ProgramError, "step limit"):
            Program("def priority(item, bins):\n    return [sum(bins) for r in bins]")(2, [4] * 300)

    def test_local_state_and_budget_are_reset_after_error(self):
        code = """def priority(item, bins):
    if item == 1:
        return [sum(bins) for r in bins]
    return [-(r - item) for r in bins]
"""
        program = Program(code)
        with self.assertRaisesRegex(ProgramError, "step limit"):
            program(1, [4] * 300)
        self.assertEqual(program(2, [4, 6]), [-2, -4])

    def test_input_validation(self):
        for item, bins in ((0, [4]), (2, [4.5]), (2, [-1]), (True, [4]), (2, (4,)), (10**15, [4])):
            with self.subTest(item=item, bins=bins), self.assertRaises(ProgramError):
                Program(BF_CODE)(item, bins)


class ResponseTests(unittest.TestCase):
    def test_fenced_source_preserved_exactly(self):
        code = "def priority(item, bins):\r\n    return [0.0 for r in bins]  \r\n"
        result = extract_response("```python\n" + code + "```", "basic")
        self.assertEqual(result["code"], code)
        self.assertTrue(result["format_ok"])

    def test_enhanced_strategy(self):
        result = extract_response("Strategy: Prefer tighter feasible bins.\n```python\n" + BF_CODE + "```", "enhanced")
        self.assertTrue(result["format_ok"])
        self.assertEqual(result["strategy"], "Prefer tighter feasible bins.")
        self.assertEqual(Program(result["code"])(2, [4]), [-2])

    def test_format_violations_do_not_discard_recoverable_code(self):
        examples = [
            ("Explanation\n```python\n" + BF_CODE + "```", "basic"),
            ("Explanation\n```python\n" + BF_CODE + "```", "enhanced"),
            ("Strategy: " + "word " * 81 + "\n```python\n" + BF_CODE + "```", "enhanced"),
            ("```python\n" + BF_CODE + "```\nextra", "basic"),
            (BF_CODE, "basic"),
            ("```\n" + BF_CODE + "```", "basic"),
            ("```python\n" + BF_CODE + "```\n```python\n" + FF_CODE + "```", "basic"),
        ]
        for text, mode in examples:
            with self.subTest(mode=mode, text=text):
                result = extract_response(text, mode)
                self.assertFalse(result["format_ok"])
                self.assertTrue(result["format_errors"])
                self.assertEqual(Program(result["code"])(2, [4]), [-2])

    def test_no_code(self):
        result = extract_response("Sorry, no solution.", "basic")
        self.assertEqual(result["code"], "")
        self.assertFalse(result["format_ok"])


if __name__ == "__main__":
    unittest.main()
