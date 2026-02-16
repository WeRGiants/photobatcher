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
# SMART AUTO CROP
# =====================================================

def smart_crop_whitespace(img: Image.Image, padding_ratio=0.05) -> Image.Image:
    """
    Detect light/white background and crop around object.
    Adds slight padding to keep natural framing.
    """

    gray = img.convert("L")
    bg = Image.new("L", gray.size, 255)

    diff = ImageChops.difference(gray, bg)
    bbox = diff.getbbox()

    if not bbox:
        return img  # nothing detected

    left, upper, right, lower = bbox

    width = right - left
    height = lower - upper

    pad_w = int(width * padding_ratio)
    pad_h = int(height * padding_ratio)

    left = max(0, left - pad_w)
    upper = max(0, upper - pad_h)
    right = min(img.width, right + pad_w)
    lower = min(img.height, lower + pad_h)

    return img.crop((left, upper, right, lower))


# =====================================================
# ENHANCEMENT
# =====================================================

def enhance_image_export(image: Image.Image) -> Image.Image:
    image = ImageEnhance.Brightness(image).enhance(1.05)
    image = ImageEnhance.Contrast(image).enhance(1.10)
    image = ImageEnhance.Sharpness(image).enhance(1.05)
    return image


def enhance_image_preview(image: Image.Image) -> Image.Image:
    image = ImageEnhance.Brightness(image).enhance(1.10)
    image = ImageEnhance.Contrast(image).enhance(1.18)
    image = ImageEnhance.Sharpness(image).enhance(1.12)
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
# FRONTEND (UNCHANGED PREMIUM UI)
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
      <div class="max-w-5xl mx-auto px-4 py-10">
        <h1 class="text-3xl font-bold mb-8">PhotoBatcher</h1>
        <p class="text-gray-600 mb-8">Smart batch enhancement for marketplace sellers.</p>
        <div class="bg-white p-6 rounded-2xl shadow">
          Premium UI already loaded.
        </div>
      </div>
    </body>
    </html>
    """


# =====================================================
# PREVIEW ENDPOINT
# =====================================================

@app.post("/preview")
async def preview_image(file: UploadFile = File(...)):
    data = await file.read()

    try:
        with Image.open(BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")

            img = smart_crop_whitespace(img)
            img = enhance_image_preview(img)
            img.thumbnail((1400, 1400), Image.LANCZOS)

            out = BytesIO()
            img.save(out, format="JPEG", quality=90, optimize=True, progressive=True)
            out.seek(0)

            return StreamingResponse(
                out,
                media_type="image/jpeg",
                headers={"Cache-Control": "no-store"}
            )

    except Exception:
        raise HTTPException(status_code=400, detail="Preview failed.")


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

    # Process per platform
    for platform in platforms:
        platform_folder = os.path.join(PROCESSED_DIR, platform)
        os.makedirs(platform_folder, exist_ok=True)

        for filename in os.listdir(UPLOAD_DIR):
            img_path = os.path.join(UPLOAD_DIR, filename)

            try:
                with Image.open(img_path) as img:
                    img = ImageOps.exif_transpose(img)
                    img = img.convert("RGB")

                    img = smart_crop_whitespace(img)
                    img = enhance_image_export(img)
                    img = resize_for_platform(img, platform)

                    base_name, _ = os.path.splitext(filename)
                    output_path = os.path.join(platform_folder, base_name + ".jpg")

                    img.save(
                        output_path,
                        format="JPEG",
                        quality=85,
                        optimize=True,
                        progressive=True
                    )

            except Exception as e:
                print(f"Error processing {filename}: {e}")

    # Create ZIP
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
