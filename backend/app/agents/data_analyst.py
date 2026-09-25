from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from app.agents.context import AgentContext
from app.agents.registry import ModelPolicy, PermissionPolicy, RetryPolicy, RoutingHints, register_agent
from app.config import get_settings
from app.models import AgentResult, AgentTask, ChatRequest, UploadedFile
from app.services.runtime_store import runtime_store

# Overridable in tests; defaults to STORAGE_ROOT/outputs.
OUTPUT_DIR: Path | None = None
DATA_EXTENSIONS = {".csv", ".xlsx", ".xls"}
MAX_STORED_DATASETS = 3
MAX_ROWS_PROFILED = 2_000_000


def _output_dir() -> Path:
    directory = OUTPUT_DIR or get_settings().outputs_dir
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@register_agent(
    name="data_analyst",
    description="Profiles CSV and Excel data quality, statistics, outliers, and correlations (deterministic, no LLM).",
    capabilities=["data_analysis"],
    tools=["data.inspect_dataset", "data.calculate_statistics"],
    timeout_seconds=90,
    retry_policy=RetryPolicy(max_retries=0),
    model_policy=ModelPolicy(tier="none"),
    token_budget=0,
    permission_policy=PermissionPolicy(granted_permissions={"files:read"}),
    routing_hints={
        "data_analysis": RoutingHints(
            description="Analyze tabular datasets (CSV/Excel): quality, statistics, trends.",
            file_extensions={".csv", ".xlsx", ".xls"},
        )
    },
)
async def data_analyst(task: AgentTask, ctx: AgentContext) -> AgentResult:
    # pandas work is CPU/disk bound: keep it off the event loop.
    return await asyncio.to_thread(data_analyst_agent, task.request())


def data_analyst_agent(request: ChatRequest) -> AgentResult:
    artifacts: list[dict[str, Any]] = []
    findings: list[str] = []
    recommendations: list[str] = []
    warnings: list[str] = []
    profiles: list[dict[str, Any]] = []

    datasets = _resolve_datasets(request)
    if not request.files and datasets:
        findings.append(
            f"Using {len(datasets)} stored dataset file{'s' if len(datasets) != 1 else ''} "
            "from the current project because none were attached to this request."
        )

    for uploaded in datasets:
        if not uploaded.storage_path:
            warnings.append(f"No storage path was available for {uploaded.name}.")
            continue
        source_path = Path(uploaded.storage_path)
        if not source_path.is_file():
            warnings.append(f"Stored dataset is missing: {uploaded.name}.")
            continue
        try:
            frames = _load_frames(source_path)
        except Exception as exc:
            warnings.append(f"Could not read {uploaded.name}: {exc}")
            continue

        file_profiles = []
        for sheet_name, dataframe in frames.items():
            profile = _profile_frame(dataframe, file_name=uploaded.name, sheet_name=sheet_name)
            profiles.append(profile)
            file_profiles.append(profile)
            findings.extend(_render_profile_findings(profile))
            recommendations.extend(_profile_recommendations(profile))

        artifact_path = _output_dir() / f"{source_path.stem}_{uuid4().hex[:8]}_profile.json"
        artifact_path.write_text(
            json.dumps(
                {
                    "question": request.message,
                    "source_file": uploaded.name,
                    "profiles": file_profiles,
                },
                indent=2,
                ensure_ascii=True,
            ),
            encoding="utf-8",
        )
        artifacts.append(
            {
                "name": artifact_path.name,
                "path": str(artifact_path),
                "type": "data_profile",
            }
        )

    if not profiles:
        if not warnings:
            warnings.append(
                "Attach a CSV/Excel file or upload one to Knowledge before asking for dataset analysis."
            )
        return AgentResult(
            summary="No supported CSV or Excel dataset was available for analysis.",
            warnings=list(dict.fromkeys(warnings)),
        )

    total_rows = sum(profile["rows"] for profile in profiles)
    average_quality = sum(profile["quality_score"] for profile in profiles) / len(profiles)
    strongest = max(
        (correlation for profile in profiles for correlation in profile["correlations"]),
        key=lambda item: abs(item["coefficient"]),
        default=None,
    )
    summary = (
        f"Analyzed {len(profiles)} dataset table{'s' if len(profiles) != 1 else ''} "
        f"covering {total_rows:,} rows. Average data quality was {average_quality:.1f}/100."
    )
    if strongest:
        summary += (
            f" The strongest numeric relationship was {strongest['left']} versus "
            f"{strongest['right']} ({strongest['coefficient']:+.2f})."
        )

    return AgentResult(
        summary=summary,
        findings=list(dict.fromkeys(findings)),
        recommendations=list(dict.fromkeys(recommendations)),
        artifacts=artifacts,
        warnings=list(dict.fromkeys(warnings)),
        metadata={
            "tables_analyzed": len(profiles),
            "rows_analyzed": total_rows,
            "average_quality_score": round(average_quality, 2),
            "profile_format": "json",
        },
    )


def _resolve_datasets(request: ChatRequest) -> list[UploadedFile]:
    attached = [file for file in request.files if _is_supported_dataset(file.name)]
    if attached:
        return attached

    try:
        documents = runtime_store.list_documents(
            user_id=request.user_id,
            project_id=request.project_id,
        )
    except Exception:
        return []

    datasets: list[UploadedFile] = []
    for document in documents:
        name = str(document.get("name") or "")
        storage_path = document.get("storage_path")
        if not _is_supported_dataset(name):
            continue
        if len(datasets) >= MAX_STORED_DATASETS:
            break
        datasets.append(
            UploadedFile(
                name=name,
                content_type=document.get("content_type"),
                storage_path=str(storage_path) if storage_path else None,
                document_id=str(document.get("id")) if document.get("id") else None,
            )
        )
    return datasets


def _is_supported_dataset(name: str) -> bool:
    return Path(name).suffix.lower() in DATA_EXTENSIONS


def _load_frames(path: Path) -> dict[str, pd.DataFrame]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return {"data": pd.read_csv(path, low_memory=False, nrows=MAX_ROWS_PROFILED)}
    if suffix in {".xlsx", ".xls"}:
        return {
            str(sheet): frame
            for sheet, frame in pd.read_excel(path, sheet_name=None).items()
        }
    raise ValueError(f"Unsupported data file type: {suffix or 'none'}")


def _profile_frame(
    dataframe: pd.DataFrame, *, file_name: str, sheet_name: str
) -> dict[str, Any]:
    rows, columns = dataframe.shape
    total_cells = rows * columns
    missing_by_column = dataframe.isna().sum().sort_values(ascending=False)
    missing_cells = int(missing_by_column.sum())
    duplicate_rows = int(dataframe.duplicated().sum()) if rows else 0
    missing_percentage = 100.0 * missing_cells / total_cells if total_cells else 0.0
    duplicate_percentage = 100.0 * duplicate_rows / rows if rows else 0.0
    unnamed_columns = sum(str(column).lower().startswith("unnamed:") for column in dataframe.columns)
    unnamed_percentage = 100.0 * unnamed_columns / columns if columns else 0.0
    quality_score = max(
        0.0,
        100.0
        - missing_percentage * 0.65
        - duplicate_percentage * 0.25
        - unnamed_percentage * 0.10,
    )

    numeric = dataframe.select_dtypes(include="number")
    numeric_summaries: dict[str, dict[str, Any]] = {}
    outliers: dict[str, int] = {}
    for column in numeric.columns[:20]:
        series = numeric[column].dropna()
        if series.empty:
            continue
        numeric_summaries[str(column)] = {
            "count": int(series.count()),
            "mean": _safe_number(series.mean()),
            "median": _safe_number(series.median()),
            "minimum": _safe_number(series.min()),
            "maximum": _safe_number(series.max()),
            "standard_deviation": _safe_number(series.std(ddof=1)),
        }
        first_quartile = series.quantile(0.25)
        third_quartile = series.quantile(0.75)
        iqr = third_quartile - first_quartile
        if pd.notna(iqr) and iqr > 0:
            count = int(
                ((series < first_quartile - 1.5 * iqr) | (series > third_quartile + 1.5 * iqr)).sum()
            )
            if count:
                outliers[str(column)] = count

    correlations: list[dict[str, Any]] = []
    if len(numeric.columns) >= 2:
        matrix = numeric.corr()
        columns_list = list(matrix.columns)
        for left_index, left in enumerate(columns_list):
            for right in columns_list[left_index + 1 :]:
                coefficient = matrix.loc[left, right]
                if pd.notna(coefficient) and abs(float(coefficient)) >= 0.3:
                    correlations.append(
                        {
                            "left": str(left),
                            "right": str(right),
                            "coefficient": round(float(coefficient), 4),
                        }
                    )
    correlations.sort(key=lambda item: abs(item["coefficient"]), reverse=True)

    categorical = dataframe.select_dtypes(exclude="number")
    categorical_summaries: dict[str, dict[str, Any]] = {}
    for column in categorical.columns[:20]:
        series = categorical[column].dropna().astype(str)
        counts = series.value_counts().head(5)
        categorical_summaries[str(column)] = {
            "unique_values": int(series.nunique()),
            "top_values": {str(key): int(value) for key, value in counts.items()},
        }

    return {
        "file_name": file_name,
        "sheet_name": sheet_name,
        "rows": rows,
        "columns": columns,
        "column_names": [str(column) for column in dataframe.columns],
        "column_types": {str(column): str(dtype) for column, dtype in dataframe.dtypes.items()},
        "missing_cells": missing_cells,
        "missing_percentage": round(missing_percentage, 2),
        "missing_by_column": {
            str(column): int(value)
            for column, value in missing_by_column.items()
            if int(value) > 0
        },
        "duplicate_rows": duplicate_rows,
        "duplicate_percentage": round(duplicate_percentage, 2),
        "quality_score": round(quality_score, 2),
        "numeric_summaries": numeric_summaries,
        "categorical_summaries": categorical_summaries,
        "outliers": outliers,
        "correlations": correlations[:20],
    }


def _render_profile_findings(profile: dict[str, Any]) -> list[str]:
    location = profile["file_name"]
    if profile["sheet_name"] != "data":
        location += f" / {profile['sheet_name']}"
    findings = [
        f"{location}: {profile['rows']:,} rows, {profile['columns']} columns, "
        f"quality score {profile['quality_score']:.1f}/100.",
        f"{location}: {profile['missing_percentage']:.1f}% missing cells and "
        f"{profile['duplicate_rows']:,} duplicate rows.",
    ]
    for column, summary in list(profile["numeric_summaries"].items())[:6]:
        findings.append(
            f"{location} / {column}: mean {summary['mean']}, median {summary['median']}, "
            f"range {summary['minimum']} to {summary['maximum']}."
        )
    for correlation in profile["correlations"][:3]:
        findings.append(
            f"{location}: correlation {correlation['left']} vs {correlation['right']} "
            f"is {correlation['coefficient']:+.2f}."
        )
    return findings


def _profile_recommendations(profile: dict[str, Any]) -> list[str]:
    recommendations = []
    if profile["missing_cells"]:
        top_missing = list(profile["missing_by_column"].items())[:5]
        rendered = ", ".join(f"{column} ({count})" for column, count in top_missing)
        recommendations.append(f"Review or impute missing values, led by: {rendered}.")
    if profile["duplicate_rows"]:
        recommendations.append(
            f"Validate and remove {profile['duplicate_rows']:,} duplicate rows if they are not intentional."
        )
    if profile["outliers"]:
        rendered = ", ".join(
            f"{column} ({count})" for column, count in list(profile["outliers"].items())[:5]
        )
        recommendations.append(f"Inspect possible IQR outliers in: {rendered}.")
    if not profile["numeric_summaries"]:
        recommendations.append("No numeric columns were detected; validate imported column types.")
    return recommendations


def _safe_number(value: Any) -> float | int | None:
    if pd.isna(value):
        return None
    numeric = float(value)
    if not math.isfinite(numeric):
        return None
    if numeric.is_integer():
        return int(numeric)
    return round(numeric, 4)
