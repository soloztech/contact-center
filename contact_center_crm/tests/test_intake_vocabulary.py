"""Review actual adapter writers without importing optional integrations."""
import ast
from pathlib import Path

from odoo.tests.common import TransactionCase

from ..models.intake import EXCLUDED_CONTENT_TYPES, HUMAN_CONTENT_TYPES


def literal_choices(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return literal_choices(node.body) | literal_choices(node.orelse)
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Dict):
        return set().union(*(literal_choices(v) for v in node.value.values))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "get" and len(node.args) > 1:
            return literal_choices(node.args[1])
    return set()


def dictionary_choices(node, name):
    result = set()
    if isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == name:
                result.update(literal_choices(value))
    return result


def assignment_choices(node):
    names = {n.id for n in node.targets if isinstance(n, ast.Name)}
    result = set()
    if names & {"content_type", "WHATSAPP_SYSTEM_CONTENT_TYPE"}:
        result.update(literal_choices(node.value))
    if isinstance(node.value, ast.Dict):
        if "_MEDIA_FIELDS" in names:
            result.update(
                k.value for k in node.value.keys if isinstance(k, ast.Constant)
            )
        if "_MEDIA_KINDS" in names:
            result.update(
                v.value for v in node.value.values if isinstance(v, ast.Constant)
            )
    return result


def wuzapi_return_choices(function):
    result = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
            if len(node.value.elts) >= 4:
                result.update(literal_choices(node.value.elts[3]))
    return result


def content_vocabulary(tree, *, wuzapi=False, structured=False):
    result = set()
    for node in ast.walk(tree):
        result.update(dictionary_choices(node, "content_type"))
        if isinstance(node, ast.keyword) and node.arg == "content_type":
            result.update(literal_choices(node.value))
        if isinstance(node, ast.Assign):
            result.update(assignment_choices(node))
        if (
            wuzapi
            and isinstance(node, ast.FunctionDef)
            and node.name
            in {
                "_message_content",
                "_structured_message_content",
                "_unsupported_human_content",
            }
        ):
            result.update(wuzapi_return_choices(node))
        if (
            structured
            and isinstance(node, ast.Return)
            and isinstance(node.value, ast.Tuple)
        ):
            result.update(dictionary_choices(node.value.elts[0], "type"))
    return result


class TestIntakeAdapterVocabulary(TransactionCase):
    def test_current_stored_adapter_types_are_explicitly_classified(self):
        root = Path(__file__).resolve().parents[2]
        sources = {
            "contact_center_wuzapi/services/adapter.py": {"wuzapi": True},
            "contact_center_wuzapi/services/structured_content.py": {
                "structured": True
            },
            "contact_center_whatsapp_cloud/services/normalizer.py": {},
            "contact_center_whatsapp_cloud/services/contracts.py": {},
            "contact_center_meta/services/normalizer.py": {},
            "contact_center_base/models/control_events.py": {},
        }
        classified = HUMAN_CONTENT_TYPES | EXCLUDED_CONTENT_TYPES
        self.assertFalse(HUMAN_CONTENT_TYPES & EXCLUDED_CONTENT_TYPES)
        observed = set()
        for relative, options in sources.items():
            path = root / relative
            if not path.is_file():
                # Optional adapter packages may be absent in a partial install.
                # Repository CI supplies every source listed here.
                continue
            with self.subTest(source=relative):
                values = content_vocabulary(ast.parse(path.read_text()), **options)
                self.assertTrue(values, "No content writer enumerated: " + relative)
                self.assertFalse(
                    values - classified,
                    "Unclassified stored type: " + repr(values - classified),
                )
                observed.update(values)
        self.assertTrue(observed)
        # Base control events are always present even in a standalone CRM graph.
        self.assertIn("identity.security.changed", observed)
