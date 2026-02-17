import os
import re
import shutil
import zipfile
import datetime
from io import BytesIO
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from PIL import Image, ImageEnhance, ImageOps, ImageChops
import numpy as np

# Database
from sqlalchemy import create_engine, Column, Integer, String, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Stripe
import stripe

Image.MAX_IMAGE_PIXELS = 50_000_000

app = FastAPI()

# =====================================================
# ENV CONFIG
# =====================================================

DATABASE_URL = os.getenv("DATABASE_URL")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL not set")

if not STRIPE_SECRET_KEY:
    raise ValueError("STRIPE_SECRET_KEY not set")

if not STRIPE_PRICE_ID:
    raise ValueError("STRIPE_PRICE_ID not set")

stripe.api_key = STRIPE_SECRET_KEY

# =====================================================
# DATABASE SETUP
# =====================================================

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    subscription_active = Column(Boolean, default=False)
    stripe_customer_id = Column(String, nullable=True)
    stripe_subscription_id = Column(String, nullable=True)

Base.metadata.create_all(bind=engine)

# =====================================================
# APP CONFIG
# =====================================================

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
# IMAGE PIPELINE
# =====================================================

def level_background(img):
    arr = np.array(img).astype(np.float32)
    hsv = Image.fromarray(arr.astype(np.uint8)).convert("HSV")
    hsv_arr = np.array(hsv).astype(np.float32)

    _, s, v = hsv_arr[:, :, 0], hsv_arr[:, :, 1], hsv_arr[:, :, 2]
    mask = (v > 200) & (s < 60)
    v[mask] = np.clip(v[mask] * 1.08 + 10, 0, 255)

    hsv_arr[:, :, 2] = v
    return Image.fromarray(hsv_arr.astype(np.uint8), "HSV").convert("RGB")

def smart_crop(img, padding_ratio=0.06):
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

def to_square(img):
    side = max(img.size)
    canvas = Image.new("RGB", (side, side), (255, 255, 255))
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    return canvas

def enhance(img):
    img = level_background(img)
    img = ImageEnhance.Brightness(img).enhance(1.05)
    img = ImageEnhance.Contrast(img).enhance(1.12)
    img = ImageEnhance.Color(img).enhance(1.04)
    img = ImageEnhance.Sharpness(img).enhance(1.05)
    return img

def resize_platform(img, platform):
    sizes = {"ebay": 1600, "poshmark": 1080, "mercari": 1200}
    return img.resize((sizes[platform], sizes[platform]), Image.LANCZOS)

# =====================================================
# ROUTES
# =====================================================

@app.get("/")
async def home():
    return {"status": "PhotoBatcher SaaS Running"}

# ========================
# STRIPE CHECKOUT
# ========================

@app.post("/create-checkout-session")
async def create_checkout_session(email: str = Form(...)):
    db = SessionLocal()
    user = db.query(User).filter(User.email == email).first()

    if not user:
        raise HTTPException(404, "User not found")

    if not user.stripe_customer_id:
        customer = stripe.Customer.create(email=email)
        user.stripe_customer_id = customer.id
        db.commit()

    session = stripe.checkout.Session.create(
        customer=user.stripe_customer_id,
        payment_method_types=["card"],
        mode="subscription",
        line_items=[{
            "price": STRIPE_PRICE_ID,
            "quantity": 1,
        }],
        success_url="https://photobatcher.com/success",
        cancel_url="https://photobatcher.com/cancel",
    )

    return {"checkout_url": session.url}

# ========================
# STRIPE WEBHOOK
# ========================

@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except Exception:
        raise HTTPException(400, "Webhook verification failed")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        customer_id = session.get("customer")
        subscription_id = session.get("subscription")

        db = SessionLocal()
        user = db.query(User).filter(User.stripe_customer_id == customer_id).first()

        if user:
            user.subscription_active = True
            user.stripe_subscription_id = subscription_id
            db.commit()

    return {"status": "success"}

# ========================
# PROCESS (LOCKED)
# ========================

@app.post("/process")
async def process(
    email: str = Form(...),
    files: List[UploadFile] = File(...),
    platforms: Optional[List[str]] = Form(None),
    item_title: Optional[str] = Form(None),
):
    db = SessionLocal()
    user = db.query(User).filter(User.email == email).first()

    if not user or not user.subscription_active:
        raise HTTPException(403, "Active subscription required")

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
