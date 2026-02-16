from fastapi import FastAPI, UploadFile, File
from fastapi.responses import StreamingResponse
from PIL import Image, ImageEnhance
import io
import zipfile

app = FastAPI()

@app.get("/")
def read_root():
    return {"status": "PhotoBatcher API is running"}


def enhance_image(image):
    enhancer = ImageEnhance.Brightness(image)
    image = enhancer.enhance(1.1)

    enhancer = ImageEnhance.Contrast(image)
    image = enhancer.enhance(1.1)

    return image


def resize_for_platform(image, platform):
    if platform == "ebay":
        image.thumbnail((1600, 1600))
    elif platform == "poshmark":
        image = image.resize((1080, 1080))
    elif platform == "mercari":
        image = image.resize((1200, 1200))
    return image


@app.post("/process")
async def process_images(
    files: list[UploadFile] = File(...),
    platforms: str = "ebay"
):
    memory_zip = io.BytesIO()
    with zipfile.ZipFile(memory_zip, mode="w") as zf:

        for platform in platforms.split(","):

            for file in files:
                contents = await file.read()
                image = Image.open(io.BytesIO(contents)).convert("RGB")

                image = enhance_image(image)
                image = resize_for_platform(image, platform)

                img_buffer = io.BytesIO()
                image.save(img_buffer, format="JPEG", quality=90)
                img_buffer.seek(0)

                zf.writestr(
                    f"{platform}/{file.filename}",
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
