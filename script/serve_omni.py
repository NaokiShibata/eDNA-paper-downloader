"""Local text-only SystemOne adapter for the verified Clef-Omni FP16 GPU split."""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from libs.screen_servers import OMNI_MODEL, OMNI_REVISION, ROOT


def load_model(model_dir: Path, runtime_dir: Path) -> tuple[Any, Any, Any]:
    # Keep the validated Transformers version isolated from the Strands environment.
    sys.path.insert(0, str(runtime_dir))
    import torch
    import transformers
    from safetensors.torch import load_file
    from transformers import AutoConfig, AutoTokenizer, Qwen3OmniMoeForConditionalGeneration

    if transformers.__version__ != "4.57.3":
        raise RuntimeError("Clef Omni needs the isolated Transformers 4.57.3 runtime; see docs/screen-models.md")
    if torch.cuda.device_count() != 2:
        raise RuntimeError("Clef Omni FP16 needs exactly two visible CUDA GPUs")
    metadata = model_dir / ".cache/huggingface/download/config.json.metadata"
    if not metadata.is_file() or metadata.read_text().splitlines()[0] != OMNI_REVISION:
        raise RuntimeError(f"download the pinned Clef Omni revision {OMNI_REVISION}; see docs/screen-models.md")
    torch.set_num_threads(4)
    transformers.utils.logging.set_verbosity_error()
    spec = importlib.util.spec_from_file_location("clef_omni_release", model_dir / "joint_schema_model.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("missing Clef Omni release code")
    release: Any = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = release
    spec.loader.exec_module(release)

    class SplitClef(release.ClefModel):
        def _forward_unpadded(self, batch: dict[str, Any]) -> Any:
            if batch.get("media"):
                raise ValueError("this server supports text only")
            ids = batch["input_ids"].to("cuda:0")
            mask = batch["attention_mask"].to("cuda:0")
            outputs = self.thinker(input_ids=ids, attention_mask=mask, use_cache=False,
                                   output_router_logits=False, use_audio_in_video=False, return_dict=True)
            return self.head(outputs.logits.to("cuda:0"), ids, mask,
                             batch["records"], self.output_embeddings.weight)

    device_map: dict[str, int] = {
        "thinker.audio_tower": 0, "thinker.visual": 0, "thinker.model.embed_tokens": 0,
        "thinker.model.rotary_emb": 0, "thinker.model.norm": 1, "thinker.lm_head": 0,
    }
    device_map.update({f"thinker.model.layers.{i}": 0 if i < 37 else 1 for i in range(48)})
    config = AutoConfig.from_pretrained(model_dir, local_files_only=True)
    config.enable_audio_output = False
    loaded: Any = Qwen3OmniMoeForConditionalGeneration.from_pretrained(
        model_dir, config=config, dtype=torch.float16, device_map=device_map,
        attn_implementation="sdpa", local_files_only=True, output_loading_info=True,
    )
    backbone, info = loaded
    problems = {"missing": info.get("missing_keys") or [],
                "unexpected": [k for k in info.get("unexpected_keys", []) if not k.startswith(("talker.", "code2wav."))],
                "mismatched": [str(k) for k in info.get("mismatched_keys", [])]}
    if any(problems.values()):
        raise RuntimeError(f"Clef Omni weights did not load cleanly: {problems}")
    head = release.JointSchemaHead(**json.loads((model_dir / "joint_head_config.json").read_text()))
    head.load_state_dict(load_file(model_dir / "joint_head.safetensors"), strict=True)
    # Leave more room on the display GPU than the pilot's head-on-GPU-1 placement.
    head.to(device="cuda:0", dtype=torch.float16)
    model = SplitClef(backbone.thinker, head).eval()
    if getattr(backbone, "is_quantized", False) or any(p.device.type != "cuda" or p.dtype != torch.float16 for p in model.parameters()):
        raise RuntimeError("Clef Omni must have unquantized FP16 parameters on both GPUs")
    processor = SimpleNamespace(tokenizer=AutoTokenizer.from_pretrained(model_dir, local_files_only=True))
    return model, processor, release


def handler_for(model: Any, processor: Any, release: Any) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path != "/health":
                self.send_json(404, {"error": "unknown endpoint"})
                return
            self.send_json(200, {"status": "ok", "model": OMNI_MODEL, "device": "cuda:0,cuda:1",
                                 "dtype": "float16", "revision": OMNI_REVISION, "max_length": 4096})

        def do_POST(self) -> None:
            if self.path != "/v1/systemone":
                self.send_json(404, {"error": "unknown endpoint"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2 * 1024**2:
                    raise ValueError("request body must be between 1 byte and 2 MiB")
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict):
                    raise ValueError("request must be a JSON object")
                if request.get("model", OMNI_MODEL) != OMNI_MODEL:
                    raise ValueError(f"expected model {OMNI_MODEL}")
                if any(request.get(key) for key in ("images", "audio", "videos")):
                    raise ValueError("this server supports text only")
                request["model"] = OMNI_MODEL
            except (ValueError, UnicodeError) as exc:
                self.send_json(400, {"error": str(exc)})
                return
            try:
                started = time.perf_counter()
                response = release.systemone(model, processor, request)
                response["latency_ms"] = (time.perf_counter() - started) * 1000
                self.send_json(200, response)
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
            except Exception as exc:
                logging.exception("Omni inference failed")
                self.send_json(500, {"error": str(exc)})

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, default=ROOT / ".cache/clef-omni")
    parser.add_argument("--runtime-dir", type=Path, default=ROOT / ".cache/omni-runtime")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8016)
    args = parser.parse_args()
    model, processor, release = load_model(args.model_dir, args.runtime_dir)
    with HTTPServer((args.host, args.port), handler_for(model, processor, release)) as server:
        print(f"Ready model={OMNI_MODEL} url=http://{args.host}:{args.port} dtype=FP16 GPUs=2", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
