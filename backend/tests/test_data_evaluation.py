import _env  # noqa: F401  (must be first)

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from app.agents.data_analyst import data_analyst_agent
from app.agents.evaluation import evaluate_report
from app.models import AgentResult, ChatRequest, SourceReference, UploadedFile
from app.routes.chat import _validate_upload
from app.services.runtime_store import RuntimeStore


class DataAnalystTests(unittest.TestCase):
    def test_csv_analysis_profiles_quality_statistics_and_correlations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "metrics.csv"
            pd.DataFrame({"users": [10, 20, 30, 30], "revenue": [100, 200, 300, 300], "region": ["north", "south", None, None]}).to_csv(source, index=False)
            request = ChatRequest(message="Analyze quality and correlations", files=[UploadedFile(name=source.name, storage_path=str(source))])
            with patch("app.agents.data_analyst.OUTPUT_DIR", root):
                result = data_analyst_agent(request)
            self.assertEqual(result.metadata["tables_analyzed"], 1)
            self.assertLess(result.metadata["average_quality_score"], 100)
            self.assertTrue(any("correlation" in finding for finding in result.findings))
            self.assertTrue(any("duplicate" in item for item in result.recommendations))
            self.assertFalse(Path(result.artifacts[0]["path"]).is_absolute(), "server paths must not leak")
            profile = json.loads((root / result.artifacts[0]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(profile["profiles"][0]["rows"], 4)

    def test_data_agent_uses_stored_project_dataset_when_no_file_is_attached(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sales.csv"
            pd.DataFrame({"month": ["Jan", "Feb", "Mar"], "revenue": [1000, 1250, 1500], "orders": [10, 12, 15]}).to_csv(source, index=False)
            store = RuntimeStore(root / "runtime.db")
            try:
                store.create_document(user_id="user-a", project_id="project-a", name=source.name, content_type="text/csv", storage_path=str(source), size=source.stat().st_size)
                request = ChatRequest(message="Analyze the uploaded sales dataset", user_id="user-a", project_id="project-a")
                with patch("app.agents.data_analyst.OUTPUT_DIR", root), patch("app.agents.data_analyst.runtime_store", store):
                    result = data_analyst_agent(request)
            finally:
                store.close()
            self.assertEqual(result.metadata["rows_analyzed"], 3)
            self.assertIn("Using 1 stored dataset file", result.findings[0])

    def test_analysis_builds_charts_rendered_as_markdown_blocks(self):
        from app.agents.visualization import render_chart_blocks, strip_chart_blocks
        from app.orchestrator.executor import _render_direct_result

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sales.csv"
            days = pd.date_range("2024-01-01", periods=90, freq="D")
            pd.DataFrame(
                {
                    "Invoice ID": [f"INV-{index}" for index in range(90)],
                    "Date": days.strftime("%m/%d/%Y"),
                    "Product line": ["food", "toys", "tools"] * 30,
                    "Total": [float(index % 17) * 12.5 + 3 for index in range(90)],
                    "Units": [index % 23 for index in range(90)],
                }
            ).to_csv(source, index=False)
            request = ChatRequest(message="Visualize Total", files=[UploadedFile(name=source.name, storage_path=str(source))])
            with patch("app.agents.data_analyst.OUTPUT_DIR", root):
                first = data_analyst_agent(request)
                second = data_analyst_agent(request)

        charts = first.metadata["charts"]
        self.assertEqual(charts, second.metadata["charts"], "charts must be deterministic")
        by_title = {chart["title"]: chart for chart in charts}
        self.assertEqual(by_title["Total per week"]["type"], "line")
        self.assertEqual(sorted(by_title["Total by Product line"]["categories"]), ["food", "tools", "toys"])
        self.assertEqual(sum(item["count"] for item in by_title["Distribution of Total"]["bins"]), 90)
        self.assertFalse(any("Invoice ID" in chart["title"] for chart in charts), "identifier columns are not charted")

        text = _render_direct_result(first)
        self.assertIn("## Visualizations", text)
        self.assertEqual(text.count("```chart"), len(charts))
        block = text.split("```chart\n", 1)[1].split("\n```", 1)[0]
        self.assertEqual(json.loads(block), charts[0])
        self.assertNotIn("```chart", strip_chart_blocks(text))
        self.assertEqual(render_chart_blocks([]), "")

    def test_requested_transformations_are_applied_and_returned_as_a_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "people.csv"
            pd.DataFrame(
                {"Name": ["a", "b", "b", "c"], "Age": [25, 40, 40, None], "City": ["x", "y", "y", "z"], "Salary": [100, 300, 300, 200]}
            ).to_csv(source, index=False)
            request = ChatRequest(
                message="remove duplicates, fill missing Age with median, drop the City column and sort by Salary descending",
                files=[UploadedFile(name=source.name, storage_path=str(source))],
            )
            with patch("app.agents.data_analyst.OUTPUT_DIR", root):
                result = data_analyst_agent(request)
            dataset = result.artifacts[0]
            self.assertEqual(dataset["type"], "dataset")
            self.assertFalse(Path(dataset["path"]).is_absolute(), "server paths must not leak")
            output = pd.read_csv(root / dataset["path"])
            self.assertEqual(list(output.columns), ["Name", "Age", "Salary"])
            self.assertEqual(output["Salary"].tolist(), [300, 200, 100])
            self.assertEqual(output["Age"].tolist(), [40, 32.5, 25])
            self.assertEqual(result.metadata["rows_analyzed"], 3, "the analysis describes the transformed data")
            self.assertEqual(len(result.metadata["transformations"]), 4)

    def test_model_plan_is_validated_and_format_can_change(self):
        from app.agents.data_transform import parse_plan

        plan = parse_plan('{"operations":[{"op":"filter_rows","column":"age","operator":">=","value":30},{"op":"exec","code":"x"}],"output_format":"xlsx"}')
        self.assertIsNotNone(plan)
        operations, output_format = plan
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "people.csv"
            pd.DataFrame({"Age": [25, 40, 31], "Name": ["a", "b", "c"]}).to_csv(source, index=False)
            request = ChatRequest(message="sirf 30+ age wale rakho", files=[UploadedFile(name=source.name, storage_path=str(source))])
            with patch("app.agents.data_analyst.OUTPUT_DIR", root):
                result = data_analyst_agent(request, plan={"operations": operations, "output_format": output_format})
            dataset = result.artifacts[0]
            self.assertEqual(dataset["format"], "xlsx")
            self.assertEqual(pd.read_excel(root / dataset["path"])["Name"].tolist(), ["b", "c"])
            self.assertTrue(any("unsupported operation: exec" in warning for warning in result.warnings))

    def test_plain_analysis_does_not_write_a_dataset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "metrics.csv"
            pd.DataFrame({"users": [1, 2, 3]}).to_csv(source, index=False)
            request = ChatRequest(message="Analyze quality and correlations", files=[UploadedFile(name=source.name, storage_path=str(source))])
            with patch("app.agents.data_analyst.OUTPUT_DIR", root):
                result = data_analyst_agent(request)
            self.assertEqual([artifact["type"] for artifact in result.artifacts], ["data_profile"])

    def test_rule_parser_handles_common_phrasings(self):
        from app.agents.data_transform import rule_based_plan

        columns = ["Name", "Age", "City", "Status"]
        self.assertEqual(
            rule_based_plan("keep only columns Name, Age and City", columns),
            [{"op": "keep_columns", "columns": ["Name", "Age", "City"]}],
        )
        self.assertEqual(
            rule_based_plan("remove rows where Status is cancelled", columns),
            [{"op": "filter_rows", "column": "Status", "operator": "!=", "value": "cancelled"}],
        )
        self.assertEqual(
            rule_based_plan("duplicates hata do aur missing values bhar do", columns),
            [{"op": "drop_duplicates"}, {"op": "fill_missing", "strategy": "auto"}],
        )
        self.assertEqual(rule_based_plan("analyze correlations", columns), [])

    def test_csv_download_requests_are_recognised_without_other_tasks(self):
        from app.agents.data_transform import requested_format

        for message in ("convert to csv", "export csv", "give me the csv", "remove duplicates and give csv", "csv me do", "csv file chahiye", "convert the excel to csv"):
            self.assertEqual(requested_format(message), "csv", message)
        self.assertEqual(requested_format("excel file me convert karo"), "xlsx")
        for message in ("analyze this csv", "give me insights from this csv", "csv file mein kitne rows hain"):
            self.assertIsNone(requested_format(message), message)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sales.xlsx"
            pd.DataFrame({"region": ["north", "south"], "revenue": [10, 20]}).to_excel(source, index=False)
            request = ChatRequest(message="csv me do", files=[UploadedFile(name=source.name, storage_path=str(source))])
            with patch("app.agents.data_analyst.OUTPUT_DIR", root):
                result = data_analyst_agent(request)
            dataset = result.artifacts[0]
            self.assertEqual((dataset["type"], dataset["format"]), ("dataset", "csv"))
            self.assertEqual(pd.read_csv(root / dataset["path"])["revenue"].tolist(), [10, 20])

    def test_missing_dataset_returns_structured_warning(self):
        result = data_analyst_agent(ChatRequest(message="analyze", user_id=_env.unique("nodata")))
        self.assertIn("No supported CSV or Excel dataset", result.summary)
        self.assertTrue(result.warnings)


class EvaluationTests(unittest.TestCase):
    def test_invalid_empty_report_recommends_retry(self):
        evaluation = evaluate_report("", {"agent": AgentResult(summary="")})
        self.assertTrue(evaluation.retry_recommended)
        self.assertFalse(evaluation.format_valid)

    def test_grounded_cited_report_scores_well(self):
        report = "## Executive Summary\n\nFastAPI serves the backend API. [Source 1]\n\n## Sources\n\n1. architecture.txt"
        outputs = {
            "rag": AgentResult(
                summary="FastAPI serves the backend API.",
                sources=[SourceReference(title="architecture.txt", source_type="document", document_id="doc-1", chunk_id="chunk-1")],
                metadata={"retrieved_chunks": 1},
            )
        }
        evaluation = evaluate_report(report, outputs, request="What serves the backend API?")
        self.assertGreaterEqual(evaluation.groundedness, 90)
        self.assertEqual(evaluation.hallucination_risk, "low")
        self.assertFalse(evaluation.retry_recommended)

    def test_missing_retrieval_sources_triggers_retry(self):
        evaluation = evaluate_report(
            "## Executive Summary\n\nAn unsupported answer.",
            {"rag": AgentResult(summary="An unsupported answer.", metadata={"retrieved_chunks": 2})},
            request="Answer from the uploaded document",
        )
        self.assertLess(evaluation.groundedness, 40)
        self.assertEqual(evaluation.hallucination_risk, "high")
        self.assertTrue(evaluation.retry_recommended)


class FileValidationTests(unittest.TestCase):
    def test_accepts_legacy_excel_upload(self):
        _validate_upload("legacy_report.xls", 10)

    def test_rejects_unsupported_file_type(self):
        with self.assertRaisesRegex(Exception, "Unsupported file type"):
            _validate_upload("payload.exe", 10)

    def test_rejects_oversized_file(self):
        from app.config import get_settings

        with self.assertRaisesRegex(Exception, "maximum upload size"):
            _validate_upload("notes.txt", get_settings().max_upload_size + 1)


if __name__ == "__main__":
    unittest.main()
