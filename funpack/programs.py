"""A bounded interpreter for candidate heuristics, never Python exec/eval.

The accepted language is deliberately narrower than Python. Every executable
node is translated to one of the explicit operations below. This is a language
boundary, not an operating-system sandbox for arbitrary Python.
"""
from __future__ import annotations

import ast
import hashlib
import math
import operator
import re

CODE_LANGUAGE_VERSION = "funpack-dsl-1"
MAX_CODE_BYTES = 12 * 1024
MAX_AST_NODES = 1024
MAX_AST_DEPTH = 40
MAX_STEPS = 50_000
MAX_COLLECTION = 4096
MAX_ABS_NUMBER = 1.0e12
MAX_ABS_INPUT = 1_000_000_000
MAX_RESPONSE_CHARS = 65_536

FF_CODE = "def priority(item, bins):\n    return [0.0 for r in bins]\n"
BF_CODE = "def priority(item, bins):\n    return [-(r - item) for r in bins]\n"

# Warm-start seeds for the combination study (mechanism_v3c): the validation-
# selected winners of the sealed formal study (protocol 3eb1f24d...), read
# verbatim from candidates-sealed.json and deduplicated by code. B's winner
# is identical in all three seeds and C's in two, so 12 winners collapse to
# 9 distinct programs. Each entry is named after its first sealed run.
WARMSTART_SEEDS = (
    ("A_seed11", 'def priority(item, bins):\n    return [200 - 2 * (r - item) if r - item <= 10 else (r - item) for r in bins]\n'),
    ("B_seed11", 'def priority(item, bins):\n    return [-(r - item) + (15 if r - item >= 28 and r - item <= 48 else 0) - (15 if r - item >= 8 and r - item <= 22 else 0) for r in bins]\n'),
    ("C_seed11", 'def priority(item, bins):\n    return [-(r - item) + (20 if 30 <= r - item <= 45 and item % 3 == 1 else 0) - (20 if 8 <= r - item <= 22 else 0) for r in bins]\n'),
    ("D_seed11", 'def priority(item, bins):\n    return [-(r - item) + (item / 2 if r - item >= 28 and r - item <= 42 else 0) - (item / 2 if r - item >= 6 and r - item <= 20 else 0) for r in bins]\n'),
    ("A_seed22", 'def priority(item, bins):\n    return [100 - (r - item) if r - item <= 12 else (r - item) for r in bins]\n'),
    ("D_seed22", 'def priority(item, bins):\n    return [-(r - item) + (25 if 30 <= r - item <= 45 else 0) - (25 if 10 <= r - item <= 20 else 0) for r in bins]\n'),
    ("A_seed33", 'def priority(item, bins):\n    return [-(r - item) + (30 if ((r - item) // 10) % 2 == 0 else 0) for r in bins]\n'),
    ("C_seed33", 'def priority(item, bins):\n    return [-(r - item) + (20 if 30 <= r - item <= 48 and item % 3 == 1 else 0) - (20 if 8 <= r - item <= 22 else 0) for r in bins]\n'),
    ("D_seed33", 'def priority(item, bins):\n    return [-(r - item) + (30 - abs((r - item) - 35) if 25 <= r - item <= 45 else 0) for r in bins]\n'),
)


class ProgramError(ValueError):
    """Invalid source, invalid output, or a resource/numeric limit violation."""


class _Budget:
    __slots__ = ("remaining",)

    def __init__(self):
        self.remaining = MAX_STEPS

    def tick(self, amount=1):
        self.remaining -= amount
        if self.remaining < 0:
            raise ProgramError("evaluation step limit exceeded")


class _Returned:
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


def _number(value):
    # bool is intentionally accepted as 0/1 in score expressions.
    if type(value) not in (int, float, bool):
        raise ProgramError("expected a real number")
    try:
        value = float(value)
    except (OverflowError, ValueError):
        raise ProgramError("number exceeds finite numeric bounds") from None
    if not math.isfinite(value) or abs(value) > MAX_ABS_NUMBER:
        raise ProgramError("number exceeds finite numeric bounds")
    return value


def _collection(value):
    if type(value) is not list:
        raise ProgramError("expected a numeric list")
    if len(value) > MAX_COLLECTION:
        raise ProgramError("collection length limit exceeded")
    return value


def _truth(value):
    if type(value) not in (int, float, bool):
        raise ProgramError("condition must be a number or boolean")
    return bool(value)


def _name(name):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name):
        raise ProgramError("names must start with an ASCII letter and be at most 64 characters")
    return name


def _target(name):
    _name(name)
    if name in {"item", "bins", "priority"} or name in _SAFE_NAMES:
        raise ProgramError("cannot rebind inputs, function name, or numeric helpers")
    return name


_BINARY = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_COMPARE = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
    ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
}
_SAFE_NAMES = frozenset(("abs", "min", "max", "sum", "len", "round", "sqrt", "log", "exp"))


def _arithmetic(kind, left, right):
    left, right = _number(left), _number(right)
    try:
        if kind is ast.Pow:
            if abs(right) > 12 or abs(left) > 1.0e6:
                raise ProgramError("power requires abs(base) <= 1e6 and abs(exponent) <= 12")
            value = math.pow(left, right)
        else:
            value = _BINARY[kind](left, right)
    except (ZeroDivisionError, ValueError, OverflowError) as exc:
        if isinstance(exc, ProgramError):
            raise
        raise ProgramError("invalid arithmetic: " + type(exc).__name__) from None
    return _number(value)


def _helper(name, args, budget):
    try:
        if name in ("min", "max"):
            if not args:
                raise ProgramError(name + " needs at least one argument")
            values = _collection(args[0]) if len(args) == 1 else args
            if not values:
                raise ProgramError(name + " cannot consume an empty list")
            budget.tick(len(values))
            return _number((min if name == "min" else max)(_number(v) for v in values))
        if name == "sum":
            if len(args) not in (1, 2):
                raise ProgramError("sum expects a list and optional numeric start")
            values = _collection(args[0])
            budget.tick(len(values))
            result = _number(args[1]) if len(args) == 2 else 0.0
            # Bound every intermediate sum, not only the final result.
            for value in values:
                result = _number(result + _number(value))
            return result
        if name == "len":
            if len(args) != 1:
                raise ProgramError("len expects one list")
            return float(len(_collection(args[0])))
        if name == "round":
            if len(args) not in (1, 2):
                raise ProgramError("round expects one or two numeric arguments")
            value = _number(args[0])
            digits = _number(args[1]) if len(args) == 2 else 0.0
            if not digits.is_integer() or abs(digits) > 12:
                raise ProgramError("round digits must be an integer between -12 and 12")
            return _number(round(value, int(digits)))
        if name == "log":
            if len(args) not in (1, 2):
                raise ProgramError("log expects one or two numeric arguments")
            return _number(math.log(*(_number(v) for v in args)))
        if len(args) != 1:
            raise ProgramError(name + " expects one numeric argument")
        value = _number(args[0])
        if name == "abs":
            return abs(value)
        if name == "sqrt":
            return _number(math.sqrt(value))
        if name == "exp":
            if abs(value) > 100:
                raise ProgramError("exp requires abs(argument) <= 100")
            return _number(math.exp(value))
    except (ValueError, OverflowError, ZeroDivisionError) as exc:
        if isinstance(exc, ProgramError):
            raise
        raise ProgramError("invalid " + name + " call: " + type(exc).__name__) from None
    raise ProgramError("unknown numeric helper")


def _bad(node):
    raise ProgramError("unsupported syntax: " + type(node).__name__ + " (line " + str(getattr(node, "lineno", "?")) + ")")


def _expression(node):
    """Compile only whitelisted AST nodes into closures over controlled values."""
    if isinstance(node, ast.Constant):
        value = _number(node.value)
        def constant(env, budget):
            budget.tick()
            return value
        return constant
    if isinstance(node, ast.Name):
        name = _name(node.id)
        def variable(env, budget):
            budget.tick()
            if name not in env:
                raise ProgramError("undefined name: " + name)
            return env[name]
        return variable
    if isinstance(node, ast.List):
        parts = [_expression(part) for part in node.elts]
        if len(parts) > MAX_COLLECTION:
            raise ProgramError("collection length limit exceeded")
        def literal_list(env, budget):
            budget.tick()
            return [_number(part(env, budget)) for part in parts]
        return literal_list
    if isinstance(node, ast.BinOp):
        kind = type(node.op)
        if kind not in _BINARY and kind is not ast.Pow:
            _bad(node)
        left, right = _expression(node.left), _expression(node.right)
        def binary(env, budget):
            budget.tick()
            return _arithmetic(kind, left(env, budget), right(env, budget))
        return binary
    if isinstance(node, ast.UnaryOp):
        kind, operand = type(node.op), _expression(node.operand)
        if kind not in (ast.UAdd, ast.USub, ast.Not):
            _bad(node)
        def unary(env, budget):
            budget.tick()
            value = operand(env, budget)
            if kind is ast.Not:
                return not _truth(value)
            value = _number(value)
            return -value if kind is ast.USub else value
        return unary
    if isinstance(node, ast.Compare):
        if any(type(op) not in _COMPARE for op in node.ops):
            _bad(node)
        left = _expression(node.left)
        rest = [(_COMPARE[type(op)], _expression(part)) for op, part in zip(node.ops, node.comparators)]
        def compare(env, budget):
            budget.tick()
            previous = _number(left(env, budget))
            for operation, part in rest:
                current = _number(part(env, budget))
                if not operation(previous, current):
                    return False
                previous = current
            return True
        return compare
    if isinstance(node, ast.BoolOp):
        kind = type(node.op)
        if kind not in (ast.And, ast.Or):
            _bad(node)
        parts = [_expression(part) for part in node.values]
        def boolean(env, budget):
            budget.tick()
            value = parts[0](env, budget)
            for part in parts[1:]:
                truth = _truth(value)
                if (kind is ast.And and not truth) or (kind is ast.Or and truth):
                    return value
                value = part(env, budget)
            _truth(value)
            return value
        return boolean
    if isinstance(node, ast.IfExp):
        condition, yes, no = _expression(node.test), _expression(node.body), _expression(node.orelse)
        def conditional(env, budget):
            budget.tick()
            return (yes if _truth(condition(env, budget)) else no)(env, budget)
        return conditional
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _SAFE_NAMES or node.keywords:
            raise ProgramError("calls must use whitelisted numeric helpers without keyword arguments")
        name = node.func.id
        arguments = [_expression(arg) for arg in node.args]
        def call(env, budget):
            budget.tick()
            return _helper(name, [arg(env, budget) for arg in arguments], budget)
        return call
    if isinstance(node, ast.Subscript):
        values, index = _expression(node.value), _expression(node.slice)
        def subscript(env, budget):
            budget.tick()
            data, position = _collection(values(env, budget)), _number(index(env, budget))
            if not position.is_integer():
                raise ProgramError("list index must be an integer")
            position = int(position)
            if not -len(data) <= position < len(data):
                raise ProgramError("list index out of bounds")
            return data[position]
        return subscript
    if isinstance(node, ast.ListComp):
        if len(node.generators) != 1:
            raise ProgramError("a comprehension must have exactly one generator")
        generator = node.generators[0]
        if generator.is_async or not isinstance(generator.target, ast.Name):
            raise ProgramError("comprehension requires one simple local target")
        name = _target(generator.target.id)
        sequence, element = _expression(generator.iter), _expression(node.elt)
        filters = [_expression(part) for part in generator.ifs]
        # A nested comprehension is safe too: iteration and node work share one
        # per-call budget. Each generator binds a temporary, restored name.
        def comprehension(env, budget):
            budget.tick()
            data = _collection(sequence(env, budget))
            result = []
            had_previous, previous = name in env, env.get(name)
            try:
                for value in data:
                    budget.tick()
                    env[name] = value
                    if all(_truth(check(env, budget)) for check in filters):
                        result.append(_number(element(env, budget)))
                        if len(result) > MAX_COLLECTION:
                            raise ProgramError("collection length limit exceeded")
            finally:
                if had_previous:
                    env[name] = previous
                else:
                    env.pop(name, None)
            return result
        return comprehension
    _bad(node)


def _block(nodes):
    statements = [_statement(node) for node in nodes]
    def block(env, budget):
        for statement in statements:
            returned = statement(env, budget)
            if returned is not None:
                return returned
        return None
    return block


def _statement(node):
    if isinstance(node, ast.Assign):
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            raise ProgramError("assignment requires one simple local name")
        name, expression = _target(node.targets[0].id), _expression(node.value)
        def assign(env, budget):
            budget.tick()
            env[name] = expression(env, budget)
        return assign
    if isinstance(node, ast.Return):
        if node.value is None:
            raise ProgramError("return requires a score list")
        expression = _expression(node.value)
        def return_value(env, budget):
            budget.tick()
            return _Returned(expression(env, budget))
        return return_value
    if isinstance(node, ast.If):
        condition, yes, no = _expression(node.test), _block(node.body), _block(node.orelse)
        def conditional(env, budget):
            budget.tick()
            return (yes if _truth(condition(env, budget)) else no)(env, budget)
        return conditional
    _bad(node)


class Program:
    """Validated callable heuristic: Program(source)(item, feasible_residuals)."""
    __slots__ = ("code", "code_hash", "_body")

    def __init__(self, code: str):
        if not isinstance(code, str):
            raise ProgramError("source must be text")
        # Reject huge strings before allocating a second UTF-8 representation.
        if len(code) > MAX_CODE_BYTES:
            raise ProgramError("source byte limit exceeded")
        try:
            source_bytes = code.encode("utf-8")
        except UnicodeEncodeError:
            raise ProgramError("source must be valid UTF-8 text") from None
        if len(source_bytes) > MAX_CODE_BYTES:
            raise ProgramError("source byte limit exceeded")
        try:
            tree = ast.parse(code, mode="exec")
        except (SyntaxError, ValueError, RecursionError) as exc:
            raise ProgramError("cannot parse source: " + type(exc).__name__) from None
        count = 0
        pending = [(tree, 0)]
        while pending:
            node, depth = pending.pop()
            count += 1
            if count > MAX_AST_NODES:
                raise ProgramError("AST node limit exceeded")
            if depth > MAX_AST_DEPTH:
                raise ProgramError("AST depth limit exceeded")
            pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
        if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
            raise ProgramError("source must contain only def priority(item, bins)")
        function = tree.body[0]
        args = function.args
        if (function.name != "priority" or function.decorator_list or function.returns is not None
                or function.type_comment is not None or getattr(function, "type_params", [])
                or args.posonlyargs or args.kwonlyargs or args.vararg or args.kwarg
                or args.defaults or args.kw_defaults
                or [arg.arg for arg in args.args] != ["item", "bins"]
                or any(arg.annotation is not None or arg.type_comment is not None for arg in args.args)):
            raise ProgramError("required signature is exactly def priority(item, bins)")
        self._body = _block(function.body)
        self.code = code
        self.code_hash = hashlib.sha256(source_bytes).hexdigest()

    def __call__(self, item: int, bins: list[int]) -> list[float]:
        if type(item) is not int or not 0 < item <= MAX_ABS_INPUT:
            raise ProgramError("item must be a positive bounded integer")
        _collection(bins)
        if any(type(value) is not int or not 0 <= value <= MAX_ABS_INPUT for value in bins):
            raise ProgramError("bin residuals must be bounded nonnegative integers")
        env = {"item": float(item), "bins": [float(value) for value in bins]}
        returned = self._body(env, _Budget())
        if returned is None:
            raise ProgramError("function reached its end without returning scores")
        scores = _collection(returned.value)
        if len(scores) != len(bins):
            raise ProgramError("score count must equal the number of bins")
        return [_number(score) for score in scores]


def extract_response(text: str, prompt_mode: str) -> dict:
    """Preserve fenced source exactly; report formatting separately from validity.

    Formatting errors do not decide program validity. The caller may evaluate a
    recoverable candidate while recording these errors. With multiple blocks,
    the first Python-labelled block (otherwise the first block) is extracted.
    """
    if prompt_mode not in ("basic", "enhanced"):
        raise ValueError("prompt_mode must be basic or enhanced")
    errors = []
    if not isinstance(text, str) or len(text) > MAX_RESPONSE_CHARS:
        return {"code": "", "strategy": "", "format_ok": False,
                "format_errors": ["response must be text of at most 65536 characters"]}
    blocks = list(re.finditer(r"```([^`\r\n]*)\r?\n([\s\S]*?)```", text))
    if not blocks:
        errors.append("expected one fenced Python code block")
        # Raw source is recoverable and can be validated by Program.
        code = text if text.lstrip().startswith("def priority(") else ""
        return {"code": code, "strategy": "", "format_ok": False, "format_errors": errors}
    if len(blocks) != 1:
        errors.append("expected exactly one code block")
    python_blocks = [block for block in blocks if block.group(1).strip().lower() in ("python", "py")]
    selected = python_blocks[0] if python_blocks else blocks[0]
    if not python_blocks:
        errors.append("code fence must be labelled python")
    code = selected.group(2)
    before, after = text[:selected.start()].strip(), text[selected.end():].strip()
    strategy = ""
    if prompt_mode == "basic":
        if before or after:
            errors.append("basic response should contain only the code block")
    else:
        if before.startswith("Strategy:"):
            strategy = before[len("Strategy:"):].strip()
        else:
            strategy = before
            errors.append("enhanced response needs an explicit Strategy: prefix before code")
        if not strategy:
            errors.append("enhanced response needs a nonempty strategy")
        if len(strategy.split()) > 80:
            errors.append("strategy exceeds 80 whitespace-separated words")
        if after:
            errors.append("unexpected text after the code block")
    return {"code": code, "strategy": strategy, "format_ok": not errors, "format_errors": errors}


def language_prompt() -> str:
    return (
        "Return exactly one function: def priority(item, bins):. item is the current "
        "positive integer item size; bins is a list of feasible bin residual capacities. "
        "Return a numeric list with exactly len(bins) scores, in input order; larger "
        "scores are preferred and ties use the earliest bin. The evaluator handles "
        "feasibility and opening bins. Use only numeric local assignments, if/else, "
        "return, numeric list literals, single-generator list comprehensions over "
        "bins or numeric lists, optional comprehension filters, integer list indexing, "
        "+ - * / // % **, numerical comparisons, and/or/not, and conditional expressions. "
        "Permitted calls: abs, min, max, sum, len, round, sqrt, log, exp (no keywords). "
        "No imports, attributes, strings, general loops, recursion, external state, "
        "list multiplication, generators, or other calls. Do not rebind item, bins, "
        "priority, or helper names. Use ASCII-letter-starting names of <=64 characters. "
        "All numeric intermediates must be finite with absolute value <=1e12; power "
        "requires abs(base)<=1e6 and abs(exponent)<=12; round digits are integers "
        "between -12 and 12; exp requires abs(argument)<=100. Source limit 12 KiB, "
        "AST limit 1024 nodes/depth 40, list limit 4096, evaluation limit 50000 steps. "
        "Language version: " + CODE_LANGUAGE_VERSION + "."
    )
