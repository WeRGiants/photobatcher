import os
import re
import shutil
import zipfile
import datetime
from io import BytesIO
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from PIL import Image, ImageEnhance, ImageOps, ImageChops
import numpy as np

Image.MAX_IMAGE_PIXELS = 50_000_000

app = FastAPI()

UPLOAD_DIR = "temp_uploads"
PROCESSED_DIR = "temp_processed"
MAX_IMAGES = 24


# =====================================================
# UTILITIES
# =====================================================

def slugify(title: str) -> str:
    title = (title or "").strip()
    if not title:
        return "Batch"
    title = title.replace(" ", "_")
    title = re.sub(r"[^A-Za-z0-9_\-]", "", title)
    title = re.sub(r"_+", "_", title).strip("_")
    return title or "Batch"


# =====================================================
# BACKGROUND LEVELING
# =====================================================

def level_background(img: Image.Image) -> Image.Image:
    arr = np.array(img).astype(np.float32)
    hsv = Image.fromarray(arr.astype(np.uint8)).convert("HSV")
    hsv_arr = np.array(hsv).astype(np.float32)

    h, s, v = hsv_arr[:, :, 0], hsv_arr[:, :, 1], hsv_arr[:, :, 2]
    mask = (v > 200) & (s < 60)
    v[mask] = np.clip(v[mask] * 1.08 + 10, 0, 255)

    hsv_arr[:, :, 2] = v
    return Image.fromarray(hsv_arr.astype(np.uint8), "HSV").convert("RGB")


# =====================================================
# IMAGE PIPELINE
# =====================================================

def smart_crop(img: Image.Image, padding_ratio=0.06) -> Image.Image:
    gray = img.convert("L")
    bg = Image.new("L", gray.size, 255)

    diff = ImageChops.difference(gray, bg)
    diff = ImageEnhance.Contrast(diff).enhance(2.0)

    bbox = diff.getbbox()
    if not bbox:
        return img

    left, top, right, bottom = bbox
    w = right - left
    h = bottom - top

    pad_w = int(w * padding_ratio)
    pad_h = int(h * padding_ratio)

    left = max(0, left - pad_w)
    top = max(0, top - pad_h)
    right = min(img.width, right + pad_w)
    bottom = min(img.height, bottom + pad_h)

    return img.crop((left, top, right, bottom))


def to_square(img: Image.Image) -> Image.Image:
    w, h = img.size
    side = max(w, h)
    canvas = Image.new("RGB", (side, side), (255, 255, 255))
    canvas.paste(img, ((side - w) // 2, (side - h) // 2))
    return canvas


def enhance(img: Image.Image) -> Image.Image:
    img = level_background(img)
    img = ImageEnhance.Brightness(img).enhance(1.05)
    img = ImageEnhance.Contrast(img).enhance(1.12)
    img = ImageEnhance.Color(img).enhance(1.04)
    img = ImageEnhance.Sharpness(img).enhance(1.05)
    return img


def resize_platform(img: Image.Image, platform: str) -> Image.Image:
    sizes = {
        "ebay": 1600,
        "poshmark": 1080,
        "mercari": 1200,
    }
    if platform not in sizes:
        return img
    return img.resize((sizes[platform], sizes[platform]), Image.LANCZOS)


# =====================================================
# FRONTEND UI
# =====================================================

@app.get("/", response_class=HTMLResponse)
async def home():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>PhotoBatcher</title>
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-gray-100 min-h-screen flex items-center justify-center p-8">
        <div class="bg-white shadow-xl rounded-2xl p-8 w-full max-w-xl">
            <h1 class="text-3xl font-bold mb-6 text-center">PhotoBatcher</h1>

            <form id="uploadForm" class="space-y-4">
                <input type="text" name="item_title" placeholder="Item Title"
                    class="w-full border rounded-lg p-2" />

                <input type="file" name="files" multiple required
                    class="w-full border rounded-lg p-2" />

                <div class="flex gap-4">
                    <label><input type="checkbox" name="platforms" value="ebay" checked> eBay</label>
                    <label><input type="checkbox" name="platforms" value="poshmark"> Poshmark</label>
                    <label><input type="checkbox" name="platforms" value="mercari"> Mercari</label>
                </div>

                <button type="submit"
                    class="w-full bg-black text-white rounded-lg py-2">
                    Process Photos
                </button>
            </form>
        </div>

        <script>
        const form = document.getElementById("uploadForm");

        form.addEventListener("submit", async (e) => {
            e.preventDefault();

            const formData = new FormData(form);

            const response = await fetch("/process", {
                method: "POST",
                body: formData
            });

            if (!response.ok) {
                alert("Error processing images.");
                return;
            }

            const blob = await response.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = "PhotoBatcher.zip";
            document.body.appendChild(a);
            a.click();
            a.remove();
        });
        </script>
    </body>
    </html>
    """


@app.get("/health")
async def health():
    return {"status": "ok"}


# =====================================================
# PROCESS ENDPOINT
# =====================================================

@app.post("/process")
async def process(
    files: List[UploadFile] = File(...),
    platforms: Optional[List[str]] = Form(None),
    item_title: Optional[str] = Form(None),
):
    if not platforms:
        raise HTTPException(400, "Select at least one platform")
    if len(files) > MAX_IMAGES:
        raise HTTPException(400, "Maximum 24 images")

    title = slugify(item_title)
    date = datetime.datetime.now().strftime("%Y-%m-%d")

    if os.path.exists(UPLOAD_DIR):
        shutil.rmtree(UPLOAD_DIR)
    if os.path.exists(PROCESSED_DIR):
        shutil.rmtree(PROCESSED_DIR)

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(PROCESSED_DIR, exist_ok=True)

    for f in files:
        with open(os.path.join(UPLOAD_DIR, f.filename), "wb") as buffer:
            buffer.write(await f.read())

    for platform in platforms:
        folder = os.path.join(PROCESSED_DIR, platform)
        os.makedirs(folder, exist_ok=True)

        for name in os.listdir(UPLOAD_DIR):
            with Image.open(os.path.join(UPLOAD_DIR, name)) as img:
                img = ImageOps.exif_transpose(img)
                img = img.convert("RGB")
                img = smart_crop(img)
                img = to_square(img)
                img = enhance(img)
                img = resize_platform(img, platform)

                base, _ = os.path.splitext(name)
                img.save(
                    os.path.join(folder, base + ".jpg"),
                    format="JPEG",
                    quality=85,
                    optimize=True,
                    progressive=True,
                )

    zip_buffer = BytesIO()
    parent = f"{title}_PhotoBatcher_{date}"

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root, _, files_on_disk in os.walk(PROCESSED_DIR):
            for f in files_on_disk:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, PROCESSED_DIR)
                zipf.write(full, os.path.join(parent, rel))

    zip_buffer.seek(0)

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{parent}.zip"'},
    )
