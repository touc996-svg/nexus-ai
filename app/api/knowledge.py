import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.services.rag import rag

router = APIRouter(prefix="/knowledge", tags=["Knowledge"])

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}
MAX_FILE_SIZE = 20 * 1024 * 1024


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=5000)
    user_id: str = Field(default="system", min_length=1, max_length=128)
    limit: int = Field(default=5, ge=1, le=50)


@router.post("/ingest")
async def ingest_document(
    file: UploadFile = File(...),
    user_id: str = Form(default="system")
):
    filename = Path(file.filename or "").name
    suffix = Path(filename).suffix.lower()

    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail="Supported formats: PDF, DOCX, TXT, MD"
        )

    if not user_id.strip() or len(user_id) > 128:
        raise HTTPException(status_code=400, detail="Invalid user_id")

    content = await file.read(MAX_FILE_SIZE + 1)

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File exceeds 20 MB limit")

    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            suffix=suffix,
            delete=False
        ) as temp:
            temp.write(content)
            temp_path = temp.name

        return rag.ingest_file(
            temp_path,
            user_id=user_id,
            metadata={"filename": filename}
        )

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Document ingestion failed: {type(exc).__name__}"
        ) from exc
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
        await file.close()


@router.post("/search")
def search_knowledge(request: KnowledgeSearchRequest):
    try:
        results = rag.search(
            query=request.query,
            user_id=request.user_id,
            limit=request.limit
        )
        return {
            "query": request.query,
            "results_count": len(results),
            "results": results
        }
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Knowledge search failed: {type(exc).__name__}"
        ) from exc
