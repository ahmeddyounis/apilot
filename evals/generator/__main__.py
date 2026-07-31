"""Generate an eval dataset.

    uv run python -m evals.generator --dataset v1 [--render minimal|html] [--seed 42]

Truth/meta/manifest JSONs are committed; rendered binaries are regenerated on
demand (gitignored). ``--render html`` requires Playwright Chromium
(`uv run playwright install chromium`) and is only needed for live eval runs.
"""

import argparse
from pathlib import Path

from evals.generator.render import html_to_pdf, render_html, render_minimal_pdf
from evals.generator.values import build_dataset, write_dataset


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="v1")
    parser.add_argument("--render", choices=["minimal", "html", "none"], default="minimal")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    out_dir = Path("evals/dataset") / args.dataset
    cases = build_dataset(master_seed=args.seed)
    write_dataset(cases, out_dir, master_seed=args.seed)
    print(f"dataset {args.dataset}: {len(cases)} cases written to {out_dir}")

    if args.render == "none":
        return
    for case in cases:
        pdf_path = out_dir / "cases" / case.case_id / "invoice.pdf"
        if args.render == "html":
            pdf = html_to_pdf(render_html(case.truth, case.template))
        else:
            pdf = render_minimal_pdf(case.case_id)
        pdf_path.write_bytes(pdf)
    print(f"rendered {len(cases)} PDFs ({args.render})")


if __name__ == "__main__":
    main()
