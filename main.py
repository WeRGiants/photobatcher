from fastapi import FastAPI, UploadFile, File
from fastapi.responses import StreamingResponse
from PIL import Image, ImageEnhance
import io
import zipfile

app = FastAPI()


@app.get("/")
def read_root():
    return {"status": "PhotoBatcher API is running"}


# -----------------------------
# Image Enhancement
# -----------------------------

def enhance_image(image):
    # Subtle brightness boost
    brightness = ImageEnhance.Brightness(image)
    image = brightness.enhance(1.08)

    # Subtle contrast boost
    contrast = ImageEnhance.Contrast(image)
    image = contrast.enhance(1.08)

    # Slight sharpness boost
    sharpness = ImageEnhance.Sharpness(image)
    image = sharpness.enhance(1.05)

    return image


# -----------------------------
# Smart Square Crop
# -----------------------------

def crop_to_square(image):
    width, height = image.size
    min_dim = min(width, height)

    left = (width - min_dim) // 2
    top = (height - min_dim) // 2
    right = left + min_dim
    bottom = top + min_dim

    return image.crop((left, top, right, bottom))


# -----------------------------
# Platform Resize Logic
# -----------------------------

def resize_for_platform(image, platform):

    if platform == "ebay":
        # Keep proportions, max 1600px
        image.thumbnail((1600, 1600))

    elif platform == "poshmark":
        image = crop_to_square(image)
        image = image.resize((1080, 1080))

    elif platform == "mercari":
        image = crop_to_square(image)
        image = image.resize((1200, 1200))

    return image


# -----------------------------
# Main Processing Endpoint
# -----------------------------

@app.post("/process")
async def process_images(
    files: list[UploadFile] = File(...),
    platforms: str = "ebay"
):
    memory_zip = io.BytesIO()

    with zipfile.ZipFile(memory_zip, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:

        for file in files:
            contents = await file.read()

            for platform in platforms.split(","):

                image = Image.open(io.BytesIO(contents)).convert("RGB")

                # Apply enhancement
                image = enhance_image(image)

                # Resize per platform
                image = resize_for_platform(image, platform.strip())

                # Save processed image to memory
                img_buffer = io.BytesIO()
                image.save(img_buffer, format="JPEG", quality=92, optimize=True)
                img_buffer.seek(0)

                # Write to zip under platform folder
                zf.writestr(
                    f"{platform.strip()}/{file.filename}",
                    img_buffer.read()
                )

    memory_zip.seek(0)

    return StreamingResponse(
        memory_zip,
        media_type="application/zip",
        headers={
            "Content-Disposition": "attachment; filename=photobatcher_output.zip"
        }
    )
