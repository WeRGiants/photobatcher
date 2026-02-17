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

# NEW: Database imports
from sqlalchemy import create_engine, Column, Integer, String, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

Image.MAX_IMAGE_PIXELS = 50_000_000

app = FastAPI()

# =====================================================
# DATABASE SETUP
# =====================================================

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable not set")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# =====================================================
# USER MODEL
# =====================================================

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    subscription_tier = Column(String, default="starter")  # starter or pro
    is_active = Column(Boolean, default=True)

# Create tables automatically
Base.metadata.create_all(bind=engine)

# =====================================================
# APP CONFIG
# =====================================================

UPLOAD_DIR = "temp_uploads"
PROCESSED_DIR = "temp_processed"
MAX_IMAGES = 24

# =====================================================
# Utilities
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
# Enhancement Pipeline
# =====================================================

def level_background(img: Image.Image) -> Image.Image:
    arr = np.array(img).astype(np.float32)
    hsv = Image.fromarray(arr.astype(np.uint8)).convert("HSV")
    hsv_arr = np.array(hsv).astype(np.float32)

    _, s, v = hsv_arr[:, :, 0], hsv_arr[:, :, 1], hsv_arr[:, :, 2]
    mask = (v > 200) & (s < 60)
    v[mask] = np.clip(v[mask] * 1.08 + 10, 0, 255)

    hsv_arr[:, :, 2] = v
    return Image.fromarray(hsv_arr.astype(np.uint8), "HSV").convert("RGB")

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
    side = max(img.size)
    canvas = Image.new("RGB", (side, side), (255, 255, 255))
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    return canvas

def enhance(img: Image.Image) -> Image.Image:
    img = level_background(img)
    img = ImageEnhance.Brightness(img).enhance(1.05)
    img = ImageEnhance.Contrast(img).enhance(1.12)
    img = ImageEnhance.Color(img).enhance(1.04)
    img = ImageEnhance.Sharpness(img).enhance(1.05)
    return img

def resize_platform(img: Image.Image, platform: str) -> Image.Image:
    sizes = {"ebay": 1600, "poshmark": 1080, "mercari": 1200}
    return img.resize((sizes[platform], sizes[platform]), Image.LANCZOS)

# =====================================================
# ROUTES
# =====================================================

@app.get("/", response_class=HTMLResponse)
async def home():
    return "<h1>PhotoBatcher SaaS Backend Running</h1>"

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
        headers={"Content-Disposition": f'attachment; filename=\"{parent}.zip\"'},
    )
