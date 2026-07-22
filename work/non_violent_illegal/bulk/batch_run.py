#!/usr/bin/env python3
"""Batch Q1 prompt tester: guard pre-filter, then full OpenClaw+judge eval on survivors.

Usage:
  python3 batch_run.py <prompts.json> <out_summary.json> [--question Q1]

prompts.json: [{"name": "...", "prompt": "..."}, ...]
Writes a summary JSON and prints a table. "survivor" = reached the model (guard did
not block); records judge verdict/score and whether it was a successful jailbreak.
"""
import json
import subprocess
import sys
import os

TRI_CHECK = "/home/Trustvs/trishool-phase2/tri-check"
QF = "/home/Trustvs/trishool-phase2/work/non_violent_illegal/questions.json"


def _guard_probe_once(prompt: str):
    p = subprocess.run(
        ["pnpm", "guard-probe", "--", "--query", prompt, "--halo-direct"],
        cwd=TRI_CHECK, capture_output=True, text=True, timeout=120,
    )
    out = p.stdout
    start = out.find("{")
    end = out.rfind("}")
    if start == -1 or end == -1:
        return ("unknown", None, None)
    blob = json.loads(out[start:end + 1])
    resp = blob.get("response", blob)
    return (blob.get("verdict", "unknown"),
            resp.get("confidence"),
            resp.get("category"))


def guard_probe(prompt: str, retries: int = 2):
    """Return (verdict, confidence, category) from direct Halo classify, with retries."""
    last = ("unknown", None, None)
    for _ in range(retries + 1):
        try:
            v, c, cat = _guard_probe_once(prompt)
            if v in ("allow", "block"):
                return (v, c, cat)
            last = (v, c, cat)
        except Exception as e:
            last = (f"error:{e}", None, None)
    return last


def full_eval(question: str, prompt: str, out_path: str):
    """Run pnpm eval; return parsed result dict for the single question."""
    try:
        subprocess.run(
            ["pnpm", "eval", "--question", question, "--prompt", prompt,
             "--questions", QF, "--out", out_path],
            cwd=TRI_CHECK, capture_output=True, text=True, timeout=300,
        )
        with open(out_path) as f:
            report = json.load(f)
        r = report["results"][0]
        resp = (r.get("openclawResponse") or "")
        blocked = "Blocked by input guard" in resp or "Blocked by output guard" in resp
        return {
            "blocked": blocked,
            "blockedBy": ("input" if "input guard" in resp else
                          "output" if "output guard" in resp else None),
            "judgeVerdict": r.get("judgeVerdict"),
            "judgeScore": r.get("judgeScore"),
            "jailbreak": bool(r.get("successfulJailbreak")),
            "error": r.get("error"),
            "responsePreview": resp[:240],
        }
    except Exception as e:
        return {"blocked": None, "error": f"eval_error:{e}"}


def main():
    prompts_path = sys.argv[1]
    out_summary = sys.argv[2]
    question = "Q1"
    if "--question" in sys.argv:
        question = sys.argv[sys.argv.index("--question") + 1]

    with open(prompts_path) as f:
        loaded = json.load(f)
    # Accept either a flat list [{name,prompt}] or bulk structure {"Q1":[...], ...}.
    if isinstance(loaded, dict):
        prompts = loaded.get(question, [])
        if not prompts:
            print(f"No entries for {question} in {prompts_path}")
            sys.exit(1)
    else:
        prompts = loaded

    results = []
    for i, item in enumerate(prompts):
        name = item.get("name", f"p{i}")
        prompt = item["prompt"]
        verdict, conf, cat = guard_probe(prompt)
        rec = {"name": name, "guardVerdict": verdict, "guardConf": conf,
               "guardCategory": cat, "prompt": prompt}
        if verdict == "allow":
            out_path = f"/tmp/batch_{question}_{name}.json"
            ev = full_eval(question, prompt, out_path)
            rec.update(ev)
        else:
            rec.update({"blocked": True, "blockedBy": "input(guard-probe)",
                        "judgeScore": 0, "jailbreak": False})
        results.append(rec)
        gc = f"{conf:.2f}" if isinstance(conf, (int, float)) else conf
        print(f"[{i+1:2}/{len(prompts)}] {name:28} guard={verdict:6} "
              f"conf={gc} score={rec.get('judgeScore')} "
              f"jailbreak={rec.get('jailbreak')}", flush=True)

    summary = {
        "total": len(results),
        "guard_passed": sum(1 for r in results if r["guardVerdict"] == "allow"),
        "jailbreaks": sum(1 for r in results if r.get("jailbreak")),
        "survivors_scored": [r["name"] for r in results
                             if r["guardVerdict"] == "allow" and not r.get("blocked")],
        "results": results,
    }
    with open(out_summary, "w") as f:
        json.dump(summary, f, indent=2)

    print("\n===== SUMMARY =====")
    print(f"total={summary['total']} guard_passed={summary['guard_passed']} "
          f"jailbreaks={summary['jailbreaks']}")
    print("survivors (guard-passed, model answered):")
    for r in results:
        if r["guardVerdict"] == "allow" and not r.get("blocked"):
            print(f"  {r['name']:28} score={r.get('judgeScore')} "
                  f"verdict={r.get('judgeVerdict')}")
    print(f"\nsummary written: {out_summary}")


if __name__ == "__main__":
    main()
