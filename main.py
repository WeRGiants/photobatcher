import os
import re
import shutil
import zipfile
import datetime
from io import BytesIO
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from PIL import Image, ImageEnhance, ImageOps

Image.MAX_IMAGE_PIXELS = 50_000_000

app = FastAPI()

UPLOAD_DIR = "temp_uploads"
PROCESSED_DIR = "temp_processed"


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


# =====================================================
# IMAGE PROCESSING
# =====================================================

def enhance_image(image: Image.Image) -> Image.Image:
    image = ImageEnhance.Brightness(image).enhance(1.05)
    image = ImageEnhance.Contrast(image).enhance(1.10)
    image = ImageEnhance.Sharpness(image).enhance(1.05)
    return image


def resize_for_platform(image: Image.Image, platform: str) -> Image.Image:
    if platform == "ebay":
        max_size = (1600, 1600)
    elif platform == "poshmark":
        max_size = (1080, 1080)
    elif platform == "mercari":
        max_size = (1200, 1200)
    else:
        return image

    image.thumbnail(max_size, Image.LANCZOS)
    return image


# =====================================================
# FRONTEND
# =====================================================

@app.get("/", response_class=HTMLResponse)
async def home():
    return """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>PhotoBatcher</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>

<body class="bg-gray-50 min-h-screen">

<div class="max-w-5xl mx-auto px-4 py-10">

<h1 class="text-3xl font-bold mb-6">PhotoBatcher</h1>

<div class="grid grid-cols-1 lg:grid-cols-2 gap-6">

<!-- LEFT PANEL -->
<div class="bg-white rounded-2xl shadow p-6">

<form id="uploadForm" class="space-y-4">

<input id="fileInput" type="file" name="files" multiple required class="hidden"/>

<div id="dropzone" class="cursor-pointer border-2 border-dashed rounded-xl p-6 text-center">
<p class="font-semibold">Drag & drop images</p>
<p class="text-sm text-gray-500">or click to select</p>
<p id="fileCount" class="text-xs text-gray-400 mt-1">No files selected</p>
</div>

<input name="item_title" id="itemTitle" placeholder="Item title"
class="w-full border rounded-xl px-4 py-3"/>

<div class="flex gap-4">
<label><input type="checkbox" name="platforms" value="ebay"> eBay</label>
<label><input type="checkbox" name="platforms" value="poshmark"> Poshmark</label>
<label><input type="checkbox" name="platforms" value="mercari"> Mercari</label>
</div>

<button class="w-full bg-black text-white py-3 rounded-xl">
Process Photos
</button>

</form>
</div>

<!-- RIGHT PANEL -->
<div class="bg-white rounded-2xl shadow p-6">

<h2 class="font-semibold mb-3">Before / After Preview</h2>

<div class="relative w-full aspect-square rounded-xl overflow-hidden border">

<img id="beforeImg" class="absolute inset-0 w-full h-full object-cover"/>
<img id="afterImg" class="absolute inset-0 w-full h-full object-cover"/>

<input type="range" min="0" max="100" value="50"
id="slider"
class="absolute bottom-4 left-1/2 -translate-x-1/2 w-3/4"/>

</div>

</div>

</div>
</div>

<script>
const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("fileInput");
const fileCount = document.getElementById("fileCount");
const beforeImg = document.getElementById("beforeImg");
const afterImg = document.getElementById("afterImg");
const slider = document.getElementById("slider");

dropzone.onclick = () => fileInput.click();

fileInput.addEventListener("change", () => {
  const files = fileInput.files;
  fileCount.textContent = files.length + " file(s) selected";

  if (files.length > 0) {
    const reader = new FileReader();
    reader.onload = function(e) {
      beforeImg.src = e.target.result;
      afterImg.src = e.target.result;
    };
    reader.readAsDataURL(files[0]);
  }
});

slider.addEventListener("input", (e) => {
  const value = e.target.value;
  afterImg.style.clipPath = `inset(0 ${100-value}% 0 0)`;
});
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
        raise HTTPException(status_code=400, detail="No platform selected")

    title_slug = slugify_title(item_title)

    if os.path.exists(UPLOAD_DIR):
        shutil.rmtree(UPLOAD_DIR)
    if os.path.exists(PROCESSED_DIR):
        shutil.rmtree(PROCESSED_DIR)

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(PROCESSED_DIR, exist_ok=True)

    # Save uploads
    for file in files:
        file_path = os.path.join(UPLOAD_DIR, file.filename)
        with open(file_path, "wb") as buffer:
            buffer.write(await file.read())

    # Process
    for platform in platforms:
        platform_folder = os.path.join(PROCESSED_DIR, platform)
        os.makedirs(platform_folder, exist_ok=True)

        for filename in os.listdir(UPLOAD_DIR):
            img_path = os.path.join(UPLOAD_DIR, filename)

            with Image.open(img_path) as img:
                img = ImageOps.exif_transpose(img)  # AUTO ROTATE
                img = img.convert("RGB")
                img = enhance_image(img)
                img = resize_for_platform(img, platform)

                output_path = os.path.join(platform_folder, filename)

                img.save(
                    output_path,
                    format="JPEG",
                    quality=85,
                    optimize=True,
                    progressive=True
                )

    # ZIP
    zip_buffer = BytesIO()
    date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    parent_folder = f"{title_slug}_PhotoBatcher_{date_str}"

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for root, _, files_on_disk in os.walk(PROCESSED_DIR):
            for f in files_on_disk:
                full_path = os.path.join(root, f)
                relative_path = os.path.relpath(full_path, PROCESSED_DIR)
                zip_path = os.path.join(parent_folder, relative_path)
                zip_file.write(full_path, zip_path)

    zip_buffer.seek(0)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="PhotoBatcher_{title_slug}_{timestamp}.zip"'
        }
    )
