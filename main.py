import os
import re
import shutil
import zipfile
import datetime
from io import BytesIO
from typing import List, Optional, Tuple

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from PIL import Image, ImageEnhance, ImageOps, ImageChops

Image.MAX_IMAGE_PIXELS = 50_000_000

app = FastAPI()

UPLOAD_DIR = "temp_uploads"
PROCESSED_DIR = "temp_processed"
MAX_IMAGES = 24


# =====================================================
# HELPERS
# =====================================================

def slugify_title(title: str) -> str:
    title = (title or "").strip()
    if not title:
        return "Batch"
    title = title.replace(" ", "_")
    title = re.sub(r"[^A-Za-z0-9_\-]", "", title)
    title = re.sub(r"_+", "_", title).strip("_")
    return title or "Batch"


def smart_crop_whitespace(img: Image.Image, padding_ratio: float = 0.06) -> Image.Image:
    gray = img.convert("L")
    bg = Image.new("L", gray.size, 255)

    diff = ImageChops.difference(gray, bg)
    diff = ImageEnhance.Contrast(diff).enhance(2.0)

    bbox = diff.getbbox()
    if not bbox:
        return img

    left, upper, right, lower = bbox
    w = right - left
    h = lower - upper

    pad_w = int(w * padding_ratio)
    pad_h = int(h * padding_ratio)

    left = max(0, left - pad_w)
    upper = max(0, upper - pad_h)
    right = min(img.width, right + pad_w)
    lower = min(img.height, lower + pad_h)

    return img.crop((left, upper, right, lower))


def to_square_canvas(img: Image.Image, background: Tuple[int, int, int] = (255, 255, 255)) -> Image.Image:
    w, h = img.size
    side = max(w, h)
    canvas = Image.new("RGB", (side, side), background)
    canvas.paste(img, ((side - w) // 2, (side - h) // 2))
    return canvas


def enhance_export(image: Image.Image) -> Image.Image:
    image = ImageEnhance.Brightness(image).enhance(1.05)
    image = ImageEnhance.Contrast(image).enhance(1.10)
    image = ImageEnhance.Sharpness(image).enhance(1.05)
    return image


def resize_for_platform(image: Image.Image, platform: str) -> Image.Image:
    sizes = {
        "ebay": (1600, 1600),
        "poshmark": (1080, 1080),
        "mercari": (1200, 1200),
    }
    if platform not in sizes:
        return image
    return image.resize(sizes[platform], Image.LANCZOS)


# =====================================================
# FRONTEND
# =====================================================

@app.get("/", response_class=HTMLResponse)
async def home():
    return """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>PhotoBatcher</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>

<body class="bg-gray-50 min-h-screen">
  <div class="max-w-5xl mx-auto px-4 py-10">

    <div class="flex items-center justify-between mb-8">
      <div>
        <h1 class="text-3xl font-bold">PhotoBatcher</h1>
        <p class="text-gray-600 mt-1">Batch clean, crop, frame, resize, and export marketplace-ready photos.</p>
      </div>
      <span class="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-white border text-sm">
        <span class="w-2 h-2 bg-green-500 rounded-full"></span> Live
      </span>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">

      <!-- LEFT -->
      <div class="bg-white rounded-2xl border p-6">
        <form id="uploadForm" class="space-y-5">

          <div>
            <label class="block text-sm font-semibold mb-2">Item title</label>
            <input id="itemTitle" name="item_title" type="text"
              class="w-full border rounded-xl px-4 py-3"
              placeholder="e.g. Nike Air Max 90"/>
          </div>

          <div>
            <label class="block text-sm font-semibold mb-2">Photos (max 24)</label>
            <input id="fileInput" type="file" name="files" multiple required class="hidden"/>
            <div id="dropzone" class="border-2 border-dashed rounded-xl p-6 text-center cursor-pointer bg-gray-50 hover:bg-gray-100">
              Drag & drop images or click to select
              <p id="fileCount" class="text-xs text-gray-500 mt-2">No files selected</p>
            </div>
          </div>

          <div>
            <label class="block text-sm font-semibold mb-2">Platforms</label>
            <div class="flex gap-4">
              <label><input type="checkbox" name="platforms" value="ebay"> eBay</label>
              <label><input type="checkbox" name="platforms" value="poshmark"> Poshmark</label>
              <label><input type="checkbox" name="platforms" value="mercari"> Mercari</label>
            </div>
          </div>

          <button id="processBtn" type="submit"
            class="w-full bg-black text-white rounded-xl py-3 font-semibold">
            Process Photos
          </button>
        </form>
      </div>

      <!-- RIGHT -->
      <div class="bg-white rounded-2xl border p-6">
        <h2 class="text-sm font-semibold mb-4">Preview</h2>
        <div id="thumbGrid" class="grid grid-cols-3 gap-3"></div>
      </div>

    </div>
  </div>

<script>
  const MAX_IMAGES = 24;
  const fileInput = document.getElementById("fileInput");
  const dropzone = document.getElementById("dropzone");
  const fileCount = document.getElementById("fileCount");
  const thumbGrid = document.getElementById("thumbGrid");

  function renderThumbs(files) {
    thumbGrid.innerHTML = "";
    for (let i = 0; i < files.length; i++) {
      const url = URL.createObjectURL(files[i]);
      const img = document.createElement("img");
      img.src = url;
      img.className = "rounded-lg object-cover aspect-square";
      thumbGrid.appendChild(img);
    }
  }

  function setFiles(files) {
    if (files.length > MAX_IMAGES) {
      alert("Maximum 24 images allowed.");
      return;
    }
    fileInput.files = files;
    fileCount.textContent = files.length + " file(s) selected";
    renderThumbs(files);
  }

  dropzone.onclick = () => fileInput.click();
  fileInput.onchange = () => setFiles(fileInput.files);

  dropzone.ondrop = (e) => {
    e.preventDefault();
    setFiles(e.dataTransfer.files);
  };
  dropzone.ondragover = (e) => e.preventDefault();

  document.getElementById("uploadForm").onsubmit = async function(e) {
    e.preventDefault();

    if (fileInput.files.length > MAX_IMAGES) {
      alert("Maximum 24 images allowed.");
      return;
    }

    const formData = new FormData(this);
    const res = await fetch("/process", { method: "POST", body: formData });

    if (!res.ok) {
      alert("Error processing images.");
      return;
    }

    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "PhotoBatcher.zip";
    a.click();
  };
</script>
</body>
</html>
"""


# =====================================================
# PROCESS ENDPOINT
# =====================================================

@app.post("/process")
async def process_images(
    files: List[UploadFile] = File(...),
    platforms: Optional[List[str]] = Form(None),
    item_title: Optional[str] = Form(None),
):
    if not platforms:
        raise HTTPException(status_code=400, detail="Select at least one platform")

    if len(files) > MAX_IMAGES:
        raise HTTPException(status_code=400, detail="Maximum 24 images allowed")

    title_slug = slugify_title(item_title)
    date_str = datetime.datetime.now().strftime("%Y-%m-%d")

    if os.path.exists(UPLOAD_DIR):
        shutil.rmtree(UPLOAD_DIR)
    if os.path.exists(PROCESSED_DIR):
        shutil.rmtree(PROCESSED_DIR)

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(PROCESSED_DIR, exist_ok=True)

    for f in files:
        path = os.path.join(UPLOAD_DIR, f.filename)
        with open(path, "wb") as buffer:
            buffer.write(await f.read())

    for platform in platforms:
        folder = os.path.join(PROCESSED_DIR, platform)
        os.makedirs(folder, exist_ok=True)

        for filename in os.listdir(UPLOAD_DIR):
            img_path = os.path.join(UPLOAD_DIR, filename)
            with Image.open(img_path) as img:
                img = ImageOps.exif_transpose(img)
                img = img.convert("RGB")
                img = smart_crop_whitespace(img)
                img = to_square_canvas(img)
                img = enhance_export(img)
                img = resize_for_platform(img, platform)

                base, _ = os.path.splitext(filename)
                img.save(os.path.join(folder, base + ".jpg"),
                         format="JPEG",
                         quality=85,
                         optimize=True,
                         progressive=True)

    zip_buffer = BytesIO()
    parent = f"{title_slug}_PhotoBatcher_{date_str}"

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
        headers={"Content-Disposition": f'attachment; filename=\"{parent}.zip\"'}
    )
