import base64
import io
import json
import os
import re

OpenAI = None
try:
    from openai import OpenAI  # type: ignore
except Exception:
    OpenAI = None

genai = None
try:
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        import google.generativeai as genai  # type: ignore
except Exception:
    genai = None

EXTRACTION_SYSTEM = """You read timetable grids from images (and screenshots of PDF pages). Output ONLY valid JSON, no markdown fences.
Schema: {"entries":[{"class_name":"string","day":"Monday|Tuesday|Wednesday|Thursday|Friday","slot_index":0-5,"subject":"string","faculty":"string","room":"string","batch_name":"","is_lab":false}]}
Rules:
- slot_index: 0=09:00-10:00, 1=10:00-11:00, 2=11:00-12:00, 3=13:00-14:00, 4=14:00-15:00, 5=15:00-16:00. If the image uses different times, map to the closest slot_index.
- One entry per occupied hour cell. Multi-hour labs: one entry per hour (same subject/faculty/room).
- class_name: use labels printed on the table (e.g. Y1-A, Division A). If the sheet has no class column, use empty string "".
- faculty: empty string if not visible.
- batch_name: only if clearly a batch split; else "".
- is_lab: true only if marked lab/practical/workshop.
Do not include any text outside the JSON object."""


def generate_response(prompt):
    openai_key = os.getenv("OPENAI_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY")

    if openai_key and OpenAI is not None:
        try:
            client = OpenAI(api_key=openai_key)
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a helpful academic assistant."},
                    {"role": "user", "content": prompt},
                ],
            )
            return response.choices[0].message.content
        except Exception as exc:
            print("OpenAI Error:", exc)

    if gemini_key and genai is not None:
        try:
            genai.configure(api_key=gemini_key)
            model = genai.GenerativeModel("gemini-3-flash-preview")
            res = model.generate_content(prompt)
            return res.text
        except Exception as exc:
            print("Gemini Error:", exc)

    return "LLM not configured."


def _strip_json_object(text):
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*```$", "", t)
    m = re.search(r"\{[\s\S]*\}", t)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _mime_from_upload(filename, content_type):
    name = (filename or "").lower()
    ctype = (content_type or "").lower()
    if "jpeg" in ctype or name.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if "webp" in ctype or name.endswith(".webp"):
        return "image/webp"
    if "gif" in ctype or name.endswith(".gif"):
        return "image/gif"
    return "image/png"


def extract_timetable_openai_vision(image_chunks):
    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        return None, "OPENAI_API_KEY not set (required for image/PDF vision extraction with OpenAI)."
    if OpenAI is None:
        return None, "openai package is not installed."

    client = OpenAI(api_key=openai_key)
    content = [{"type": "text", "text": EXTRACTION_SYSTEM + "\nExtract all timetable cells from the image(s)."}]
    for raw, mime in image_chunks:
        b64 = base64.standard_b64encode(raw).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You output only valid JSON objects."},
                {"role": "user", "content": content},
            ],
            max_tokens=4096,
        )
        raw = response.choices[0].message.content
    except Exception as exc:
        return None, f"OpenAI vision error: {exc}"

    data = _strip_json_object(raw)
    if not data or "entries" not in data:
        return None, "Could not parse JSON from model response."
    return data, None


def extract_timetable_gemini_vision(image_chunks):
    gemini_key = os.getenv("GEMINI_API_KEY")
    if not gemini_key:
        return None, "GEMINI_API_KEY not set."
    if genai is None:
        return None, "google-generativeai package is not installed."

    try:
        from PIL import Image
    except ImportError:
        return None, "Pillow is required for Gemini image extraction. pip install Pillow"

    genai.configure(api_key=gemini_key)
    model = genai.GenerativeModel("gemini-3-flash-preview")
    parts = [EXTRACTION_SYSTEM + "\nExtract all timetable cells from the image(s). Output ONLY the JSON object."]
    for raw, _mime in image_chunks:
        parts.append(Image.open(io.BytesIO(raw)))

    try:
        res = model.generate_content(parts)
        raw = res.text
    except Exception as exc:
        return None, f"Gemini vision error: {exc}"

    data = _strip_json_object(raw)
    if not data or "entries" not in data:
        return None, "Could not parse JSON from Gemini response."
    return data, None


def pdf_to_png_pages(file_bytes, max_pages=4, zoom=2.0):
    try:
        import fitz
    except ImportError:
        return None, "PyMuPDF (pymupdf) is required for PDF import. pip install pymupdf"

    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
    except Exception as exc:
        return None, f"Invalid PDF: {exc}"

    out = []
    n = min(len(doc), max_pages)
    mat = fitz.Matrix(zoom, zoom)
    for i in range(n):
        page = doc.load_page(i)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        out.append(pix.tobytes("png"))
    doc.close()
    if not out:
        return None, "PDF has no pages."
    return out, None


def extract_timetable_from_upload(file_bytes, filename, content_type):
    name = (filename or "").lower()
    ctype = (content_type or "").lower()

    if "pdf" in ctype or name.endswith(".pdf"):
        png_list, err = pdf_to_png_pages(file_bytes)
        if err:
            return None, err
        image_chunks = [(p, "image/png") for p in png_list]
    elif ctype.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
        image_chunks = [(file_bytes, _mime_from_upload(filename, content_type))]
    else:
        return None, "Unsupported file type. Use PDF, PNG, JPG, or WEBP."

    if os.getenv("OPENAI_API_KEY"):
        return extract_timetable_openai_vision(image_chunks)
    if os.getenv("GEMINI_API_KEY"):
        return extract_timetable_gemini_vision(image_chunks)
    return None, "No API key configured. Set OPENAI_API_KEY or GEMINI_API_KEY."
