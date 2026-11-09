"""Test doubles: stub boto3/botocore modules and a tiny in-memory DynamoDB Table.

The FakeTable understands only the update and condition expressions the
signup API actually uses.
"""

import copy
import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAMBDA_DIR = os.path.join(ROOT, "lambda")
SITE_DIR = os.path.join(ROOT, "site")

TEST_PHONE = "+15005550006"


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def install_stubs():
    if "botocore.exceptions" not in sys.modules:
        botocore = types.ModuleType("botocore")
        exc = types.ModuleType("botocore.exceptions")
        exc.ClientError = ClientError
        botocore.exceptions = exc
        sys.modules["botocore"] = botocore
        sys.modules["botocore.exceptions"] = exc
    if "boto3" not in sys.modules:
        boto3 = types.ModuleType("boto3")

        def _unavailable(*a, **k):
            raise RuntimeError("boto3 is stubbed in tests")

        boto3.resource = _unavailable
        boto3.client = _unavailable
        sys.modules["boto3"] = boto3
    if LAMBDA_DIR not in sys.path:
        sys.path.insert(0, LAMBDA_DIR)


def _split_top(s, sep=","):
    parts, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


class FakeTable:
    def __init__(self, *key_names):
        self.key_names = key_names
        self.items = {}
        self.calls = []

    def _key(self, d):
        return tuple(d[k] for k in self.key_names)

    def get_item(self, Key, **kw):
        self.calls.append(("get_item", Key))
        item = self.items.get(self._key(Key))
        return {"Item": copy.deepcopy(item)} if item is not None else {}

    def query(self, KeyConditionExpression, ExpressionAttributeValues, **kw):
        self.calls.append(("query", KeyConditionExpression))
        attr, placeholder = [x.strip() for x in KeyConditionExpression.split("=")]
        value = ExpressionAttributeValues[placeholder]
        return {"Items": [copy.deepcopy(i) for i in self.items.values() if i.get(attr) == value]}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None,
                 ExpressionAttributeNames=None, **kw):
        self.calls.append(("put_item", Item))
        old = copy.deepcopy(self.items.get(self._key(Item)))
        if ConditionExpression:
            current = self.items.get(self._key(Item))
            names, values = ExpressionAttributeNames or {}, ExpressionAttributeValues or {}
            if not self._eval_cond(ConditionExpression, current or {}, lambda n: names.get(n, n), lambda v: values[v]):
                raise ClientError("ConditionalCheckFailedException")
        self.items[self._key(Item)] = copy.deepcopy(Item)
        if kw.get("ReturnValues") == "ALL_OLD" and old is not None:
            return {"Attributes": old}
        return {}

    def delete_item(self, Key, **kw):
        self.calls.append(("delete_item", Key))
        self.items.pop(self._key(Key), None)
        return {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                    ExpressionAttributeNames=None, ConditionExpression=None, **kw):
        self.calls.append(("update_item", Key, UpdateExpression))
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        k = self._key(Key)
        current = copy.deepcopy(self.items.get(k))
        exists = current is not None
        item = current if exists else dict(Key)

        name = lambda n: names.get(n, n)  # noqa: E731
        val = lambda v: values[v]  # noqa: E731

        if ConditionExpression and not self._eval_cond(ConditionExpression, item if exists else {}, name, val):
            raise ClientError("ConditionalCheckFailedException")

        touched = set()
        before = copy.deepcopy(current)
        clauses = re.split(r"\b(SET|ADD|REMOVE)\b", UpdateExpression)
        it = iter(c.strip() for c in clauses if c.strip())
        for kind in it:
            body = next(it)
            if kind == "SET":
                for assign in _split_top(body):
                    lhs, rhs = [x.strip() for x in assign.split("=", 1)]
                    touched.add(name(lhs))
                    item[name(lhs)] = self._eval_value(rhs, item, name, val)
            elif kind == "ADD":
                for part in _split_top(body):
                    attr, v = part.split()
                    touched.add(name(attr))
                    item[name(attr)] = item.get(name(attr), 0) + val(v)
            elif kind == "REMOVE":
                for attr in _split_top(body):
                    touched.add(name(attr))
                    item.pop(name(attr), None)
        self.items[k] = item
        if kw.get("ReturnValues") == "UPDATED_OLD" and exists:
            return {"Attributes": {a: before[a] for a in touched if a in before}}
        return {}

    def _eval_value(self, expr, item, name, val):
        expr = expr.strip()
        if "+" in expr and not expr.startswith(":"):
            left, right = expr.rsplit("+", 1)
            return self._eval_value(left, item, name, val) + self._eval_value(right, item, name, val)
        m = re.match(r"if_not_exists\(\s*([#\w]+)\s*,\s*(:\w+)\s*\)$", expr)
        if m:
            attr = name(m.group(1))
            return item[attr] if attr in item else val(m.group(2))
        if expr.startswith(":"):
            return val(expr)
        return item.get(name(expr))

    def _eval_cond(self, expr, item, name, val):
        for disj in re.split(r"\s+OR\s+", expr):
            if all(self._atom(a.strip(), item, name, val) for a in re.split(r"\s+AND\s+", disj)):
                return True
        return False

    def _atom(self, atom, item, name, val):
        m = re.match(r"attribute_not_exists\(\s*([#\w]+)\s*\)$", atom)
        if m:
            return name(m.group(1)) not in item
        m = re.match(r"attribute_exists\(\s*([#\w]+)\s*\)$", atom)
        if m:
            return name(m.group(1)) in item
        m = re.match(r"([#\w]+)\s*(<>|<|>|=)\s*(:\w+)$", atom)
        if m:
            left = item.get(name(m.group(1)))
            right = val(m.group(3))
            if m.group(2) == ">":
                return left is not None and left > right
            if m.group(2) == "<":
                return left is not None and left < right
            if m.group(2) == "<>":
                return left != right
            return left == right
        raise AssertionError(f"unsupported condition atom: {atom}")
