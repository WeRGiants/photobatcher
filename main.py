import os
import re
import shutil
import zipfile
import datetime
import hashlib
from io import BytesIO
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Response, Depends
from fastapi.responses import StreamingResponse
from jose import jwt, JWTError
from passlib.context import CryptContext
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

# Bump this anytime you deploy changes (helps confirm Render is running latest code)
VERSION = "auth-sha256-bcrypt-v1"

# =====================================================
# ENV CONFIG
# =====================================================

DATABASE_URL = os.getenv("DATABASE_URL")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID_MONTHLY = os.getenv("STRIPE_PRICE_ID_MONTHLY")
STRIPE_PRICE_ID_ANNUAL = os.getenv("STRIPE_PRICE_ID_ANNUAL")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
JWT_SECRET = os.getenv("JWT_SECRET")
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "10080"))

required_envs = [
    DATABASE_URL,
    STRIPE_SECRET_KEY,
    STRIPE_PRICE_ID_MONTHLY,
    STRIPE_PRICE_ID_ANNUAL,
    STRIPE_WEBHOOK_SECRET,
    JWT_SECRET
]

if not all(required_envs):
    raise ValueError("One or more required environment variables are missing")

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
# AUTH SETUP
# =====================================================

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
JWT_ALG = "HS256"
COOKIE_NAME = "pb_token"

# 🔥 FIXED PASSWORD HASHING (NO 72 BYTE LIMIT)
def hash_password(password: str) -> str:
    sha = hashlib.sha256(password.encode()).hexdigest()
    return pwd_context.hash(sha)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    sha = hashlib.sha256(plain_password.encode()).hexdigest()
    return pwd_context.verify(sha, hashed_password)

def create_token(user_id: int):
    exp = datetime.datetime.utcnow() + datetime.timedelta(minutes=JWT_EXPIRE_MINUTES)
    payload = {"sub": str(user_id), "exp": exp}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)

def get_current_user(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(401, "Not logged in")

    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
        user_id = int(payload.get("sub"))
    except (JWTError, ValueError):
        raise HTTPException(401, "Invalid session")

    db = SessionLocal()
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(401, "User not found")

    return user

# =====================================================
# ROUTES
# =====================================================

@app.get("/")
async def home():
    return {"status": "PhotoBatcher SaaS Running", "version": VERSION}

@app.get("/version")
async def version():
    return {"version": VERSION}

# ========================
# AUTH ROUTES
# ========================

@app.post("/auth/register")
async def register(email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()

    if len(password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")

    db = SessionLocal()
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise HTTPException(409, "Email already registered")

    user = User(
        email=email,
        password_hash=hash_password(password),
        subscription_active=False
    )

    db.add(user)
    db.commit()
    db.refresh(user)

    return {"status": "registered"}

@app.post("/auth/login")
async def login(response: Response, email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()

    db = SessionLocal()
    user = db.query(User).filter(User.email == email).first()

    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(401, "Invalid credentials")

    token = create_token(user.id)

    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=JWT_EXPIRE_MINUTES * 60,
    )

    return {"status": "logged_in", "subscription_active": user.subscription_active}

@app.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"status": "logged_out"}

@app.get("/me")
async def me(user: User = Depends(get_current_user)):
    return {"email": user.email, "subscription_active": user.subscription_active}

# ========================
# STRIPE CHECKOUT
# ========================

@app.post("/create-checkout-session")
async def create_checkout_session(request: Request, billing_cycle: str = Form("monthly")):
    user = get_current_user(request)
    db = SessionLocal()
    user = db.query(User).filter(User.id == user.id).first()

    if not user.stripe_customer_id:
        customer = stripe.Customer.create(email=user.email)
        user.stripe_customer_id = customer.id
        db.commit()

    selected_price = STRIPE_PRICE_ID_ANNUAL if billing_cycle == "annual" else STRIPE_PRICE_ID_MONTHLY

    session = stripe.checkout.Session.create(
        customer=user.stripe_customer_id,
        payment_method_types=["card"],
        mode="subscription",
        line_items=[{"price": selected_price, "quantity": 1}],
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
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
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
    request: Request,
    files: List[UploadFile] = File(...),
    platforms: Optional[List[str]] = Form(None),
    item_title: Optional[str] = Form(None),
):
    user = get_current_user(request)

    if not user.subscription_active:
        raise HTTPException(403, "Active subscription required")

    if not platforms:
        raise HTTPException(400, "Select at least one platform")

    if len(files) > 24:
        raise HTTPException(400, "Maximum 24 images")

    title = (item_title or "Batch").replace(" ", "_")
    date = datetime.datetime.now().strftime("%Y-%m-%d")

    UPLOAD_DIR = f"temp/{user.id}/uploads"
    PROCESSED_DIR = f"temp/{user.id}/processed"

    if os.path.exists(f"temp/{user.id}"):
        shutil.rmtree(f"temp/{user.id}")

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(PROCESSED_DIR, exist_ok=True)

    sizes = {"ebay": 1600, "poshmark": 1080, "mercari": 1200}

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
                img = img.resize((sizes[platform], sizes[platform]), Image.LANCZOS)

                base, _ = os.path.splitext(name)
                img.save(os.path.join(folder, base + ".jpg"), "JPEG", quality=85)

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
