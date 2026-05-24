from __future__ import annotations

import ast
import operator as op
from typing import Any

ALLOWED_BIN = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
    ast.Mod: op.mod,
    ast.Pow: op.pow,
    ast.FloorDiv: op.floordiv,
}
ALLOWED_UNARY = {ast.UAdd: op.pos, ast.USub: op.neg}


class ExpressionEvaluator:
    def __init__(self, max_length: int = 128, max_depth: int = 10):
        self.max_length = max_length
        self.max_depth = max_depth

    def evaluate(self, text: str) -> float:
        expr = text.strip()
        if not expr or len(expr) > self.max_length:
            raise ValueError("Пустое или слишком длинное выражение")
        tree = ast.parse(expr, mode="eval")
        return self._eval(tree.body, depth=0)

    def _eval(self, node: ast.AST, depth: int) -> Any:
        if depth > self.max_depth:
            raise ValueError("Слишком глубокое выражение")
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in ALLOWED_BIN:
            left = self._eval(node.left, depth + 1)
            right = self._eval(node.right, depth + 1)
            return ALLOWED_BIN[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in ALLOWED_UNARY:
            return ALLOWED_UNARY[type(node.op)](self._eval(node.operand, depth + 1))
        raise ValueError("Недопустимое выражение")
