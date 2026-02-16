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


# ===============================
# Utilities
# ===============================

def slugify(title: str) -> str:
    title = (title or "").strip()
    if not title:
        return "Batch"
    title = re.sub(r"[^A-Za-z0-9_\-]", "", title.replace(" ", "_"))
    return title or "Batch"


# ===============================
# Enhancement Pipeline
# ===============================

def level_background(img: Image.Image) -> Image.Image:
    arr = np.array(img).astype(np.float32)
    hsv = Image.fromarray(arr.astype(np.uint8)).convert("HSV")
    hsv_arr = np.array(hsv).astype(np.float32)
    h, s, v = hsv_arr[:,:,0], hsv_arr[:,:,1], hsv_arr[:,:,2]
    mask = (v > 200) & (s < 60)
    v[mask] = np.clip(v[mask] * 1.08 + 10, 0, 255)
    hsv_arr[:,:,2] = v
    return Image.fromarray(hsv_arr.astype(np.uint8), "HSV").convert("RGB")


def smart_crop(img: Image.Image) -> Image.Image:
    gray = img.convert("L")
    bg = Image.new("L", gray.size, 255)
    diff = ImageChops.difference(gray, bg)
    diff = ImageEnhance.Contrast(diff).enhance(2.0)
    bbox = diff.getbbox()
    if not bbox:
        return img
    left, top, right, bottom = bbox
    return img.crop((left, top, right, bottom))


def to_square(img: Image.Image) -> Image.Image:
    side = max(img.size)
    canvas = Image.new("RGB", (side, side), (255,255,255))
    canvas.paste(img, ((side-img.width)//2, (side-img.height)//2))
    return canvas


def enhance(img: Image.Image) -> Image.Image:
    img = level_background(img)
    img = ImageEnhance.Brightness(img).enhance(1.05)
    img = ImageEnhance.Contrast(img).enhance(1.12)
    img = ImageEnhance.Color(img).enhance(1.04)
    img = ImageEnhance.Sharpness(img).enhance(1.05)
    return img


def resize_platform(img: Image.Image, platform: str) -> Image.Image:
    sizes = {"ebay":1600,"poshmark":1080,"mercari":1200}
    return img.resize((sizes[platform], sizes[platform]), Image.LANCZOS)


# ===============================
# UI
# ===============================

@app.get("/", response_class=HTMLResponse)
async def home():
    return """
<!DOCTYPE html>
<html>
<head>
<title>PhotoBatcher</title>
<script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100 min-h-screen p-10">

<div class="max-w-6xl mx-auto">
    <h1 class="text-4xl font-bold mb-2">PhotoBatcher</h1>
    <p class="text-gray-600 mb-6">
        Batch clean, resize, and export marketplace-ready photos in one click.
    </p>

    <div class="grid grid-cols-2 gap-8">

        <!-- LEFT -->
        <div class="bg-white p-6 rounded-xl shadow">

            <label class="block mb-2 font-medium">Item Title</label>
            <input id="title" class="w-full border p-2 rounded mb-4"/>

            <label class="block mb-2 font-medium">Photos</label>
            <input id="fileInput" type="file" multiple class="mb-4"/>

            <div class="mb-4">
                <label><input type="checkbox" value="ebay" checked> eBay</label>
                <label class="ml-4"><input type="checkbox" value="poshmark" checked> Poshmark</label>
                <label class="ml-4"><input type="checkbox" value="mercari"> Mercari</label>
            </div>

            <button id="processBtn"
                class="w-full bg-black text-white py-3 rounded-lg">
                Process Photos
            </button>

        </div>

        <!-- RIGHT -->
        <div class="bg-white p-6 rounded-xl shadow">
            <h2 class="font-semibold mb-4">Preview</h2>
            <div id="previewGrid"
                 class="grid grid-cols-4 gap-4">
            </div>
        </div>

    </div>
</div>

<!-- Processing Modal -->
<div id="modal"
     class="fixed inset-0 bg-black bg-opacity-40 hidden flex items-center justify-center">
    <div class="bg-white p-8 rounded-xl shadow-xl text-center">
        <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-black mx-auto mb-4"></div>
        <p class="font-medium">Processing your photos...</p>
        <p class="text-gray-500 text-sm">This can take a moment for large batches.</p>
    </div>
</div>

<script>

const input = document.getElementById("fileInput");
const grid = document.getElementById("previewGrid");
const btn = document.getElementById("processBtn");
const modal = document.getElementById("modal");

input.addEventListener("change", () => {
    grid.innerHTML = "";
    Array.from(input.files).slice(0,24).forEach(file => {
        const reader = new FileReader();
        reader.onload = e => {
            const img = document.createElement("img");
            img.src = e.target.result;
            img.className = "rounded-lg shadow";
            grid.appendChild(img);
        };
        reader.readAsDataURL(file);
    });
});

btn.addEventListener("click", async () => {

    const files = input.files;
    if (!files.length) return alert("Select images first.");

    const form = new FormData();
    Array.from(files).forEach(f => form.append("files", f));

    document.querySelectorAll("input[type=checkbox]:checked")
        .forEach(cb => form.append("platforms", cb.value));

    form.append("item_title", document.getElementById("title").value);

    modal.classList.remove("hidden");

    const res = await fetch("/process", {
        method: "POST",
        body: form
    });

    modal.classList.add("hidden");

    if (!res.ok) return alert("Error processing images.");

    const blob = await res.blob();
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


# ===============================
# PROCESS
# ===============================

@app.post("/process")
async def process(
    files: List[UploadFile] = File(...),
    platforms: Optional[List[str]] = Form(None),
    item_title: Optional[str] = Form(None),
):
    if not platforms:
        raise HTTPException(400, "Select platform")

    if len(files) > MAX_IMAGES:
        raise HTTPException(400, "Max 24 images")

    title = slugify(item_title)
    date = datetime.datetime.now().strftime("%Y-%m-%d")

    if os.path.exists(UPLOAD_DIR):
        shutil.rmtree(UPLOAD_DIR)
    if os.path.exists(PROCESSED_DIR):
        shutil.rmtree(PROCESSED_DIR)

    os.makedirs(UPLOAD_DIR)
    os.makedirs(PROCESSED_DIR)

    for f in files:
        with open(os.path.join(UPLOAD_DIR, f.filename), "wb") as buffer:
            buffer.write(await f.read())

    for platform in platforms:
        folder = os.path.join(PROCESSED_DIR, platform)
        os.makedirs(folder)

        for name in os.listdir(UPLOAD_DIR):
            with Image.open(os.path.join(UPLOAD_DIR, name)) as img:
                img = ImageOps.exif_transpose(img)
                img = img.convert("RGB")
                img = smart_crop(img)
                img = to_square(img)
                img = enhance(img)
                img = resize_platform(img, platform)

                base,_ = os.path.splitext(name)
                img.save(os.path.join(folder, base+".jpg"),
                         format="JPEG", quality=85)

    zip_buffer = BytesIO()
    parent = f"{title}_PhotoBatcher_{date}"

    with zipfile.ZipFile(zip_buffer,"w",zipfile.ZIP_DEFLATED) as zipf:
        for root,_,files_on_disk in os.walk(PROCESSED_DIR):
            for f in files_on_disk:
                full = os.path.join(root,f)
                rel = os.path.relpath(full,PROCESSED_DIR)
                zipf.write(full,os.path.join(parent,rel))

    zip_buffer.seek(0)

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{parent}.zip"'}
    )
