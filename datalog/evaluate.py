"""Execute a versioned Datalog dataset and optionally enforce a CI baseline."""

import argparse
import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def run(base, project, token, config, baseline=None, max_drop=0):
    def call(path, params=None, body=None):
        query = urlencode({"project": project, **(params or {})})
        request = Request(
            f"{base.rstrip('/')}/api/v1/{path}?{query}",
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        with urlopen(request, timeout=90) as response:
            return json.load(response)

    split = {"split": config["split"]} if config.get("split") is not None else {}
    dataset = call("agents/dataset", {"name": config["dataset"], "version": config["dataset_version"], **split})
    call("agents/experiments", body=config)
    cases = [
        call("agents/experiment-case", body={"id": config["id"], "case_id": case["id"]}) for case in dataset["cases"]
    ]
    result = {"run": config["id"], "cases": cases}
    if baseline is None:
        return result, any(case["status"] != "ok" for case in cases)
    comparison = call(
        "agents/compare",
        {
            "dataset": config["dataset"],
            "version": config["dataset_version"],
            "before": baseline,
            "after": config["execution_version"],
            "criterion": config["criterion"],
            **split,
        },
    )
    regressions = []
    for case in comparison["cases"]:
        before, after = case["before"], case["after"]
        if before is None or after is None or before["score"] - after["score"] > max_drop:
            current = next(item for item in cases if item["case_id"] == case["id"])
            query = urlencode({"project": project, "id": current["trace_id"], "span": current["span_id"]})
            regressions.append(
                {"case_id": case["id"], "before": before, "after": after, "url": f"{base.rstrip('/')}/#traces?{query}"}
            )
    return {**result, "comparison": comparison, "regressions": regressions}, bool(regressions)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:4310")
    parser.add_argument("--project", required=True)
    parser.add_argument("--config", required=True, type=argparse.FileType(encoding="utf8"))
    parser.add_argument("--baseline")
    parser.add_argument("--max-drop", type=float, default=0)
    args = parser.parse_args()
    token = os.environ.get("DATALOG_EVALUATION_TOKEN")
    if not token or not 0 <= args.max_drop <= 1:
        parser.error("Configure DATALOG_EVALUATION_TOKEN and a max-drop between 0 and 1")
    result, failed = run(args.url, args.project, token, json.load(args.config), args.baseline, args.max_drop)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
