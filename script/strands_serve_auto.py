from __future__ import annotations

import argparse
import os
import sys


MODEL = "StrandsAgents/strands-decider-2B-hobson-v19"


def _detect_compute(preferred_gpu: int) -> tuple[str, int | None, str]:
    try:
        import torch
    except Exception as exc:
        return "cpu", None, f"PyTorch CUDA detection failed: {exc}"

    try:
        cuda_available = bool(torch.cuda.is_available())
        gpu_count = int(torch.cuda.device_count()) if cuda_available else 0
    except Exception as exc:
        return "cpu", None, f"CUDA detection failed: {exc}"

    if not cuda_available or gpu_count <= 0:
        return "cpu", None, "No CUDA GPU detected"

    selected = preferred_gpu if 0 <= preferred_gpu < gpu_count else 0
    try:
        name = str(torch.cuda.get_device_name(selected))
    except Exception:
        name = "unknown GPU"

    if selected == preferred_gpu:
        reason = f"CUDA GPU {selected} selected: {name}"
    else:
        reason = (
            f"Preferred GPU {preferred_gpu} is unavailable; "
            f"falling back to GPU {selected}: {name}"
        )
    return "cuda", selected, reason


def _print_selection(device: str, gpu_index: int | None, reason: str) -> None:
    print(f"[strands-serve] {reason}", flush=True)
    if device == "cuda":
        assert gpu_index is not None
        print(
            "[strands-serve] "
            f"Using physical GPU {gpu_index}; Strands will see it as logical cuda:0.",
            flush=True,
        )
    else:
        print(
            "[strands-serve] WARNING: Falling back to CPU explicitly (--device cpu). "
            "Inference will be slower than CUDA.",
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Select GPU 1 -> GPU 0 -> CPU, then start Strands Decider."
    )
    parser.add_argument("--preferred-gpu", type=int, default=1)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8012)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Print the selected compute device without starting the server.",
    )
    args = parser.parse_args()

    if args.preferred_gpu < 0:
        parser.error("--preferred-gpu must be >= 0")

    device, gpu_index, reason = _detect_compute(args.preferred_gpu)
    _print_selection(device, gpu_index, reason)

    if args.check_only:
        return

    env = os.environ.copy()
    if device == "cuda":
        assert gpu_index is not None
        env["CUDA_VISIBLE_DEVICES"] = str(gpu_index)
        env["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
        strands_device = "cuda"
    else:
        env.pop("CUDA_VISIBLE_DEVICES", None)
        env.pop("PYTORCH_CUDA_ALLOC_CONF", None)
        strands_device = "cpu"

    cmd = [
        "strands-decider",
        "serve",
        args.model,
        "--device",
        strands_device,
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    print(
        "[strands-serve] Starting: "
        f"model={args.model} device={strands_device} host={args.host} port={args.port}",
        flush=True,
    )

    try:
        os.execvpe(cmd[0], cmd, env)
    except FileNotFoundError:
        print(
            "[strands-serve] ERROR: strands-decider executable was not found in PATH.",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(127)


if __name__ == "__main__":
    main()
