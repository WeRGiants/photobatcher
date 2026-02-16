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

# Protect against extremely large images (memory safety)
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


def enhance_image_export(image: Image.Image) -> Image.Image:
    """Subtle, marketplace-safe enhancement (export)."""
    image = ImageEnhance.Brightness(image).enhance(1.05)
    image = ImageEnhance.Contrast(image).enhance(1.10)
    image = ImageEnhance.Sharpness(image).enhance(1.05)
    return image


def enhance_image_preview(image: Image.Image) -> Image.Image:
    """Slightly stronger enhancement (preview) to demonstrate value."""
    image = ImageEnhance.Brightness(image).enhance(1.09)
    image = ImageEnhance.Contrast(image).enhance(1.18)
    image = ImageEnhance.Sharpness(image).enhance(1.12)
    return image


def resize_for_platform(image: Image.Image, platform: str) -> Image.Image:
    """Preserve aspect ratio, avoid upscaling, reduce memory usage."""
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
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>PhotoBatcher</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>

<body class="bg-gray-50 min-h-screen">
  <!-- Loading Overlay -->
  <div id="loadingOverlay" class="hidden fixed inset-0 bg-black/40 backdrop-blur-sm z-50">
    <div class="min-h-screen flex items-center justify-center p-6">
      <div class="bg-white rounded-2xl shadow-xl w-full max-w-sm p-6 text-center">
        <div class="mx-auto w-10 h-10 border-4 border-gray-200 border-t-black rounded-full animate-spin"></div>
        <p class="mt-4 font-semibold text-gray-900">Processing your photos…</p>
        <p class="mt-1 text-sm text-gray-500">This can take a moment for large batches.</p>
      </div>
    </div>
  </div>

  <div class="max-w-5xl mx-auto px-4 py-10">
    <div class="flex items-center justify-between mb-8">
      <div>
        <h1 class="text-3xl font-bold tracking-tight text-gray-900">PhotoBatcher</h1>
        <p class="text-gray-600 mt-1">Batch clean, resize, and export marketplace-ready photos in one click.</p>
      </div>
      <div class="text-sm text-gray-500">
        <span class="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-white shadow-sm border">
          <span class="w-2 h-2 rounded-full bg-green-500"></span>
          Live
        </span>
      </div>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <!-- Left: Inputs -->
      <div class="bg-white rounded-2xl shadow-sm border p-6">
        <form id="uploadForm" class="space-y-5">
          <!-- Item Title -->
          <div>
            <label class="block text-sm font-semibold text-gray-900 mb-2">Item title</label>
            <input
              id="itemTitle"
              name="item_title"
              type="text"
              placeholder="e.g., Nike Air Max 90"
              class="w-full rounded-xl border-gray-200 focus:border-black focus:ring-black px-4 py-3"
              maxlength="80"
            />
            <p class="mt-2 text-xs text-gray-500">
              Used to name your export folder. Spaces will become underscores.
            </p>
          </div>

          <!-- Drag & Drop -->
          <div>
            <label class="block text-sm font-semibold text-gray-900 mb-2">Photos</label>

            <input id="fileInput" type="file" name="files" multiple required class="hidden" />

            <div
              id="dropzone"
              class="group cursor-pointer rounded-2xl border-2 border-dashed border-gray-200 bg-gray-50 hover:bg-gray-100 transition p-6"
            >
              <div class="flex items-center gap-4">
                <div class="w-12 h-12 rounded-xl bg-white border flex items-center justify-center">
                  <svg xmlns="http://www.w3.org/2000/svg" class="w-6 h-6 text-gray-800" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16a4 4 0 01.88-7.903A5 5 0 0115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
                  </svg>
                </div>

                <div class="flex-1">
                  <p class="font-semibold text-gray-900">Drag & drop images here</p>
                  <p class="text-sm text-gray-600">or click to select files</p>
                  <p id="fileCount" class="text-xs text-gray-500 mt-1">No files selected</p>
                </div>
              </div>
            </div>
          </div>

          <!-- Platforms -->
          <div>
            <label class="block text-sm font-semibold text-gray-900 mb-2">Platforms</label>
            <div class="flex flex-wrap gap-3">
              <label class="inline-flex items-center gap-2 px-3 py-2 rounded-xl border bg-white hover:bg-gray-50 cursor-pointer">
                <input type="checkbox" name="platforms" value="ebay" class="rounded border-gray-300 text-black focus:ring-black">
                <span class="text-sm font-medium">eBay</span>
              </label>

              <label class="inline-flex items-center gap-2 px-3 py-2 rounded-xl border bg-white hover:bg-gray-50 cursor-pointer">
                <input type="checkbox" name="platforms" value="poshmark" class="rounded border-gray-300 text-black focus:ring-black">
                <span class="text-sm font-medium">Poshmark</span>
              </label>

              <label class="inline-flex items-center gap-2 px-3 py-2 rounded-xl border bg-white hover:bg-gray-50 cursor-pointer">
                <input type="checkbox" name="platforms" value="mercari" class="rounded border-gray-300 text-black focus:ring-black">
                <span class="text-sm font-medium">Mercari</span>
              </label>
            </div>
            <p class="mt-2 text-xs text-gray-500">We’ll export a separate folder per platform inside the ZIP.</p>
          </div>

          <!-- Batch Name Preview -->
          <div class="rounded-2xl bg-gray-50 border p-4">
            <p class="text-xs text-gray-500 font-semibold">Batch folder preview</p>
            <p id="batchPreview" class="mt-1 font-mono text-sm text-gray-900">Batch_PhotoBatcher_YYYY-MM-DD</p>
          </div>

          <!-- Submit -->
          <button
            id="processBtn"
            type="submit"
            class="w-full rounded-xl bg-black text-white py-3 font-semibold hover:bg-gray-800 transition disabled:opacity-60 disabled:cursor-not-allowed"
          >
            Process Photos
          </button>

          <p class="text-xs text-gray-500 text-center">
            Tip: For best results, use well-lit photos. Output is optimized for marketplace upload speed.
          </p>
        </form>
      </div>

      <!-- Right: Preview -->
      <div class="bg-white rounded-2xl shadow-sm border p-6">
        <div class="flex items-center justify-between mb-4">
          <h2 class="text-sm font-semibold text-gray-900">Preview</h2>
          <span id="previewHint" class="text-xs text-gray-500">Upload images to see thumbnails</span>
        </div>

        <!-- Thumbnails -->
        <div id="thumbGrid" class="grid grid-cols-3 gap-3"></div>

        <div id="emptyState" class="mt-10 text-center text-gray-500">
          <div class="mx-auto w-14 h-14 rounded-2xl bg-gray-50 border flex items-center justify-center">
            <svg xmlns="http://www.w3.org/2000/svg" class="w-7 h-7 text-gray-700" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7h18M3 12h18M3 17h18" />
            </svg>
          </div>
          <p class="mt-3 font-semibold text-gray-900">No previews yet</p>
          <p class="text-sm text-gray-600">Add photos to preview the batch before processing.</p>
        </div>

        <!-- Compare Module -->
        <div id="compareWrap" class="hidden mt-6 pt-6 border-t">
          <div class="flex items-center justify-between mb-3">
            <h3 class="text-sm font-semibold text-gray-900">Before / After</h3>
            <span class="text-xs text-gray-500">Auto preview (stronger enhancement)</span>
          </div>

          <div class="relative w-full aspect-square rounded-2xl overflow-hidden border bg-gray-50">
            <img id="beforeImg" class="absolute inset-0 w-full h-full object-cover" alt="Before"/>
            <img id="afterImg"  class="absolute inset-0 w-full h-full object-cover" alt="After"/>
            <div class="absolute inset-y-0 left-1/2 w-[2px] bg-white/80 pointer-events-none" id="divider"></div>
          </div>

          <input
            id="slider"
            type="range"
            min="0"
            max="100"
            value="50"
            class="mt-4 w-full accent-black"
          />

          <div class="mt-2 flex justify-between text-xs text-gray-500">
            <span>Original</span>
            <span>Enhanced</span>
          </div>
        </div>
      </div>
    </div>
  </div>

<script>
  const form = document.getElementById("uploadForm");
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("fileInput");
  const fileCount = document.getElementById("fileCount");
  const thumbGrid = document.getElementById("thumbGrid");
  const emptyState = document.getElementById("emptyState");
  const previewHint = document.getElementById("previewHint");

  const itemTitle = document.getElementById("itemTitle");
  const batchPreview = document.getElementById("batchPreview");
  const processBtn = document.getElementById("processBtn");
  const loadingOverlay = document.getElementById("loadingOverlay");

  const compareWrap = document.getElementById("compareWrap");
  const beforeImg = document.getElementById("beforeImg");
  const afterImg = document.getElementById("afterImg");
  const slider = document.getElementById("slider");
  const divider = document.getElementById("divider");

  function slugifyTitleClient(title) {
    title = (title || "").trim();
    if (!title) return "Batch";
    title = title.replaceAll(" ", "_");
    title = title.replace(/[^A-Za-z0-9_\\-]/g, "");
    title = title.replace(/_+/g, "_").replace(/^_+|_+$/g, "");
    return title || "Batch";
  }

  function todayString() {
    const d = new Date();
    const yyyy = d.getFullYear();
    const mm = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    return `${yyyy}-${mm}-${dd}`;
  }

  function updateBatchPreview() {
    const t = slugifyTitleClient(itemTitle.value);
    batchPreview.textContent = `${t}_PhotoBatcher_${todayString()}`;
  }

  function setAfterClip(value) {
    // show enhanced on the right side based on slider
    afterImg.style.clipPath = `inset(0 ${100 - value}% 0 0)`;
    divider.style.left = `${value}%`;
  }

  slider.addEventListener("input", (e) => setAfterClip(Number(e.target.value)));
  setAfterClip(50);

  // Assign files to the hidden input using DataTransfer
  function setFiles(files) {
    const dt = new DataTransfer();
    for (const f of files) dt.items.add(f);
    fileInput.files = dt.files;

    const count = fileInput.files.length;
    fileCount.textContent = count ? `${count} file(s) selected` : "No files selected";

    renderThumbs(fileInput.files);

    if (count > 0) {
      // Before image = local (instant)
      const first = fileInput.files[0];
      const beforeUrl = URL.createObjectURL(first);
      beforeImg.src = beforeUrl;

      // After image = server enhanced preview
      fetchPreview(first);
    }
  }

  function renderThumbs(files) {
    thumbGrid.innerHTML = "";

    if (!files || files.length === 0) {
      emptyState.classList.remove("hidden");
      compareWrap.classList.add("hidden");
      previewHint.textContent = "Upload images to see thumbnails";
      return;
    }

    emptyState.classList.add("hidden");
    previewHint.textContent = `${files.length} image(s) ready`;

    const maxThumbs = Math.min(files.length, 12);
    for (let i = 0; i < maxThumbs; i++) {
      const f = files[i];
      const url = URL.createObjectURL(f);

      const wrap = document.createElement("div");
      wrap.className = "relative aspect-square rounded-xl overflow-hidden border bg-gray-50";

      const img = document.createElement("img");
      img.src = url;
      img.alt = f.name;
      img.className = "w-full h-full object-cover";
      img.onload = () => URL.revokeObjectURL(url);

      wrap.appendChild(img);
      thumbGrid.appendChild(wrap);
    }

    if (files.length > maxThumbs) {
      const more = document.createElement("div");
      more.className = "aspect-square rounded-xl border bg-gray-50 flex items-center justify-center text-sm font-semibold text-gray-700";
      more.textContent = `+${files.length - maxThumbs}`;
      thumbGrid.appendChild(more);
    }
  }

  async function fetchPreview(file) {
    compareWrap.classList.remove("hidden");
    previewHint.textContent = "Generating preview…";

    const fd = new FormData();
    fd.append("file", file);

    try {
      const res = await fetch("/preview", { method: "POST", body: fd });
      if (!res.ok) {
        previewHint.textContent = "Preview unavailable";
        return;
      }

      const blob = await res.blob();
      const afterUrl = URL.createObjectURL(blob);
      afterImg.src = afterUrl;

      // Reset slider position for a consistent “wow”
      slider.value = 50;
      setAfterClip(50);

      previewHint.textContent = "Preview ready";
    } catch (e) {
      previewHint.textContent = "Preview unavailable";
    }
  }

  // Click dropzone opens file picker
  dropzone.addEventListener("click", () => fileInput.click());

  // Input change
  fileInput.addEventListener("change", () => setFiles(fileInput.files));

  // Drag events
  dropzone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropzone.classList.add("border-black");
  });
  dropzone.addEventListener("dragleave", () => {
    dropzone.classList.remove("border-black");
  });
  dropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropzone.classList.remove("border-black");
    if (e.dataTransfer.files && e.dataTransfer.files.length) {
      setFiles(e.dataTransfer.files);
    }
  });

  // Title changes update preview
  itemTitle.addEventListener("input", updateBatchPreview);
  updateBatchPreview();

  // Submit
  form.addEventListener("submit", async function(e) {
    e.preventDefault();

    const checked = form.querySelectorAll('input[name="platforms"]:checked');
    if (checked.length === 0) {
      alert("Please select at least one platform.");
      return;
    }

    processBtn.disabled = true;
    loadingOverlay.classList.remove("hidden");

    const formData = new FormData(form);

    try {
      const response = await fetch("/process", { method: "POST", body: formData });

      if (!response.ok) {
        const txt = await response.text();
        alert("Error: " + txt);
        processBtn.disabled = false;
        loadingOverlay.classList.add("hidden");
        return;
      }

      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);

      const a = document.createElement("a");
      a.href = url;
      a.download = "PhotoBatcher_Export.zip";
      document.body.appendChild(a);
      a.click();
      a.remove();

      window.URL.revokeObjectURL(url);

    } catch (err) {
      alert("Something went wrong. Please try again.");
    }

    processBtn.disabled = false;
    loadingOverlay.classList.add("hidden");
  });
</script>
</body>
</html>
"""


# =====================================================
# PREVIEW ENDPOINT (STRONGER ENHANCEMENT, EXIF SAFE)
# =====================================================

@app.post("/preview")
async def preview_image(file: UploadFile = File(...)):
    data = await file.read()

    try:
        with Image.open(BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)  # auto-rotate iPhone photos
            img = img.convert("RGB")

            # Stronger preview enhancement to demonstrate value
            img = enhance_image_preview(img)

            # Keep preview reasonably sized for speed
            img.thumbnail((1400, 1400), Image.LANCZOS)

            out = BytesIO()
            img.save(out, format="JPEG", quality=88, optimize=True, progressive=True)
            out.seek(0)

            return StreamingResponse(
                out,
                media_type="image/jpeg",
                headers={"Cache-Control": "no-store"}
            )
    except Exception:
        raise HTTPException(status_code=400, detail="Preview failed (unsupported image).")


# =====================================================
# PROCESS ENDPOINT (SUBTLE EXPORT ENHANCEMENT, EXIF SAFE)
# =====================================================

@app.post("/process")
async def process_images(
    files: List[UploadFile] = File(...),
    platforms: Optional[List[str]] = Form(None),
    item_title: Optional[str] = Form(None),
):
    if not platforms:
        raise HTTPException(status_code=400, detail="No platform selected")

    title_slug = slugify_title(item_title or "Batch")

    # Clean temp folders safely
    if os.path.exists(UPLOAD_DIR):
        shutil.rmtree(UPLOAD_DIR)
    if os.path.exists(PROCESSED_DIR):
        shutil.rmtree(PROCESSED_DIR)

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(PROCESSED_DIR, exist_ok=True)

    # Save uploads
    for f in files:
        file_path = os.path.join(UPLOAD_DIR, f.filename)
        with open(file_path, "wb") as buffer:
            buffer.write(await f.read())

    # Process images (memory-safe)
    for platform in platforms:
        platform_folder = os.path.join(PROCESSED_DIR, platform)
        os.makedirs(platform_folder, exist_ok=True)

        for filename in os.listdir(UPLOAD_DIR):
            img_path = os.path.join(UPLOAD_DIR, filename)

            try:
                with Image.open(img_path) as img:
                    img = ImageOps.exif_transpose(img)  # auto-rotate iPhone photos
                    img = img.convert("RGB")

                    # Subtle export enhancement
                    img = enhance_image_export(img)
                    img = resize_for_platform(img, platform)

                    base_name, _ = os.path.splitext(filename)
                    out_name = f"{base_name}.jpg"
                    output_path = os.path.join(platform_folder, out_name)

                    img.save(
                        output_path,
                        format="JPEG",
                        quality=85,
                        optimize=True,
                        progressive=True
                    )
            except Exception as e:
                print(f"Error processing {filename}: {e}")

    # ZIP with dated parent folder including title
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
    download_name = f"PhotoBatcher_{title_slug}_{timestamp}.zip"

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{download_name}"'}
    )
