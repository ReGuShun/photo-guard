"""MusePose + Dormant protection frontend.

The optional Dormant step is strict: when enabled, missing protection weights
stop the job instead of silently bypassing protection.
"""

import datetime as dt
import base64
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import time
import threading
from pathlib import Path

# Keep Gradio output files in the persistent project directory. The remote
# browser proxy cannot reliably reach its default /tmp/gradio location.
PROJECT_ROOT = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("PHOTO_PROJECT_ROOT", str(PROJECT_ROOT))).expanduser().resolve()
os.environ.setdefault("GRADIO_TEMP_DIR", str(ROOT / "web_frontend_runs/gradio"))

import gradio as gr
import imageio.v3 as iio
import numpy as np
from PIL import Image
from fastapi import HTTPException
from fastapi.responses import FileResponse


LIVE_LOG = threading.local()

VIDEO_PROXY_JS = r"""
() => {
  const rewriteVideoSources = () => {
    document.querySelectorAll('video').forEach((video) => {
      const source = video.getAttribute('src');
      if (!source || video.dataset.proxyRewritten === '1') return;
      try {
        const url = new URL(source, window.location.href);
        const internalHost = (url.hostname === '127.0.0.1' || url.hostname === 'localhost') && url.port === '6006';
        const gradioApiFile = url.pathname.startsWith('/gradio_api/file=');
        const legacyTmpFile = url.pathname.startsWith('/file=/tmp/gradio/');
        if (internalHost || gradioApiFile || legacyTmpFile) {
          if (gradioApiFile) {
            url.pathname = '/file=' + url.pathname.slice('/gradio_api/file='.length);
          }
          if (legacyTmpFile) {
            url.pathname = '/gradio_api/file=/tmp/gradio/' + url.pathname.slice('/file=/tmp/gradio/'.length);
          }
          if (internalHost) {
            url.protocol = window.location.protocol;
            url.host = window.location.host;
          }
          video.src = url.toString();
          video.dataset.proxyRewritten = '1';
          video.load();
        }
      } catch (_) {}
    });
  };
  rewriteVideoSources();
  new MutationObserver(rewriteVideoSources).observe(document.body, {childList: true, subtree: true, attributes: true, attributeFilter: ['src']});
  setInterval(rewriteVideoSources, 1000);
}
"""


def no_progress(*args, **kwargs):
    pass


def embedded_video(path: str | Path | None, label: str) -> str:
    """Return a self-contained browser player so proxy file routes are avoided."""
    if not path:
        return ""
    video_path = Path(path)
    if not video_path.is_file() or video_path.stat().st_size == 0:
        return f'<div class="video-error">{label}暂不可用</div>'
    encoded = base64.b64encode(video_path.read_bytes()).decode("ascii")
    return (
        f'<div class="embedded-video"><div class="video-label">{label}</div>'
        f'<video controls preload="metadata" playsinline style="width:100%;max-height:720px;">'
        f'<source src="data:video/mp4;base64,{encoded}" type="video/mp4">'
        f'</video></div>'
    )


def embedded_image(path: str | Path | None, label: str) -> str:
    """Return a self-contained image preview without a proxy file request."""
    if not path:
        return ""
    image_path = Path(path)
    if not image_path.is_file() or image_path.stat().st_size == 0:
        return f'<div class="image-error">{label}暂不可用</div>'
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    suffix = image_path.suffix.lower()
    mime = "image/jpeg" if suffix in {".jpg", ".jpeg"} else "image/png"
    return (
        f'<div class="embedded-image"><div class="image-label">{label}</div>'
        f'<img src="data:{mime};base64,{encoded}" alt="{label}" '
        f'style="max-width:100%;max-height:520px;object-fit:contain;">'
        f'</div>'
    )


def serve_legacy_file(path: str):
    """Keep old Gradio /file= URLs playable after upgrading Gradio."""
    candidate = Path(path).expanduser().resolve()
    allowed_roots = [Path('/tmp').resolve(), RUN_ROOT.resolve(), (ROOT / 'MusePose' / 'output').resolve()]
    if not candidate.is_file() or not any(
        candidate == root or root in candidate.parents for root in allowed_roots
    ):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(candidate, media_type="video/mp4" if candidate.suffix.lower() == ".mp4" else None)


def stream_task(worker, args, output_count, progress=None):
    events = queue.Queue()
    latest = [None] * (output_count - 1)
    log_text = ""

    def execute():
        LIVE_LOG.emit = lambda chunk: events.put(("log", chunk))
        try:
            result = worker(*args, progress=lambda *a, **kw: events.put(("progress", (a, kw))))
            if hasattr(result, "__next__"):
                for value in result:
                    events.put(("result", value))
            else:
                events.put(("result", result))
        except Exception as exc:
            events.put(("error", exc))
        finally:
            LIVE_LOG.emit = None
            events.put(("done", None))

    thread = threading.Thread(target=execute, daemon=True)
    thread.start()
    error = None
    last_update = 0.0
    dirty = False
    while True:
        try:
            kind, value = events.get(timeout=0.25)
        except queue.Empty:
            if dirty:
                yield (*latest, log_text[-50000:])
                last_update = time.monotonic()
                dirty = False
            continue
        if kind == "done":
            break
        if kind == "progress":
            if progress is not None:
                progress(*value[0], **value[1])
            # Send a visible event even when Gradio does not render the
            # global progress overlay for a generator callback.
            log_text += f"[阶段] {value[1].get('desc', '')} {value[0][0] if value[0] else ''}\n"
            dirty = True
            continue
        if kind == "error":
            error = value
            log_text += f"\n任务失败: {value}\n"
        elif kind == "result":
            latest = list(value[:-1])
            summary = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", str(value[-1])).replace("\r", "\n")
            summary = "\n".join(line for line in summary.splitlines()
                                if not re.search(r"\d+%\|.*\|.*\d+/\d+", line))
            log_text += "\n" + summary + "\n"
        else:
            log_text += value
        dirty = True
        now = time.monotonic()
        if kind != "log" or now - last_update >= 0.25:
            yield (*latest, log_text[-50000:])
            last_update = now
            dirty = False
    thread.join()
    yield (*latest, log_text[-50000:])
    if error is not None:
        raise error


MUSEPOSE_DIR = ROOT / "MusePose"
DORMANT_DIR = ROOT / "Dormant"
RUN_ROOT = ROOT / "web_frontend_runs"
GRADIO_TEMP_ROOT = RUN_ROOT / "gradio"
MUSEPOSE_PYTHON = Path(os.environ.get("MUSEPOSE_PYTHON", sys.executable))
DORMANT_PYTHON = Path(os.environ.get("DORMANT_PYTHON", str(MUSEPOSE_PYTHON)))

CSS = """
.gradio-container { max-width: 1280px !important; color: #16202a; }
.app-head { padding: 18px 0 14px; border-bottom: 1px solid #dbe3ec; margin-bottom: 16px; }
.app-head h1 { font-size: 30px; margin: 0 0 6px; }
.app-head p { color: #64748b; margin: 0; }
.compact textarea { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
"""


def timestamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def new_task_dir(prefix: str) -> Path:
    path = RUN_ROOT / f"{prefix}_{timestamp()}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_image(image: np.ndarray, task_dir: Path) -> Path:
    if image is None:
        raise gr.Error("请先上传人物图片。")
    path = task_dir / "source.png"
    Image.fromarray(image.astype(np.uint8)).convert("RGB").save(path)
    return path


def dormant_missing_weights() -> list[str]:
    required = [
        ("DWPose/yolox_l.onnx", [DORMANT_DIR / "pretrained_weights/DWPose/yolox_l.onnx"]),
        ("DWPose/dw-ll_ucoco_384.onnx", [DORMANT_DIR / "pretrained_weights/DWPose/dw-ll_ucoco_384.onnx"]),
        ("AnimateAnyone/denoising_unet.pth", [DORMANT_DIR / "pretrained_weights/AnimateAnyone/denoising_unet.pth"]),
        ("AnimateAnyone/reference_unet.pth", [DORMANT_DIR / "pretrained_weights/AnimateAnyone/reference_unet.pth"]),
        ("AnimateAnyone/pose_guider.pth", [DORMANT_DIR / "pretrained_weights/AnimateAnyone/pose_guider.pth"]),
        ("AnimateAnyone/motion_module.pth", [DORMANT_DIR / "pretrained_weights/AnimateAnyone/motion_module.pth"]),
        ("MagicAnimate/appearance_encoder/config.json", [DORMANT_DIR / "pretrained_weights/MagicAnimate/appearance_encoder/config.json"]),
        ("MagicAnimate/appearance_encoder weights", [
            DORMANT_DIR / "pretrained_weights/MagicAnimate/appearance_encoder/diffusion_pytorch_model.safetensors",
            DORMANT_DIR / "pretrained_weights/MagicAnimate/appearance_encoder/diffusion_pytorch_model.bin",
        ]),
        ("MagicPose/model_state-110000.th", [DORMANT_DIR / "pretrained_weights/MagicPose/model_state-110000.th"]),
        ("LPIPS/VGG16 weights", [ROOT / "torch_cache/hub/checkpoints/vgg16-397923af.pth"]),
        ("stable-diffusion-v1-5/unet/config.json", [DORMANT_DIR / "pretrained_weights/stable-diffusion-v1-5/unet/config.json"]),
        ("stable-diffusion-v1-5/unet weights", [
            DORMANT_DIR / "pretrained_weights/stable-diffusion-v1-5/unet/diffusion_pytorch_model.bin",
            DORMANT_DIR / "pretrained_weights/stable-diffusion-v1-5/unet/diffusion_pytorch_model.safetensors",
        ]),
        ("stable-diffusion-v1-5/text_encoder/config.json", [DORMANT_DIR / "pretrained_weights/stable-diffusion-v1-5/text_encoder/config.json"]),
        ("stable-diffusion-v1-5/text_encoder weights", [
            DORMANT_DIR / "pretrained_weights/stable-diffusion-v1-5/text_encoder/pytorch_model.bin",
            DORMANT_DIR / "pretrained_weights/stable-diffusion-v1-5/text_encoder/model.safetensors",
        ]),
        ("stable-diffusion-v1-5/tokenizer/vocab.json", [DORMANT_DIR / "pretrained_weights/stable-diffusion-v1-5/tokenizer/vocab.json"]),
        ("stable-diffusion-v1-5/tokenizer/merges.txt", [DORMANT_DIR / "pretrained_weights/stable-diffusion-v1-5/tokenizer/merges.txt"]),
        ("sd-vae-ft-mse/config.json", [DORMANT_DIR / "pretrained_weights/sd-vae-ft-mse/config.json"]),
        ("sd-vae-ft-mse weights", [
            DORMANT_DIR / "pretrained_weights/sd-vae-ft-mse/diffusion_pytorch_model.bin",
            DORMANT_DIR / "pretrained_weights/sd-vae-ft-mse/diffusion_pytorch_model.safetensors",
        ]),
        ("sd-image-variations-diffusers/image_encoder/config.json", [DORMANT_DIR / "pretrained_weights/sd-image-variations-diffusers/image_encoder/config.json"]),
        ("sd-image-variations-diffusers/image_encoder weights", [
            DORMANT_DIR / "pretrained_weights/sd-image-variations-diffusers/image_encoder/pytorch_model.bin",
            DORMANT_DIR / "pretrained_weights/sd-image-variations-diffusers/image_encoder/model.safetensors",
        ]),
    ]
    return [label for label, paths in required if not any(path.is_file() and path.stat().st_size > 0 for path in paths)]


def musepose_missing_weights() -> list[str]:
    """Return required MusePose inference files before doing GPU work."""
    base_model = DORMANT_DIR / "pretrained_weights/sd-image-variations-diffusers"
    vae = DORMANT_DIR / "pretrained_weights/sd-vae-ft-mse"
    image_encoder = base_model / "image_encoder"
    required = [
        ("model_index.json", [base_model / "model_index.json"]),
        ("unet/config.json", [base_model / "unet/config.json"]),
        (
            "unet model weights",
            [
                base_model / "unet/diffusion_pytorch_model.bin",
                base_model / "unet/diffusion_pytorch_model.safetensors",
            ],
        ),
        ("VAE config", [vae / "config.json"]),
        (
            "VAE model weights",
            [
                vae / "diffusion_pytorch_model.bin",
                vae / "diffusion_pytorch_model.safetensors",
            ],
        ),
        ("image encoder config", [image_encoder / "config.json"]),
        (
            "image encoder weights",
            [
                image_encoder / "pytorch_model.bin",
                image_encoder / "model.safetensors",
            ],
        ),
        ("denoising_unet.pth", [MUSEPOSE_DIR / "pretrained_weights/MusePose/denoising_unet.pth"]),
        ("reference_unet.pth", [MUSEPOSE_DIR / "pretrained_weights/MusePose/reference_unet.pth"]),
        ("pose_guider.pth", [MUSEPOSE_DIR / "pretrained_weights/MusePose/pose_guider.pth"]),
        ("motion_module.pth", [MUSEPOSE_DIR / "pretrained_weights/MusePose/motion_module.pth"]),
        ("inference_v2.yaml", [MUSEPOSE_DIR / "configs/inference_v2.yaml"]),
    ]
    return [label for label, paths in required if not any(path.exists() for path in paths)]


def run_dormant(
    source_path: Path,
    task_dir: Path,
    width: int,
    height: int,
    length: int,
    steps: int,
    eps: int,
    pgd_iters: int,
) -> tuple[Path, str]:
    if not DORMANT_DIR.is_dir():
        raise gr.Error(f"照片保护模块不存在: {DORMANT_DIR}")
    missing = dormant_missing_weights()
    if missing:
        raise gr.Error(
            "已勾选照片保护，但必要权重不完整，已停止流程，没有跳过保护。\n\n缺失文件:\n"
            + "\n".join(missing)
        )

    output_dir = task_dir / "dormant"
    output_dir.mkdir(parents=True, exist_ok=True)
    log = run_command(
        [
            DORMANT_PYTHON,
            "-u",
            "protect_image.py",
            "--config",
            "configs/protect.yaml",
            "--ref_image_path",
            source_path,
            "--W",
            int(width),
            "--H",
            int(height),
            "--L",
            int(length),
            "--steps",
            max(11, int(steps)),
            "--eps",
            int(eps),
            "--pgd_iters",
            int(pgd_iters),
            "--output_dir",
            output_dir,
            "--gpu_id",
            0,
        ],
        DORMANT_DIR,
        {"PYTHONPATH": str(DORMANT_DIR), "TORCH_HOME": str(ROOT / "torch_cache")},
    )
    protected_candidates = sorted(
        output_dir.glob("*_eps_*_iters_*.png"),
        key=lambda path: path.stat().st_mtime,
    )
    if not protected_candidates:
        raise gr.Error(f"照片保护未生成保护图片。\n\n{log[-5000:]}")

    protected_path = task_dir / "dormant_protected.png"
    original = Image.open(source_path).convert("RGB")
    protected = Image.open(protected_candidates[-1]).convert("RGB")
    if protected.size != original.size:
        protected = protected.resize(original.size, Image.Resampling.LANCZOS)
    protected.save(protected_path)
    return protected_path, log


def save_upload(uploaded, task_dir: Path, name: str) -> Path:
    if uploaded is None:
        raise gr.Error("请先上传视频。")
    src = Path(uploaded if isinstance(uploaded, (str, os.PathLike)) else uploaded.name)
    if not src.exists():
        raise gr.Error(f"上传文件不存在: {src}")
    dst = task_dir / f"{name}{src.suffix or '.mp4'}"
    shutil.copy2(src, dst)
    return dst


def run_command(cmd: list[object], cwd: Path, env: dict[str, str] | None = None) -> str:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    merged["PYTHONUNBUFFERED"] = "1"
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        # The launcher environment may contain an invalid value such as an
        # empty string.  Keep subprocess diagnostics out of machine-readable
        # ffprobe output and avoid libgomp startup warnings.
        merged[key] = "1"
    emit = getattr(LIVE_LOG, "emit", None)
    if emit:
        emit(f"\n$ {' '.join(map(str, cmd))}\n")
    proc = subprocess.Popen(
        [str(part) for part in cmd],
        cwd=str(cwd),
        env=merged,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    import codecs
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    chunks = []
    pending = ""
    try:
        while True:
            chunk = os.read(proc.stdout.fileno(), 4096)
            if not chunk:
                break
            decoded = decoder.decode(chunk)
            chunks.append(decoded)
            pending += decoded.replace("\r", "\n")
            lines = pending.split("\n")
            pending = lines.pop()
            for line in lines:
                line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line)
                if emit and line.strip() and not re.search(r"\d+%\|.*\|.*\d+/\d+", line):
                    emit(line + "\n")
        tail = decoder.decode(b"", final=True)
        chunks.append(tail)
        if emit and (pending + tail).strip():
            emit(pending + tail + "\n")
        proc.wait()
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.terminate()
            proc.wait()
    output = "".join(chunks)
    log_path = getattr(LIVE_LOG, "path", None)
    if log_path:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(f"\n$ {' '.join(map(str, cmd))}\n{output}\n")
    if proc.returncode:
        raise gr.Error(f"命令失败: {' '.join(map(str, cmd))}\n\n{output[-6000:]}")
    return output


def transcode_h264(input_path: Path, output_path: Path) -> tuple[Path, str]:
    log = run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-map",
            "0:v:0",
            "-vf",
            "pad=ceil(iw/2)*2:ceil(ih/2)*2:0:0:black",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-preset",
            "medium",
            "-crf",
            "18",
            output_path,
        ],
        ROOT,
    )
    return output_path, log


def transcode_browser_video(input_path: Path, task_dir: Path, name: str) -> tuple[Path, str]:
    """Make intermediate videos playable in browsers and Gradio."""
    output_path = task_dir / f"{name}_h264.mp4"
    return transcode_h264(input_path, output_path)


def comparison_with_driving(input_path: Path, driving_path: Path, output_path: Path) -> tuple[Path, str]:
    probe = json.loads(run_command(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height:format=duration", "-of", "json", input_path], ROOT))
    width = int(probe["streams"][0]["width"])
    height = int(probe["streams"][0]["height"])
    # torchvision.make_grid adds two pixels around and between the three panels.
    panel_width = (width - 8) // 3
    inset_width = max(2, int(panel_width * 0.65) // 2 * 2)
    inset_height = max(2, int((height - 4) * 0.45) // 2 * 2)
    filters = (
        "[0:v]setpts=PTS-STARTPTS[grid];"
        f"[1:v]setpts=PTS-STARTPTS,scale={inset_width}:{inset_height}:"
        "force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[inset];"
        f"[grid][inset]overlay=x={panel_width + 4}:y=2:"
        "eof_action=repeat:shortest=0[out]"
    )
    log = run_command(
        ["ffmpeg", "-y", "-i", input_path, "-i", driving_path,
         "-filter_complex", filters, "-map", "[out]", "-an", "-t", probe["format"]["duration"],
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
         "-preset", "medium", "-crf", "18", output_path], ROOT)
    return output_path, log


def latest_musepose_output(before: set[Path]) -> Path:
    # Generation filenames end in the numeric skip value; comparison grids
    # append pose-guider and motion-module names instead.
    candidates = [
        path
        for path in (MUSEPOSE_DIR / "output").glob("video-*/*/*.mp4")
        if path.resolve() not in before and path.stem.rsplit("_", 1)[-1].isdigit()
    ]
    if not candidates:
        raise gr.Error("MusePose 本次任务没有生成独立人物视频输出。")
    return sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)[0]


def extract_pose(
    video_path: Path,
    reference_path: Path,
    task_dir: Path,
    control_parts: str,
) -> tuple[Path, Path, str]:
    pose_path = task_dir / "musepose_pose.mp4"
    aligned_path = task_dir / "aligned_driving.mp4"
    log = run_command(
        [
            MUSEPOSE_PYTHON,
            "-u",
            MUSEPOSE_DIR / "run_uploaded_musepose.py",
            "--reference",
            reference_path,
            "--driving",
            video_path,
            "--pose_out",
            pose_path,
            "--aligned_driving_out",
            aligned_path,
            "--control_parts",
            control_parts,
        ],
        MUSEPOSE_DIR,
        {"PYTHONPATH": str(MUSEPOSE_DIR)},
    )
    with Image.open(reference_path) as reference_image:
        reference_size = reference_image.convert("RGB").size
    probe = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            pose_path,
        ],
        ROOT,
    ).strip()
    size_line = next(
        (line.strip() for line in probe.splitlines() if "x" in line and line.strip()),
        "",
    )
    try:
        actual_size = tuple(int(value) for value in size_line.split("x", 1))
    except (TypeError, ValueError) as exc:
        raise gr.Error(f"无法读取姿态视频尺寸，ffprobe 输出为：{probe[-500:]}") from exc
    if len(actual_size) != 2:
        raise gr.Error(f"无法读取姿态视频尺寸，ffprobe 输出为：{probe[-500:]}")
    if actual_size != reference_size:
        raise gr.Error(
            f"姿态视频尺寸校验失败：期望 {reference_size[0]}x{reference_size[1]}，"
            f"实际 {actual_size[0]}x{actual_size[1]}。"
        )
    log += f"\n驱动视频已按参考图人脸等比例缩放、平移对齐，空白补黑：{aligned_path}。"
    log += f"\n姿态视频已在对齐后的驱动视频上提取，输出 {reference_size[0]}x{reference_size[1]}。"
    return pose_path, aligned_path, log


def write_musepose_config(source_path: Path, pose_video: Path, task_dir: Path) -> Path:
    config = task_dir / "musepose.yaml"
    # test_stage_2.py runs with MusePose as cwd, but the frontend may be
    # launched from elsewhere. Absolute paths keep model/config resolution
    # independent of the process working directory.
    musepose_weights = MUSEPOSE_DIR / "pretrained_weights"
    dormant_weights = DORMANT_DIR / "pretrained_weights"
    config.write_text(
        f"""pretrained_base_model_path: '{dormant_weights / 'sd-image-variations-diffusers'}'
pretrained_vae_path: '{dormant_weights / 'sd-vae-ft-mse'}'
image_encoder_path: '{dormant_weights / 'sd-image-variations-diffusers/image_encoder'}'
denoising_unet_path: '{musepose_weights / 'MusePose/denoising_unet.pth'}'
reference_unet_path: '{musepose_weights / 'MusePose/reference_unet.pth'}'
pose_guider_path: '{musepose_weights / 'MusePose/pose_guider.pth'}'
motion_module_path: '{musepose_weights / 'MusePose/motion_module.pth'}'
inference_config: '{MUSEPOSE_DIR / 'configs/inference_v2.yaml'}'
weight_dtype: 'fp16'
test_cases:
  "{source_path}":
    - "{pose_video}"
""",
        encoding="utf-8",
    )
    return config


def run_musepose_worker(
    source_image,
    driving_video,
    auto_pose,
    use_dormant,
    dormant_eps,
    dormant_iters,
    control_parts,
    steps,
    cfg,
    frames,
    width,
    height,
    progress=no_progress,
):
    task_dir = new_task_dir("musepose")
    LIVE_LOG.path = task_dir / "runtime.log"
    missing_musepose = musepose_missing_weights()
    if missing_musepose:
        raise gr.Error(
            "MusePose 推理所需模型不完整，已在开始前停止。缺失文件:\n\n"
            + "\n".join(missing_musepose)
        )
    source_path = save_image(source_image, task_dir)
    video_path = save_upload(driving_video, task_dir, "driving")
    progress(0.10, desc="准备输入")
    dormant_log = "未启用照片保护。"
    protected_preview = None
    yield None, None, None, None, None, "准备输入。"
    if use_dormant:
        progress(0.18, desc="照片保护")
        source_path, dormant_log = run_dormant(
            source_path,
            task_dir,
            int(width),
            int(height),
            min(5, int(frames)),
            int(steps),
            int(dormant_eps),
            int(dormant_iters),
        )
        protected_preview = str(source_path)
        yield None, None, None, embedded_image(protected_preview, "保护后的图片"), None, f"照片保护完成: {source_path}\n{dormant_log[-2200:]}"
    if auto_pose:
        progress(0.30, desc="姿态提取")
        pose_video, aligned_video, pose_log = extract_pose(video_path, source_path, task_dir, control_parts)
    else:
        pose_video, aligned_video, pose_log = video_path, video_path, "使用上传视频作为姿态控制视频。"
    progress(0.36, desc="准备浏览器视频")
    aligned_video, aligned_transcode_log = transcode_browser_video(
        aligned_video, task_dir, "aligned_driving"
    )
    pose_video, pose_transcode_log = transcode_browser_video(
        pose_video, task_dir, "pose"
    )
    pose_log += f"\n--- 视频兼容性转码 ---\n{aligned_transcode_log[-1200:]}\n{pose_transcode_log[-1200:]}\n"
    config = write_musepose_config(source_path, pose_video, task_dir)
    progress(0.45, desc="运行姿态生成")
    before = {path.resolve() for path in (MUSEPOSE_DIR / "output").glob("video-*/*/*.mp4")}
    infer_log = run_command(
        [
            MUSEPOSE_PYTHON,
            "-u",
            "test_stage_2.py",
            "--config",
            config,
            "-W",
            int(width),
            "-H",
            int(height),
            "-L",
            int(frames),
            "-S",
            min(48, int(frames)),
            "-O",
            4,
            "--cfg",
            float(cfg),
            "--seed",
            42,
            "--steps",
            int(steps),
            "--skip",
            0,
        ],
        MUSEPOSE_DIR,
        {"PYTHONPATH": str(MUSEPOSE_DIR)},
    )
    raw = latest_musepose_output(before)
    result, transcode_log = transcode_h264(raw, task_dir / "musepose_output_h264.mp4")
    comparison = None
    comparison_candidates = [
        path for path in raw.parent.glob(f"{raw.stem}_*.mp4")
        if path.resolve() not in before
    ]
    if comparison_candidates:
        comparison, comparison_log = comparison_with_driving(
            max(comparison_candidates, key=lambda path: path.stat().st_mtime),
            video_path,
            task_dir / "musepose_comparison_h264.mp4",
        )
        transcode_log += "\n" + comparison_log
    yield (
        embedded_video(result, "姿态驱动生成视频"),
        embedded_video(aligned_video, "YOLO 人物对齐视频"),
        embedded_video(pose_video, "提取的姿态视频"),
        embedded_image(protected_preview, "保护后的图片"),
        embedded_video(comparison, "三连对比：参考图 / 姿态 / 生成结果") if comparison else "",
        f"姿态驱动生成视频: {result}\nYOLO 人物对齐视频: {aligned_video}\n动作姿态视频: {pose_video}\n",
    )
    progress(1.0, desc="完成")
    log = (
        f"YOLO 人物对齐视频: {aligned_video}\n动作姿态视频: {pose_video}\n原始生成输出: {raw}\n姿态驱动生成视频: {result}\n"
        f"保护参考图: {source_path}\n"
        f"\n--- 照片保护日志 ---\n{dormant_log[-2200:]}\n"
        f"\n--- 姿态日志 ---\n{pose_log[-1800:]}\n"
        f"\n--- 姿态驱动推理日志 ---\n{infer_log[-3000:]}\n"
        f"\n--- 转码日志 ---\n{transcode_log[-800:]}\n"
    )
    yield (
        embedded_video(result, "姿态驱动生成视频"),
        embedded_video(aligned_video, "YOLO 人物对齐视频"),
        embedded_video(pose_video, "提取的姿态视频"),
        embedded_image(protected_preview, "保护后的图片"),
        embedded_video(comparison, "三连对比：参考图 / 姿态 / 生成结果") if comparison else "",
        log,
    )


def run_facefusion_only(video, model, blend, weight, provider, progress=gr.Progress()):
    task_dir = new_task_dir("facefusion")
    video_path = save_upload(video, task_dir, "input")
    progress(0.2, desc="FaceFusion 人脸修复")
    output, log = run_facefusion(video_path, task_dir, model, blend, weight, provider)
    progress(1.0, desc="完成")
    return str(output), f"输入: {video_path}\n输出: {output}\n\n{log[-6000:]}"


def run_dormant_only_worker(
    image,
    width,
    height,
    steps,
    eps,
    pgd_iters,
    progress=no_progress,
):
    """Run Dormant as an independent photo-protection task."""
    task_dir = new_task_dir("dormant")
    LIVE_LOG.path = task_dir / "runtime.log"
    source_path = save_image(image, task_dir)
    original_size = Image.open(source_path).size
    progress(0.08, desc="准备照片")
    progress(0.15, desc="检查照片保护资源")
    protected_path, log = run_dormant(
        source_path,
        task_dir,
        int(width),
        int(height),
        1,
        int(steps),
        int(eps),
        int(pgd_iters),
    )
    # run_dormant already preserves the input size; enforce it again here so
    # the standalone page keeps the same contract after future changes.
    protected = Image.open(protected_path).convert("RGB")
    if protected.size != original_size:
        protected = protected.resize(original_size, Image.Resampling.LANCZOS)
        protected.save(protected_path)
    progress(1.0, desc="照片保护完成")
    return embedded_image(protected_path, "保护后的照片"), (
        f"输入图片: {source_path}\n"
        f"原图尺寸: {original_size[0]}x{original_size[1]}\n"
        f"保护图片: {protected_path}\n"
        f"输出尺寸: {protected.size[0]}x{protected.size[1]}\n\n"
        f"--- 照片保护日志 ---\n{log[-7000:]}"
    )


def run_musepose(source_image, driving_video, auto_pose, use_dormant, dormant_eps,
                 dormant_iters, control_parts, steps, cfg, frames, width, height,
                 progress=gr.Progress()):
    yield from stream_task(run_musepose_worker, (source_image, driving_video, auto_pose,
                           use_dormant, dormant_eps, dormant_iters, control_parts,
                           steps, cfg, frames, width, height), 6, progress)


def run_dormant_only(image, width, height, steps, eps, pgd_iters, progress=gr.Progress()):
    yield from stream_task(run_dormant_only_worker, (image, width, height, steps, eps, pgd_iters), 2, progress)


def build_ui():
    with gr.Blocks(css=CSS, js=VIDEO_PROXY_JS, head='<meta name="video-proxy-version" content="20261020-4">', title="面向姿态驱动人物视频生成的照片主动防护平台") as demo:
        gr.HTML(
            """
            <div class="app-head">
              <h1>面向姿态驱动人物视频生成的照片主动防护平台</h1>
              <p>支持照片保护、姿态驱动和生成结果对比；勾选照片保护后，保护必须成功才会继续。</p>
            </div>
            """
        )
        with gr.Tabs():
            with gr.Tab("姿态驱动"):
                with gr.Row():
                    source = gr.Image(label="人物图片", type="numpy", sources=["upload", "clipboard"], height=320)
                    driving = gr.File(label="驱动视频", file_types=[".mp4", ".mov", ".webm"], height=320)
                with gr.Row():
                    auto_pose = gr.Checkbox(value=True, label="自动提取 DWPose")
                    use_dormant = gr.Checkbox(value=False, label="先进行照片保护")
                    dormant_eps = gr.Slider(1, 32, value=16, step=1, label="保护强度")
                    dormant_iters = gr.Slider(1, 200, value=100, step=1, label="保护迭代步数")
                    parts = gr.Dropdown(["body", "body_hand", "full"], value="body_hand", label="姿态控制")
                    steps = gr.Slider(10, 50, value=20, step=1, label="采样步数")
                    cfg = gr.Slider(1.0, 10.0, value=3.5, step=0.5, label="CFG")
                    frames = gr.Slider(16, 300, value=48, step=8, label="帧数")
                with gr.Row():
                    width = gr.Slider(384, 768, value=768, step=64, label="宽")
                    height = gr.Slider(384, 1024, value=768, step=8, label="高")
                run_btn = gr.Button("运行姿态驱动", variant="primary")
                protected_result = gr.HTML(label="保护后的图片")
                result = gr.HTML(label="姿态驱动生成视频")
                aligned_result = gr.HTML(label="YOLO 人物对齐视频")
                pose_result = gr.HTML(label="提取的姿态视频")
                comparison_result = gr.HTML(label="三连对比：参考图 / 姿态 / 生成结果")
                log = gr.Textbox(label="运行日志", lines=16, elem_classes=["compact"])
                run_btn.click(
                    run_musepose,
                    [
                        source,
                        driving,
                        auto_pose,
                        use_dormant,
                        dormant_eps,
                        dormant_iters,
                        parts,
                        steps,
                        cfg,
                        frames,
                        width,
                        height,
                    ],
                    [result, aligned_result, pose_result, protected_result, comparison_result, log],
                    show_progress="full",
                    show_progress_on=[result, aligned_result, pose_result, protected_result, comparison_result],
                )
            with gr.Tab("照片保护"):
                dormant_image = gr.Image(
                    label="待保护照片",
                    type="numpy",
                    sources=["upload", "clipboard"],
                    height=360,
                )
                with gr.Row():
                    dormant_width = gr.Slider(384, 1024, value=768, step=64, label="内部处理宽度")
                    dormant_height = gr.Slider(384, 1024, value=768, step=8, label="内部处理高度")
                    dormant_steps = gr.Slider(10, 50, value=20, step=1, label="采样步数")
                with gr.Row():
                    dormant_only_eps = gr.Slider(1, 32, value=16, step=1, label="eps")
                    dormant_only_iters = gr.Slider(1, 200, value=100, step=1, label="PGD 步数")
                dormant_only_btn = gr.Button("运行照片保护", variant="primary")
                dormant_only_out = gr.HTML(label="保护后的照片")
                dormant_only_log = gr.Textbox(label="运行日志", lines=16, elem_classes=["compact"])
                dormant_only_btn.click(
                    run_dormant_only,
                    [
                        dormant_image,
                        dormant_width,
                        dormant_height,
                        dormant_steps,
                        dormant_only_eps,
                        dormant_only_iters,
                    ],
                    [dormant_only_out, dormant_only_log],
                    show_progress="full",
                    show_progress_on=[dormant_only_out],
                )
    return demo


if __name__ == "__main__":
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    GRADIO_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    app = build_ui().queue(default_concurrency_limit=1)
    app.launch(
        server_name="0.0.0.0",
        server_port=int(os.environ.get("WEBAPP_PORT", "6006")),
        allowed_paths=["/tmp", str(RUN_ROOT), str(MUSEPOSE_DIR / "output")],
        pwa=True,
        share=False,
        show_error=True,
        prevent_thread_lock=True,
    )
    app.app.add_api_route(
        "/file={path:path}",
        serve_legacy_file,
        methods=["GET", "HEAD"],
        name="legacy_file",
    )
    # Gradio also registers a legacy /file= route. Put ours first so older
    # cached video URLs are served through the current file response handler.
    for index, route in enumerate(app.app.router.routes):
        if getattr(route, "path", "") == "/file={path:path}" and getattr(route, "name", "") == "legacy_file":
            app.app.router.routes.insert(0, app.app.router.routes.pop(index))
            break
    while True:
        time.sleep(3600)
