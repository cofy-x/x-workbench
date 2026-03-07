from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import random
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Sequence, Tuple
from urllib.parse import unquote, urlparse

from PIL import Image, ImageDraw, ImageFont


# ====================
# Default settings
# ====================
DEFAULT_BRAND_NAME = "Avant"
DEFAULT_FONT_PATH: str | None = None
DEFAULT_VARIANT = "v3_forward"
DEFAULT_GENERATE_ALL_VARIANTS = False
DEFAULT_ICON_MODE = "brand_seeded"

ICON_MODES = ("static_variant", "brand_initial", "brand_seeded")


def discover_workspace_root() -> Path:
    env_root = os.getenv("TOOLS_WORKSPACE_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    script_dir = Path(__file__).resolve().parent
    for candidate in [script_dir, *script_dir.parents]:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return Path.cwd().resolve()


WORKSPACE_ROOT = discover_workspace_root()
OUTPUT_ROOT = WORKSPACE_ROOT / "generated"
WEB_DIR = Path(__file__).with_name("web")
INDEX_FILE = WEB_DIR / "index.html"


@dataclass(frozen=True)
class VariantSpec:
    blocks: Sequence[Tuple[int, int]]
    grid_size: Tuple[int, int] = (5, 5)
    unit: int = 40
    icon_canvas_scale: float = 1.55
    text_scale: float = 7.2
    spacing_scale: float = 1.35
    logo_height_scale: float = 1.30
    text_nudge_scale: float = 0.10


@dataclass(frozen=True)
class GenerationRequest:
    brand_name: str = DEFAULT_BRAND_NAME
    custom_font_path: str | None = DEFAULT_FONT_PATH
    default_variant: str = DEFAULT_VARIANT
    generate_all_variants: bool = DEFAULT_GENERATE_ALL_VARIANTS
    icon_mode: str = DEFAULT_ICON_MODE
    random_seed: str | None = None


VARIANTS: Dict[str, VariantSpec] = {
    "v1_open": VariantSpec(
        blocks=[
            (1, 0),
            (2, 0),
            (3, 0),
            (0, 1),
            (4, 1),
            (0, 2),
            (4, 2),
            (1, 2),
            (3, 2),
        ]
    ),
    "v2_classic": VariantSpec(
        blocks=[
            (2, 0),
            (1, 1),
            (3, 1),
            (0, 2),
            (1, 2),
            (2, 2),
            (3, 2),
            (4, 2),
            (0, 3),
            (4, 3),
            (0, 4),
            (4, 4),
        ],
        text_scale=7.0,
    ),
    "v3_forward": VariantSpec(
        blocks=[
            (1, 0),
            (2, 0),
            (3, 0),
            (0, 1),
            (4, 1),
            (0, 2),
            (1, 2),
            (2, 2),
            (3, 2),
            (4, 2),
            (1, 3),
            (3, 3),
            (1, 4),
            (3, 4),
        ],
        spacing_scale=1.25,
    ),
    "v4_minimal": VariantSpec(
        blocks=[
            (1, 0),
            (2, 0),
            (3, 0),
            (0, 1),
            (4, 1),
            (0, 2),
            (2, 2),
            (4, 2),
            (0, 3),
            (4, 3),
            (0, 4),
            (4, 4),
        ],
        text_scale=7.1,
    ),
}


def color_for_mode(mode: str) -> Tuple[Tuple[int, int, int, int], Tuple[int, int, int, int]]:
    if mode == "light":
        return (255, 255, 255, 0), (0, 0, 0, 255)
    if mode == "dark":
        return (0, 0, 0, 0), (255, 255, 255, 255)
    raise ValueError("mode must be 'light' or 'dark'")


def slugify(value: str) -> str:
    chars: List[str] = []
    last_hyphen = False
    for ch in value.lower():
        if ch.isalnum():
            chars.append(ch)
            last_hyphen = False
        elif not last_hyphen:
            chars.append("-")
            last_hyphen = True
    slug = "".join(chars).strip("-")
    return slug or "brand"


def load_font(size: int, custom_font_path: str | None) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        custom_font_path,
        os.getenv("LOGO_FONT_PATH"),
        os.getenv("AVANT_FONT_PATH"),
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "Arial Bold.ttf",
        "Arial.ttf",
    ]
    for path in candidates:
        if not path:
            continue
        try:
            if os.path.isabs(path) and not os.path.exists(path):
                continue
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def measure_text(text: str, font: ImageFont.ImageFont) -> Tuple[int, int]:
    measure_draw = ImageDraw.Draw(Image.new("RGBA", (1, 1), (0, 0, 0, 0)))
    bbox = measure_draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def first_alnum_upper(value: str) -> str:
    for ch in value:
        if ch.isalnum():
            return ch.upper()
    return "A"


LETTER_PATTERNS_5X5: Dict[str, Sequence[str]] = {
    "A": (".###.", "#...#", "#####", "#...#", "#...#"),
    "B": ("####.", "#...#", "####.", "#...#", "####."),
    "C": (".####", "#....", "#....", "#....", ".####"),
    "D": ("###..", "#..#.", "#...#", "#..#.", "###.."),
    "E": ("#####", "#....", "####.", "#....", "#####"),
    "F": ("#####", "#....", "####.", "#....", "#...."),
    "G": (".####", "#....", "#.###", "#...#", ".###."),
    "H": ("#...#", "#...#", "#####", "#...#", "#...#"),
    "I": ("#####", "..#..", "..#..", "..#..", "#####"),
    "J": ("..###", "...#.", "...#.", "#..#.", ".##.."),
    "K": ("#...#", "#..#.", "###..", "#..#.", "#...#"),
    "L": ("#....", "#....", "#....", "#....", "#####"),
    "M": ("#...#", "##.##", "#.#.#", "#...#", "#...#"),
    "N": ("#...#", "##..#", "#.#.#", "#..##", "#...#"),
    "O": (".###.", "#...#", "#...#", "#...#", ".###."),
    "P": ("####.", "#...#", "####.", "#....", "#...."),
    "Q": (".###.", "#...#", "#...#", "#..##", ".####"),
    "R": ("####.", "#...#", "####.", "#..#.", "#...#"),
    "S": (".####", "#....", ".###.", "....#", "####."),
    "T": ("#####", "..#..", "..#..", "..#..", "..#.."),
    "U": ("#...#", "#...#", "#...#", "#...#", ".###."),
    "V": ("#...#", "#...#", "#...#", ".#.#.", "..#.."),
    "W": ("#...#", "#...#", "#.#.#", "##.##", "#...#"),
    "X": ("#...#", ".#.#.", "..#..", ".#.#.", "#...#"),
    "Y": ("#...#", ".#.#.", "..#..", "..#..", "..#.."),
    "Z": ("#####", "...#.", "..#..", ".#...", "#####"),
    "0": (".###.", "#...#", "#...#", "#...#", ".###."),
    "1": ("..#..", ".##..", "..#..", "..#..", ".###."),
    "2": (".###.", "#...#", "...#.", "..#..", "#####"),
    "3": ("####.", "...#.", ".##..", "...#.", "####."),
    "4": ("#..#.", "#..#.", "#####", "...#.", "...#."),
    "5": ("#####", "#....", "####.", "....#", "####."),
    "6": (".###.", "#....", "####.", "#...#", ".###."),
    "7": ("#####", "...#.", "..#..", ".#...", ".#..."),
    "8": (".###.", "#...#", ".###.", "#...#", ".###."),
    "9": (".###.", "#...#", ".####", "....#", ".###."),
}


def parse_pattern_blocks(pattern: Sequence[str]) -> List[Tuple[int, int]]:
    blocks: List[Tuple[int, int]] = []
    for y, row in enumerate(pattern):
        for x, ch in enumerate(row):
            if ch == "#":
                blocks.append((x, y))
    return blocks


def render_letter_fallback(
    letter: str,
    grid_size: Tuple[int, int],
    custom_font_path: str | None,
) -> List[Tuple[int, int]]:
    grid_w, grid_h = grid_size
    canvas_size = 256
    font_size = 210
    font = load_font(font_size, custom_font_path)

    glyph = Image.new("L", (canvas_size, canvas_size), 0)
    draw = ImageDraw.Draw(glyph)
    bbox = draw.textbbox((0, 0), letter, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    text_x = (canvas_size - text_w) // 2 - bbox[0]
    text_y = (canvas_size - text_h) // 2 - bbox[1]
    draw.text((text_x, text_y), letter, fill=255, font=font)

    downsampled = glyph.resize((grid_w, grid_h), Image.Resampling.BOX)
    blocks: List[Tuple[int, int]] = []
    for by in range(grid_h):
        for bx in range(grid_w):
            if downsampled.getpixel((bx, by)) >= 56:
                blocks.append((bx, by))
    return blocks


def letter_to_blocks(
    letter: str,
    grid_size: Tuple[int, int],
    custom_font_path: str | None,
) -> List[Tuple[int, int]]:
    if grid_size == (5, 5) and letter in LETTER_PATTERNS_5X5:
        return parse_pattern_blocks(LETTER_PATTERNS_5X5[letter])
    return render_letter_fallback(letter, grid_size, custom_font_path)


def add_seeded_accents(
    blocks: Sequence[Tuple[int, int]],
    grid_size: Tuple[int, int],
    seed_value: str,
) -> List[Tuple[int, int]]:
    grid_w, grid_h = grid_size
    rng = random.Random(seed_value)
    out = set(blocks)

    border_cells = [
        (x, y)
        for y in range(grid_h)
        for x in range(grid_w)
        if (x in (0, grid_w - 1) or y in (0, grid_h - 1))
    ]
    candidates = [cell for cell in border_cells if cell not in out]
    if not candidates:
        return sorted(out, key=lambda item: (item[1], item[0]))

    accent_count = 1 + rng.randrange(2)
    rng.shuffle(candidates)
    for cell in candidates[:accent_count]:
        out.add(cell)

    return sorted(out, key=lambda item: (item[1], item[0]))


def resolve_icon_blocks(
    request: GenerationRequest,
    variant_name: str,
    variant: VariantSpec,
) -> List[Tuple[int, int]]:
    if request.icon_mode not in ICON_MODES:
        raise ValueError(f"icon_mode must be one of: {', '.join(ICON_MODES)}")

    if request.icon_mode == "static_variant":
        return list(variant.blocks)

    letter = first_alnum_upper(request.brand_name)
    dynamic_blocks = letter_to_blocks(letter, variant.grid_size, request.custom_font_path)
    if len(dynamic_blocks) < 5:
        dynamic_blocks = list(variant.blocks)

    if request.icon_mode == "brand_initial":
        return dynamic_blocks

    seed_base = request.random_seed or request.brand_name
    seed_hash = hashlib.sha256(f"{seed_base}|{variant_name}|{letter}".encode("utf-8")).hexdigest()
    seed_value = f"{seed_base}|{seed_hash}"
    return add_seeded_accents(dynamic_blocks, variant.grid_size, seed_value)


def create_logo(
    variant: VariantSpec,
    mode: str,
    is_icon: bool,
    brand_name: str,
    custom_font_path: str | None,
    icon_blocks: Sequence[Tuple[int, int]] | None = None,
) -> Image.Image:
    bg_color, fg_color = color_for_mode(mode)
    unit = variant.unit
    grid_w, grid_h = variant.grid_size
    icon_w = grid_w * unit
    icon_h = grid_h * unit

    font = None
    if not is_icon:
        font_size = int(unit * variant.text_scale)
        font = load_font(font_size, custom_font_path)
        tmp_draw = ImageDraw.Draw(Image.new("RGBA", (1, 1), (0, 0, 0, 0)))
        bbox = tmp_draw.textbbox((0, 0), brand_name, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        spacing = int(unit * variant.spacing_scale)
        side_padding = unit
        canvas_w = int(icon_w + spacing + text_w + side_padding * 2)
        canvas_h = int(max(icon_h, text_h) * variant.logo_height_scale + unit * 0.4)
        img = Image.new("RGBA", (canvas_w, canvas_h), bg_color)
        draw = ImageDraw.Draw(img)
        icon_x = side_padding
        icon_y = (canvas_h - icon_h) / 2
        text_x = icon_x + icon_w + spacing - bbox[0]
        text_y = (canvas_h - text_h) / 2 - bbox[1] - int(unit * variant.text_nudge_scale)
    else:
        canvas_w = int(icon_w * variant.icon_canvas_scale)
        canvas_h = int(icon_h * variant.icon_canvas_scale)
        img = Image.new("RGBA", (canvas_w, canvas_h), bg_color)
        draw = ImageDraw.Draw(img)
        icon_x = (canvas_w - icon_w) / 2
        icon_y = (canvas_h - icon_h) / 2

    blocks = icon_blocks if icon_blocks is not None else variant.blocks
    for bx, by in blocks:
        x0 = int(icon_x + bx * unit)
        y0 = int(icon_y + by * unit)
        draw.rectangle([x0, y0, x0 + unit - 1, y0 + unit - 1], fill=fg_color)

    if not is_icon and font is not None:
        draw.text((text_x, text_y), brand_name, fill=fg_color, font=font)

    return img


def save_four_files(
    prefix: Path,
    variant: VariantSpec,
    icon_blocks: Sequence[Tuple[int, int]],
    brand_name: str,
    custom_font_path: str | None,
) -> List[Path]:
    files: List[Path] = []
    suffix_modes = [
        ("icon", "light"),
        ("logo", "light"),
        ("icon-dark", "dark"),
        ("logo-dark", "dark"),
    ]
    for suffix, mode in suffix_modes:
        is_icon = suffix.startswith("icon")
        path = Path(f"{prefix}-{suffix}.png")
        create_logo(
            variant=variant,
            mode=mode,
            is_icon=is_icon,
            brand_name=brand_name,
            custom_font_path=custom_font_path,
            icon_blocks=icon_blocks,
        ).save(path)
        files.append(path)
    return files


def path_to_url(path: Path) -> str:
    relative = path.resolve().relative_to(WORKSPACE_ROOT)
    return "/" + relative.as_posix()


def generate_assets(request: GenerationRequest) -> dict:
    if request.default_variant not in VARIANTS:
        raise ValueError(f"Unknown variant: {request.default_variant}")
    if not request.brand_name.strip():
        raise ValueError("brand_name cannot be empty.")

    brand_name = request.brand_name.strip()
    brand_slug = slugify(brand_name)
    output_dir = OUTPUT_ROOT / brand_slug
    output_dir.mkdir(parents=True, exist_ok=True)

    generated_paths: List[Path] = []
    if request.generate_all_variants:
        for variant_name in VARIANTS:
            prefix = output_dir / f"{brand_slug}-{variant_name}"
            icon_blocks = resolve_icon_blocks(request, variant_name, VARIANTS[variant_name])
            generated_paths.extend(
                save_four_files(
                    prefix,
                    VARIANTS[variant_name],
                    icon_blocks,
                    brand_name,
                    request.custom_font_path,
                )
            )

    default_prefix = output_dir / brand_slug
    default_blocks = resolve_icon_blocks(request, request.default_variant, VARIANTS[request.default_variant])
    generated_paths.extend(
        save_four_files(
            default_prefix,
            VARIANTS[request.default_variant],
            default_blocks,
            brand_name,
            request.custom_font_path,
        )
    )

    file_items = [
        {
            "name": path.name,
            "relative_path": str(path.resolve().relative_to(WORKSPACE_ROOT)),
            "absolute_path": str(path.resolve()),
            "url": path_to_url(path),
        }
        for path in generated_paths
    ]
    return {
        "brand_name": brand_name,
        "brand_slug": brand_slug,
        "default_variant": request.default_variant,
        "generate_all_variants": request.generate_all_variants,
        "icon_mode": request.icon_mode,
        "random_seed": request.random_seed,
        "output_dir": str(output_dir.resolve()),
        "files": file_items,
    }


def parse_generation_payload(payload: dict) -> GenerationRequest:
    brand_name = str(payload.get("brand_name", DEFAULT_BRAND_NAME)).strip()
    default_variant = str(payload.get("default_variant", DEFAULT_VARIANT)).strip()
    generate_all_variants = bool(payload.get("generate_all_variants", DEFAULT_GENERATE_ALL_VARIANTS))
    icon_mode = str(payload.get("icon_mode", DEFAULT_ICON_MODE)).strip()
    custom_font_path_raw = payload.get("custom_font_path", DEFAULT_FONT_PATH)
    custom_font_path = str(custom_font_path_raw).strip() if custom_font_path_raw else None
    random_seed_raw = payload.get("random_seed")
    random_seed = str(random_seed_raw).strip() if random_seed_raw else None

    if default_variant not in VARIANTS:
        raise ValueError(f"default_variant must be one of: {', '.join(VARIANTS.keys())}")
    if icon_mode not in ICON_MODES:
        raise ValueError(f"icon_mode must be one of: {', '.join(ICON_MODES)}")
    if not brand_name:
        raise ValueError("brand_name cannot be empty.")

    return GenerationRequest(
        brand_name=brand_name,
        custom_font_path=custom_font_path,
        default_variant=default_variant,
        generate_all_variants=generate_all_variants,
        icon_mode=icon_mode,
        random_seed=random_seed,
    )


class DemoRequestHandler(BaseHTTPRequestHandler):
    server_version = "AvantDemoHTTP/1.0"

    def _send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, file_path: Path, content_type: str | None = None) -> None:
        if not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        raw = file_path.read_bytes()
        guessed_type = content_type or mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", guessed_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _serve_generated(self, requested_path: str) -> None:
        relative = requested_path.lstrip("/")
        target = (WORKSPACE_ROOT / relative).resolve()
        generated_root = OUTPUT_ROOT.resolve()
        if generated_root not in target.parents:
            self.send_error(HTTPStatus.FORBIDDEN, "Path is outside generated directory")
            return
        self._serve_file(target)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        request_path = unquote(parsed.path)

        if request_path in ("/", "/index.html"):
            self._serve_file(INDEX_FILE, "text/html; charset=utf-8")
            return

        if request_path == "/api/options":
            self._send_json(
                {
                    "brand_name": DEFAULT_BRAND_NAME,
                    "default_variant": DEFAULT_VARIANT,
                    "generate_all_variants": DEFAULT_GENERATE_ALL_VARIANTS,
                    "default_icon_mode": DEFAULT_ICON_MODE,
                    "variants": list(VARIANTS.keys()),
                    "icon_modes": list(ICON_MODES),
                }
            )
            return

        if request_path.startswith("/generated/"):
            self._serve_generated(request_path)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Unknown route")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        request_path = unquote(parsed.path)
        if request_path != "/api/generate":
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown route")
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            payload_raw = self.rfile.read(content_length) if content_length > 0 else b"{}"
            payload = json.loads(payload_raw.decode("utf-8"))
            request = parse_generation_payload(payload)
            result = generate_assets(request)
            self._send_json({"ok": True, "result": result})
        except json.JSONDecodeError:
            self._send_json({"ok": False, "error": "Invalid JSON payload."}, status=HTTPStatus.BAD_REQUEST)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover - defensive response
            self._send_json({"ok": False, "error": f"Internal error: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)


def run_server(host: str, port: int) -> None:
    if not INDEX_FILE.exists():
        raise FileNotFoundError(f"Missing static page: {INDEX_FILE}")

    server = ThreadingHTTPServer((host, port), DemoRequestHandler)
    url = f"http://{host}:{port}"
    print(f"Logo generator server is running at {url}")
    print("Open this URL in browser and configure options in the HTML page.")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()


def run_generate_once(args: argparse.Namespace) -> None:
    request = GenerationRequest(
        brand_name=args.brand_name,
        custom_font_path=args.custom_font_path,
        default_variant=args.default_variant,
        generate_all_variants=args.generate_all_variants,
        icon_mode=args.icon_mode,
        random_seed=args.random_seed,
    )
    result = generate_assets(request)
    print(f"Done. Default template: {result['default_variant']}")
    for item in result["files"]:
        print(item["relative_path"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Brand logo generator. By default it starts a local HTTP server with a static HTML page "
            "for interactive configuration."
        )
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP server host")
    parser.add_argument("--port", default=8000, type=int, help="HTTP server port")
    parser.add_argument(
        "--generate",
        action="store_true",
        help="Generate files once in CLI mode instead of starting HTTP server",
    )
    parser.add_argument("--brand-name", default=DEFAULT_BRAND_NAME, help="Brand name used in logo text")
    parser.add_argument(
        "--default-variant",
        default=DEFAULT_VARIANT,
        choices=sorted(VARIANTS.keys()),
        help="Default icon variant for final exported files",
    )
    parser.add_argument("--custom-font-path", default=DEFAULT_FONT_PATH, help="Optional custom font path")
    parser.add_argument(
        "--generate-all-variants",
        action="store_true",
        help="Also export variant candidate files in addition to the final files",
    )
    parser.add_argument(
        "--icon-mode",
        default=DEFAULT_ICON_MODE,
        choices=sorted(ICON_MODES),
        help="Icon generation mode: static variant, brand initial, or brand-seeded",
    )
    parser.add_argument(
        "--random-seed",
        default=None,
        help="Optional seed used in brand_seeded mode (empty uses brand name)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.generate:
        run_generate_once(args)
        return
    run_server(args.host, args.port)


if __name__ == "__main__":
    main()
