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
# IMAGE PROCESSING PIPELINE
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


def to_square(img: Image.Image, background=(255, 255, 255)) -> Image.Image:
    w, h = img.size
    side = max(w, h)
    canvas = Image.new("RGB", (side, side), background)
    canvas.paste(img, ((side - w) // 2, (side - h) // 2))
    return canvas


def enhance(img: Image.Image) -> Image.Image:
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
# FRONTEND (PREMIUM RESTORED)
# =====================================================

@app.get("/", response_class=HTMLResponse)
async def home():
    return """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>PhotoBatcher</title>
<script src="https://cdn.tailwindcss.com"></script>
</head>

<body class="bg-gray-50 min-h-screen">

<div id="overlay" class="hidden fixed inset-0 bg-black/40 backdrop-blur-sm z-50 flex items-center justify-center">
  <div class="bg-white p-8 rounded-2xl shadow-xl text-center w-80">
    <div class="w-10 h-10 border-4 border-gray-200 border-t-black rounded-full animate-spin mx-auto"></div>
    <p class="mt-4 font-semibold">Processing your photos…</p>
  </div>
</div>

<div class="max-w-6xl mx-auto px-6 py-12">

  <div class="flex justify-between items-center mb-10">
    <div>
      <h1 class="text-3xl font-bold">PhotoBatcher</h1>
      <p class="text-gray-600 mt-1">Batch clean, crop, frame, resize, and export marketplace-ready photos.</p>
    </div>
    <span class="inline-flex items-center gap-2 px-3 py-1 bg-white border rounded-full text-sm">
      <span class="w-2 h-2 bg-green-500 rounded-full"></span> Live
    </span>
  </div>

  <div class="grid grid-cols-1 lg:grid-cols-2 gap-8">

    <div class="bg-white p-6 rounded-2xl border">
      <form id="form" class="space-y-6">

        <div>
          <label class="block font-semibold mb-2 text-sm">Item title</label>
          <input name="item_title" type="text"
                 class="w-full border rounded-xl px-4 py-3"
                 placeholder="e.g. Nike Air Max 90"/>
        </div>

        <div>
          <label class="block font-semibold mb-2 text-sm">Photos (max 24)</label>
          <input id="files" name="files" type="file" multiple required class="hidden"/>
          <div id="drop" class="border-2 border-dashed rounded-xl p-8 text-center cursor-pointer bg-gray-50 hover:bg-gray-100">
            Drag & drop images or click to select
            <p id="count" class="text-xs text-gray-500 mt-2">No files selected</p>
          </div>
        </div>

        <div>
          <label class="block font-semibold mb-2 text-sm">Platforms</label>
          <div class="flex gap-6 text-sm">
            <label><input type="checkbox" name="platforms" value="ebay"> eBay</label>
            <label><input type="checkbox" name="platforms" value="poshmark"> Poshmark</label>
            <label><input type="checkbox" name="platforms" value="mercari"> Mercari</label>
          </div>
        </div>

        <button class="w-full bg-black text-white rounded-xl py-3 font-semibold">
          Process Photos
        </button>

      </form>
    </div>

    <div class="bg-white p-6 rounded-2xl border">
      <h2 class="font-semibold text-sm mb-4">Preview</h2>
      <div id="grid" class="grid grid-cols-3 gap-4"></div>
    </div>

  </div>
</div>

<script>
const MAX = 24;
const input = document.getElementById("files");
const drop = document.getElementById("drop");
const count = document.getElementById("count");
const grid = document.getElementById("grid");
const overlay = document.getElementById("overlay");

function render(files){
  grid.innerHTML = "";
  for(let i=0;i<files.length;i++){
    const url = URL.createObjectURL(files[i]);
    const img = document.createElement("img");
    img.src = url;
    img.className = "aspect-square w-full object-cover rounded-xl";
    grid.appendChild(img);
  }
}

function setFiles(files){
  if(files.length > MAX){
    alert("Maximum 24 images allowed.");
    return;
  }
  input.files = files;
  count.textContent = files.length + " file(s) selected";
  render(files);
}

drop.onclick = () => input.click();
input.onchange = () => setFiles(input.files);
drop.ondragover = e => e.preventDefault();
drop.ondrop = e => { e.preventDefault(); setFiles(e.dataTransfer.files); };

document.getElementById("form").onsubmit = async function(e){
  e.preventDefault();
  overlay.classList.remove("hidden");
  const data = new FormData(this);
  const res = await fetch("/process",{method:"POST",body:data});
  overlay.classList.add("hidden");
  if(!res.ok){ alert("Error"); return; }
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

    if os.path.exists(UPLOAD_DIR): shutil.rmtree(UPLOAD_DIR)
    if os.path.exists(PROCESSED_DIR): shutil.rmtree(PROCESSED_DIR)
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
                img.save(os.path.join(folder, base+".jpg"),
                         format="JPEG",
                         quality=85,
                         optimize=True,
                         progressive=True)

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
