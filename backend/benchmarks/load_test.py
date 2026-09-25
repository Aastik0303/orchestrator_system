"""Reproducible HTTP load benchmark for the orchestrator API.

Starts isolated uvicorn server(s) with temporary storage and no paid model
provider (GROQ_API_KEY is blanked; LLM_PROVIDER=fake with a configurable
simulated latency), then measures end-to-end `/api/chat` latency at several
concurrency levels: p50/p95/p99, error rate and throughput.

  --replicas N   start N independent API processes sharing ONE database and
                 round-robin requests across them (models horizontal scaling
                 behind a load balancer; works on every OS).
  --workers N    uvicorn's own multi-process mode (unreliable under load on
                 Windows: its supervisor restarts workers with console-wide
                 CTRL_C events).

Examples (from the backend directory):

    python -m benchmarks.load_test --label after --levels 1,10,25,50
    python -m benchmarks.load_test --label after-llm300 --fake-llm-latency-ms 300
    python -m benchmarks.load_test --label after-llm300-r4 --fake-llm-latency-ms 300 --replicas 4
    python -m benchmarks.load_test --base-url http://127.0.0.1:8002 --label remote

Results are written to benchmarks/results/<label>.json. Numbers depend on the
host; compare runs made on the same machine only.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

BACKEND_DIR = Path(__file__).resolve().parents[1]
RESULTS_DIR = Path(__file__).resolve().parent / "results"

DEFAULT_MESSAGES = [
    "hello, what can you help me with?",
    "Write a python function that parses ISO dates and explain it",
    "Research current vector database trends and compare the options",
    "Analyze this repository architecture and research current patterns, then create a report",
]


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile; 0.0 for an empty sample."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), round(pct / 100.0 * len(ordered) + 0.5)))
    return ordered[rank - 1]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_servers(
    app_dir: Path,
    fake_llm_latency_ms: int,
    workers: int,
    replicas: int,
    storage_dir: str,
    database_url: str | None = None,
) -> tuple[list[subprocess.Popen], list[str]]:
    env = {
        **os.environ,
        "PYTHONPATH": str(app_dir),
        "GROQ_API_KEY": "",
        "LLM_PROVIDER": "fake",
        "FAKE_LLM_LATENCY_MS": str(fake_llm_latency_ms),
        "RUNTIME_DATABASE_PATH": str(Path(storage_dir) / "runtime.db"),
        "STORAGE_ROOT": storage_dir,
        "RATE_LIMIT_PER_MINUTE": "100000",
        "MAX_ACTIVE_RUNS_PER_USER": "1000",
        "LOG_LEVEL": "WARNING",
    }
    # Windows: a hidden, separate console (CREATE_NO_WINDOW) so console control
    # events from the server tree cannot reach this script or its shell, and
    # no terminal windows pop up. (DETACHED_PROCESS must NOT be used: the venv
    # python.exe launcher spawns a child that would then open a new window.)
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    processes: list[subprocess.Popen] = []
    urls: list[str] = []
    for index in range(replicas):
        port = _free_port()
        command = [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(port),
            "--log-level", "warning", "--workers", str(workers),
        ]
        processes.append(
            subprocess.Popen(
                command,
                cwd=storage_dir,
                env=env,
                creationflags=creationflags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=sys.platform != "win32",
            )
        )
        urls.append(f"http://127.0.0.1:{port}")
        if index == 0:
            _wait_healthy(processes[0], urls[0])  # first replica creates the schema
    for process, url in zip(processes, urls):
        _wait_healthy(process, url)
    return processes, urls


def _wait_healthy(process: subprocess.Popen, url: str) -> None:
    deadline = time.time() + 60
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Benchmark server {url} exited during startup.")
        try:
            if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.25)
    raise RuntimeError(f"Benchmark server {url} did not become healthy within 60 seconds.")


def stop_servers(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


async def run_level(
    client: httpx.AsyncClient, base_urls: list[str], concurrency: int, total: int, messages: list[str]
) -> dict:
    semaphore = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    errors: dict[str, int] = {}
    targets = itertools.cycle(base_urls)

    async def one(index: int) -> None:
        message = messages[index % len(messages)]
        base_url = next(targets)
        async with semaphore:
            started = time.perf_counter()
            try:
                response = await client.post(
                    f"{base_url}/api/chat",
                    data={"message": message, "user_id": f"bench-user-{index % 5}"},
                )
                elapsed = (time.perf_counter() - started) * 1000
                if response.status_code == 200:
                    latencies.append(elapsed)
                else:
                    key = f"http_{response.status_code}"
                    errors[key] = errors.get(key, 0) + 1
            except httpx.HTTPError as exc:
                key = exc.__class__.__name__
                errors[key] = errors.get(key, 0) + 1

    wall_started = time.perf_counter()
    await asyncio.gather(*(one(index) for index in range(total)))
    wall_seconds = time.perf_counter() - wall_started
    error_count = sum(errors.values())
    return {
        "concurrency": concurrency,
        "requests": total,
        "successes": len(latencies),
        "errors": errors,
        "error_rate": round(error_count / total, 4) if total else 0.0,
        "p50_ms": round(percentile(latencies, 50), 1),
        "p95_ms": round(percentile(latencies, 95), 1),
        "p99_ms": round(percentile(latencies, 99), 1),
        "mean_ms": round(statistics.fmean(latencies), 1) if latencies else 0.0,
        "max_ms": round(max(latencies), 1) if latencies else 0.0,
        "throughput_rps": round(len(latencies) / wall_seconds, 2) if wall_seconds else 0.0,
        "wall_seconds": round(wall_seconds, 2),
    }


async def benchmark(base_urls: list[str], levels: list[int], requests_per_level: int | None, messages: list[str]) -> list[dict]:
    limits = httpx.Limits(max_connections=max(levels) + 10, max_keepalive_connections=max(levels) + 10)
    async with httpx.AsyncClient(timeout=120, limits=limits) as client:
        # Warm-up: imports, first DB connections, caches.
        await run_level(client, base_urls, 1, 3 * len(base_urls), messages)
        results = []
        for level in levels:
            total = requests_per_level or max(20, level * 2)
            results.append(await run_level(client, base_urls, level, total, messages))
            print(json.dumps(results[-1]), flush=True)
        return results


def write_results(args: argparse.Namespace, results: list[dict]) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "label": args.label,
        "app_dir": Path(args.app_dir).name,
        "fake_llm_latency_ms": args.fake_llm_latency_ms,
        "workers": args.workers,
        "replicas": args.replicas,
        "database": "postgresql" if (args.database_url or "").startswith("postgres") else "sqlite",
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "cpu_count": os.cpu_count(),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "messages": DEFAULT_MESSAGES,
        "levels": results,
    }
    output = RESULTS_DIR / f"{args.label}.json"
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {output}")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", default="run")
    parser.add_argument("--levels", default="1,10,25,50")
    parser.add_argument("--requests-per-level", type=int, default=None)
    parser.add_argument("--base-url", default=None, help="Benchmark an already running server.")
    parser.add_argument("--app-dir", default=str(BACKEND_DIR), help="Directory containing the `app` package.")
    parser.add_argument("--fake-llm-latency-ms", type=int, default=0)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--replicas", type=int, default=1)
    parser.add_argument("--database-url", default=None, help="Run the servers against this database (e.g. PostgreSQL).")
    args = parser.parse_args()

    levels = [int(value) for value in args.levels.split(",") if value.strip()]
    processes: list[subprocess.Popen] = []
    storage = None
    if args.base_url:
        base_urls = [args.base_url]
    else:
        storage = tempfile.mkdtemp(prefix="orchestrator-bench-")
        processes, base_urls = start_servers(
            Path(args.app_dir), args.fake_llm_latency_ms, args.workers, max(1, args.replicas), storage, args.database_url
        )
    try:
        results = asyncio.run(benchmark(base_urls, levels, args.requests_per_level, DEFAULT_MESSAGES))
        write_results(args, results)
    finally:
        stop_servers(processes)
        if storage:
            import shutil

            shutil.rmtree(storage, ignore_errors=True)


if __name__ == "__main__":
    main()
