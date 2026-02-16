from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse
from PIL import Image, ImageEnhance
import io

app = FastAPI()

@app.get("/")
def read_root():
    return {"status": "PhotoBatcher API is running"}

@app.post("/process")
async def process_images(files: list[UploadFile] = File(...)):
    processed_images = []

    for file in files:
        contents = await file.read()
        image = Image.open(io.BytesIO(contents)).convert("RGB")

        # Smart Enhancement
        enhancer = ImageEnhance.Brightness(image)
        image = enhancer.enhance(1.1)

        enhancer = ImageEnhance.Contrast(image)
        image = enhancer.enhance(1.1)

        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=90)
        buffer.seek(0)

        processed_images.append({
            "filename": file.filename,
            "status": "processed"
        })

    return JSONResponse(content={
        "message": "Images processed successfully",
        "files": processed_images
    })
