#!/usr/bin/env python3
"""Probe a bulk corpus through Halo with bounded parallelism and retries."""

import concurrent.futures
import json
import subprocess
import sys
import time
from pathlib import Path

TRI_CHECK = Path("/home/Trustvs/trishool-phase2/tri-check")


def probe(prompt):
    last = ("unknown", None, None)
    for attempt in range(3):
        try:
            result = subprocess.run(
                ["pnpm", "guard-probe", "--", "--query", prompt, "--halo-direct"],
                cwd=TRI_CHECK,
                capture_output=True,
                text=True,
                timeout=120,
            )
            start, end = result.stdout.find("{"), result.stdout.rfind("}")
            if start >= 0:
                blob = json.loads(result.stdout[start : end + 1])
                response = blob.get("response", blob)
                verdict = blob.get("verdict", "unknown")
                last = (
                    verdict,
                    response.get("confidence"),
                    response.get("category"),
                )
                if verdict in ("allow", "block"):
                    return last
        except Exception:
            pass
        time.sleep(attempt + 1)
    return last


def main():
    corpus_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    requested = sys.argv[3:]
    corpus = json.loads(corpus_path.read_text())
    questions = requested or [f"Q{i}" for i in range(1, 13)]
    report = {}

    for question in questions:
        items = corpus[question]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda item: probe(item["prompt"]), items))

        rows = []
        for i, (item, result) in enumerate(zip(items, results), start=1):
            verdict, confidence, category = result
            rows.append(
                {
                    "name": item["name"],
                    "verdict": verdict,
                    "conf": confidence,
                    "cat": category,
                }
            )
            print(
                f"[{question}][{i}/10] {verdict} conf={confidence} cat={category}",
                flush=True,
            )

        report[question] = {
            "allow": sum(row["verdict"] == "allow" for row in rows),
            "block": sum(row["verdict"] == "block" for row in rows),
            "unknown": sum(row["verdict"] == "unknown" for row in rows),
            "total": len(rows),
            "rows": rows,
        }
        print(
            f"==> {question}: {report[question]['allow']}/{len(rows)} allow",
            flush=True,
        )
        output_path.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
