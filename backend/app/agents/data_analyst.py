from pathlib import Path

import pandas as pd

from app.models import AgentName, AgentResponse, ChatRequest

OUTPUT_DIR = Path("storage/outputs")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def data_analyst_agent(request: ChatRequest) -> AgentResponse:
    artifacts: list[dict[str, str]] = []
    summaries: list[str] = []

    for uploaded in request.files:
        if not uploaded.storage_path:
            continue

        source_path = Path(uploaded.storage_path)
        suffix = source_path.suffix.lower()

        if suffix == ".csv":
            dataframe = pd.read_csv(source_path)
            output_path = OUTPUT_DIR / f"{source_path.stem}_processed.csv"
            dataframe.to_csv(output_path, index=False)
        elif suffix in {".xlsx", ".xls"}:
            dataframe = pd.read_excel(source_path)
            output_path = OUTPUT_DIR / f"{source_path.stem}_processed.xlsx"
            dataframe.to_excel(output_path, index=False)
        else:
            summaries.append(f"Skipped unsupported data file: {uploaded.name}")
            continue

        summaries.append(
            f"{uploaded.name}: {len(dataframe)} rows, {len(dataframe.columns)} columns."
        )
        artifacts.append(
            {
                "name": output_path.name,
                "path": str(output_path),
                "type": "processed_data",
            }
        )

    if not summaries:
        summaries.append("No supported CSV or Excel file was provided.")

    return AgentResponse(
        active_agent=AgentName.DATA_ANALYST,
        response=(
            "Data Analyst Agent active. Original files were left unchanged. "
            + " ".join(summaries)
        ),
        artifacts=artifacts,
    )

