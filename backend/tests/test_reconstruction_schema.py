"""Every structured-output field has an API-compatible explicit type."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reconstruction_plan import PLAN_SCHEMA, MODEL_REPORT_SCHEMA
from reconstruction_review import REVIEW_SCHEMA, PLANNED_REVIEW_SCHEMA, HISTORICAL_PLANNED_REVIEW_SCHEMA


class StructuredSchemaTests(unittest.TestCase):
    def test_all_output_fields_declare_their_types(self):
        def visit(schema, location):
            self.assertTrue("type" in schema or "anyOf" in schema, location)
            for key, child in schema.get("properties", {}).items():
                visit(child, location + "." + key)
            if "items" in schema:
                visit(schema["items"], location + "[]")
            for child in schema.get("anyOf", []):
                visit(child, location + "|union")
        for name, schema in {"plan": PLAN_SCHEMA, "model_report": MODEL_REPORT_SCHEMA,
                             "review": REVIEW_SCHEMA, "review_v1": HISTORICAL_PLANNED_REVIEW_SCHEMA,
                             "review_v2": PLANNED_REVIEW_SCHEMA}.items():
            with self.subTest(schema=name):
                visit(schema, name)


if __name__ == "__main__":
    unittest.main()
