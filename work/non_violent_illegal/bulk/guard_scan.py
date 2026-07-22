#!/usr/bin/env python3
"""Input-guard-only diagnostic across all questions.

Probes every prompt in a bulk corpus through the direct Halo classify and
reports, per question: pass count (guard=allow), block count, and the
confidence distribution. No full eval -- the input guard is the bottleneck.

Usage:
  python3 guard_scan.py <corpus.json> <out.json> [Q1 Q5 ...]
"""
import json
import subprocess
import sys
import statistics

TRI_CHECK = "/home/Trustvs/trishool-phase2/tri-check"


def probe_once(prompt: str):
    p = subprocess.run(
        ["pnpm", "guard-probe", "--", "--query", prompt, "--halo-direct"],
        cwd=TRI_CHECK, capture_output=True, text=True, timeout=120,
    )
    out = p.stdout
    s, e = out.find("{"), out.rfind("}")
    if s == -1 or e == -1:
        return ("unknown", None, None)
    blob = json.loads(out[s:e + 1])
    resp = blob.get("response", blob)
    return (blob.get("verdict", "unknown"), resp.get("confidence"),
            resp.get("category"))


def probe(prompt: str, retries: int = 2):
    last = ("unknown", None, None)
    for _ in range(retries + 1):
        try:
            v, c, cat = probe_once(prompt)
            if v in ("allow", "block"):
                return (v, c, cat)
            last = (v, c, cat)
        except Exception as ex:
            last = (f"error:{ex}", None, None)
    return last


def main():
    corpus_path, out_path = sys.argv[1], sys.argv[2]
    d = json.load(open(corpus_path))
    questions = sys.argv[3:] or sorted(d, key=lambda x: (len(x), x))

    report = {}
    for q in questions:
        prompts = d.get(q, [])
        rows, allows = [], []
        for i, item in enumerate(prompts):
            name = item.get("name", f"p{i}")
            v, c, cat = probe(item["prompt"])
            rows.append({"name": name, "verdict": v, "conf": c, "cat": cat})
            gc = f"{c:.3f}" if isinstance(c, (int, float)) else c
            if v == "allow":
                allows.append(name)
            print(f"[{q}][{i+1:2}/{len(prompts)}] {name:26} "
                  f"{v:6} conf={gc} {cat}", flush=True)
        confs = [r["conf"] for r in rows
                 if isinstance(r["conf"], (int, float))]
        report[q] = {
            "total": len(prompts),
            "allow": sum(1 for r in rows if r["verdict"] == "allow"),
            "block": sum(1 for r in rows if r["verdict"] == "block"),
            "min_conf": min(confs) if confs else None,
            "mean_conf": round(statistics.mean(confs), 4) if confs else None,
            "allow_names": allows,
            "rows": rows,
        }
        print(f"  ==> {q}: allow={report[q]['allow']}/{report[q]['total']} "
              f"min_conf={report[q]['min_conf']}", flush=True)
        json.dump(report, open(out_path, "w"), indent=2)

    print("\n===== GUARD SCAN SUMMARY =====")
    for q in questions:
        r = report[q]
        print(f"{q:4} allow={r['allow']:2}/{r['total']:2} "
              f"min_conf={r['min_conf']} mean_conf={r['mean_conf']} "
              f"survivors={r['allow_names']}")
    json.dump(report, open(out_path, "w"), indent=2)
    print(f"\nwritten: {out_path}")


if __name__ == "__main__":
    main()
