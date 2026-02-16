import os
import shutil
import zipfile
from io import BytesIO
from typing import List

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, StreamingResponse
from PIL import Image, ImageEnhance

app = FastAPI()

UPLOAD_DIR = "temp_uploads"
PROCESSED_DIR = "temp_processed"

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(PROCESSED_DIR, exist_ok=True)


# -----------------------------
# IMAGE PROCESSING FUNCTION
# -----------------------------
def enhance_image(image: Image.Image) -> Image.Image:
    image = ImageEnhance.Brightness(image).enhance(1.05)
    image = ImageEnhance.Contrast(image).enhance(1.1)
    image = ImageEnhance.Sharpness(image).enhance(1.05)
    return image


def resize_for_platform(image: Image.Image, platform: str) -> Image.Image:
    if platform == "ebay":
        return image.resize((1600, 1600))
    if platform == "poshmark":
        return image.resize((1080, 1080))
    if platform == "mercari":
        return image.resize((1200, 1200))
    return image


# -----------------------------
# FRONTEND
# -----------------------------
@app.get("/", response_class=HTMLResponse)
async def home():
    return """
<!DOCTYPE html>
<html>
<head>
  <title>PhotoBatcher</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-50 min-h-screen flex items-center justify-center">
  <div class="bg-white p-8 rounded-xl shadow-lg w-full max-w-xl">
    <h1 class="text-2xl font-bold mb-6 text-center">PhotoBatcher</h1>

    <form id="uploadForm" class="space-y-4">
      <input 
        type="file" 
        name="files" 
        multiple 
        required 
        class="block w-full border p-2 rounded"
      />

      <div class="flex gap-4">
        <label class="flex items-center gap-2">
          <input type="checkbox" name="platforms" value="ebay">
          eBay
        </label>
        <label class="flex items-center gap-2">
          <input type="checkbox" name="platforms" value="poshmark">
          Poshmark
        </label>
        <label class="flex items-center gap-2">
          <input type="checkbox" name="platforms" value="mercari">
          Mercari
        </label>
      </div>

      <button 
        type="submit"
        class="w-full bg-black text-white py-2 rounded hover:bg-gray-800 transition"
      >
        Process Photos
      </button>
    </form>
  </div>

<script>
document.getElementById("uploadForm").addEventListener("submit", async function(e) {
    e.preventDefault();

    const formData = new FormData(this);

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
    a.download = "processed_images.zip";
    document.body.appendChild(a);
    a.click();
    a.remove();
});
</script>

</body>
</html>
"""


# -----------------------------
# PROCESS ENDPOINT
# -----------------------------
@app.post("/process")
async def process_images(
    files: List[UploadFile] = File(...),
    platforms: List[str] = Form(...)
):
    shutil.rmtree(UPLOAD_DIR)
    shutil.rmtree(PROCESSED_DIR)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(PROCESSED_DIR, exist_ok=True)

    # Save uploads
    for file in files:
        file_path = os.path.join(UPLOAD_DIR, file.filename)
        with open(file_path, "wb") as buffer:
            buffer.write(await file.read())

    # Process per platform
    for platform in platforms:
        platform_folder = os.path.join(PROCESSED_DIR, platform)
        os.makedirs(platform_folder, exist_ok=True)

        for filename in os.listdir(UPLOAD_DIR):
            img_path = os.path.join(UPLOAD_DIR, filename)
            img = Image.open(img_path).convert("RGB")

            img = enhance_image(img)
            img = resize_for_platform(img, platform)

            output_path = os.path.join(platform_folder, filename)
            img.save(output_path, quality=95)

    # Create ZIP in memory
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for root, _, files in os.walk(PROCESSED_DIR):
            for file in files:
                full_path = os.path.join(root, file)
                relative_path = os.path.relpath(full_path, PROCESSED_DIR)
                zip_file.write(full_path, relative_path)

    zip_buffer.seek(0)

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=processed_images.zip"}
    )
