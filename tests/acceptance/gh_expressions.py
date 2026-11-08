"""A small evaluator for GitHub Actions expressions, for reading generated workflows offline.

Supports literals, context paths (with ``.name`` and ``['name']``), ``!``, ``==``, ``!=``,
``<``, ``<=``, ``>``, ``>=``, ``&&``, ``||``, parentheses and the functions ``startsWith``,
``endsWith``, ``contains``, ``format``, ``join``, ``toJSON`` and the status functions.
Semantics follow GitHub: ``&&``/``||`` return an operand, string comparison ignores case,
missing properties are null, and ``${{ }}`` inside a string interpolates.
"""

import json
import re

TOKEN = re.compile(r"""\s*(?:
    (?P<string>'(?:[^']|'')*')
  | (?P<number>-?\d+(?:\.\d+)?)
  | (?P<op>\|\||&&|==|!=|<=|>=|<|>|!|\(|\)|\[|\]|,|\.|\*)
  | (?P<name>[A-Za-z_][A-Za-z0-9_-]*)
)""", re.VERBOSE)


class ExpressionError(AssertionError):
    pass


def _tokens(text):
    position, out = 0, []
    while position < len(text):
        if text[position:].strip() == "":
            break
        match = TOKEN.match(text, position)
        if not match:
            raise ExpressionError(f"cannot read expression at {text[position:]!r}")
        position = match.end()
        kind = match.lastgroup
        out.append((kind, match.group(kind)))
    return out


def truthy(value):
    return not (value is None or value is False or value == "" or value == 0)


def _text(value):
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def _number(value):
    if value is None or value is False:
        return 0
    if value is True:
        return 1
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value) if str(value).strip() else 0
    except ValueError:
        return float("nan")


def _equal(left, right):
    if isinstance(left, str) and isinstance(right, str):
        return left.lower() == right.lower()
    if type(left) is type(right) or left is None or right is None:
        return left == right
    return _number(left) == _number(right)


class _Parser:
    def __init__(self, text, context):
        self.items = _tokens(text)
        self.index = 0
        self.context = context

    def peek(self):
        return self.items[self.index] if self.index < len(self.items) else (None, None)

    def take(self, value=None):
        kind, text = self.peek()
        if value is not None and text != value:
            raise ExpressionError(f"expected {value!r}, found {text!r}")
        self.index += 1
        return kind, text

    def parse(self):
        value = self.or_()
        if self.index != len(self.items):
            raise ExpressionError(f"unexpected {self.peek()[1]!r}")
        return value

    def or_(self):
        value = self.and_()
        while self.peek()[1] == "||":
            self.take()
            right = self.and_()
            value = value if truthy(value) else right
        return value

    def and_(self):
        value = self.compare()
        while self.peek()[1] == "&&":
            self.take()
            right = self.compare()
            value = right if truthy(value) else value
        return value

    def compare(self):
        value = self.unary()
        while self.peek()[1] in {"==", "!=", "<", "<=", ">", ">="}:
            _, op = self.take()
            right = self.unary()
            if op == "==":
                value = _equal(value, right)
            elif op == "!=":
                value = not _equal(value, right)
            else:
                a, b = _number(value), _number(right)
                value = {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]
        return value

    def unary(self):
        if self.peek()[1] == "!":
            self.take()
            return not truthy(self.unary())
        return self.postfix(self.primary())

    def primary(self):
        kind, text = self.take()
        if kind == "string":
            return text[1:-1].replace("''", "'")
        if kind == "number":
            return float(text) if "." in text else int(text)
        if text == "(":
            value = self.or_()
            self.take(")")
            return value
        if kind == "name":
            if text in {"true", "false"}:
                return text == "true"
            if text == "null":
                return None
            if self.peek()[1] == "(":
                return self.call(text)
            return self.context.get(text)
        raise ExpressionError(f"unexpected {text!r}")

    def postfix(self, value):
        while self.peek()[1] in {".", "["}:
            _, op = self.take()
            if op == ".":
                _, key = self.take()
            else:
                key = self.or_()
                self.take("]")
            if isinstance(value, dict):
                match = next((k for k in value if isinstance(k, str) and isinstance(key, str)
                              and k.lower() == key.lower()), key)
                value = value.get(match)
            elif isinstance(value, list) and isinstance(key, int):
                value = value[key] if 0 <= key < len(value) else None
            else:
                value = None
        return value

    def call(self, name):
        self.take("(")
        args = []
        while self.peek()[1] != ")":
            args.append(self.or_())
            if self.peek()[1] == ",":
                self.take()
        self.take(")")
        lowered = name.lower()
        if lowered == "startswith":
            return _text(args[0]).lower().startswith(_text(args[1]).lower())
        if lowered == "endswith":
            return _text(args[0]).lower().endswith(_text(args[1]).lower())
        if lowered == "contains":
            if isinstance(args[0], list):
                return any(_equal(item, args[1]) for item in args[0])
            return _text(args[1]).lower() in _text(args[0]).lower()
        if lowered == "format":
            template = _text(args[0])
            return re.sub(r"\{(\d+)\}", lambda m: _text(args[1 + int(m.group(1))]),
                          template.replace("{{", "\0").replace("}}", "\1")).replace("\0", "{").replace("\1", "}")
        if lowered == "join":
            separator = _text(args[1]) if len(args) > 1 else ","
            return separator.join(_text(item) for item in args[0]) if isinstance(args[0], list) else _text(args[0])
        if lowered == "tojson":
            return json.dumps(args[0])
        if lowered in {"success", "always"}:
            return True
        if lowered in {"failure", "cancelled"}:
            return False
        raise ExpressionError(f"unsupported function {name}()")


def evaluate(expression, context):
    """Evaluate one expression (without ``${{ }}``)."""
    return _Parser(expression, context).parse()


def condition(value, context):
    """A job or step ``if``: a bare expression or one wrapped in ``${{ }}``; absent means success()."""
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    text = str(value).strip()
    whole = re.fullmatch(r"\$\{\{(.*)\}\}", text, re.S)
    return truthy(evaluate(whole.group(1) if whole else text, context))


def interpolate(value, context):
    """A string field: every ``${{ expr }}`` replaced by its text value."""
    if value is None:
        return None
    if not isinstance(value, str):
        return _text(value)
    return re.sub(r"\$\{\{(.*?)\}\}", lambda m: _text(evaluate(m.group(1), context)), value, flags=re.S)
