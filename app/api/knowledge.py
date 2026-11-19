import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.security.identity import SYSTEM_USER, Principal, get_principal, resolve_user_id
from app.services.rag import rag

router = APIRouter(prefix="/knowledge", tags=["Knowledge"])

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}
MAX_FILE_SIZE = 20 * 1024 * 1024
MAX_SOURCE_NAME_LENGTH = 200


def safe_source_name(filename: str, suffix: str) -> str:
    """The name a document is stored and cited under: the uploaded file's own name, made safe.

    Only the base name is kept; control characters and the bracket, newline and pipe characters that the
    reference header in prompts is built from are replaced, and the length is capped. The result keeps the
    checked suffix. The same file name therefore maps to the same source id, so re-uploading a document
    replaces its chunks instead of adding a second copy.
    """
    stem = Path(filename).stem
    cleaned = "".join("_" if (ord(c) < 33 or c in "[]|\\/:") else c for c in stem).strip("_ .")
    cleaned = cleaned[:MAX_SOURCE_NAME_LENGTH - len(suffix)] or "document"
    return cleaned + suffix


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=5000)
    user_id: str | None = Field(default=None, min_length=1, max_length=128)
    limit: int = Field(default=5, ge=1, le=50)


@router.post("/ingest")
async def ingest_document(
    file: UploadFile = File(...),
    user_id: str | None = Form(default=None),
    principal: Principal = Depends(get_principal),
):
    user_id = resolve_user_id(principal, user_id, default=SYSTEM_USER)
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

    try:
        # The file is written under its own (sanitized) name inside a private temporary directory, so the
        # document is stored and cited under that name rather than a random temporary one.
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = os.path.join(temp_dir, safe_source_name(filename, suffix))
            Path(temp_path).write_bytes(content)
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
        await file.close()


@router.get("/sources")
def list_knowledge_sources(user_id: str | None = None, principal: Principal = Depends(get_principal)):
    user_id = resolve_user_id(principal, user_id, default=SYSTEM_USER)
    try:
        return {"user_id": user_id, "sources": rag.list_sources(user_id=user_id)}
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Knowledge source listing failed: {type(exc).__name__}"
        ) from exc


@router.delete("/sources/{source_id}")
def delete_knowledge_source(
    source_id: str,
    user_id: str | None = None,
    principal: Principal = Depends(get_principal),
):
    user_id = resolve_user_id(principal, user_id, default=SYSTEM_USER)
    try:
        deleted = rag.delete_source(source_id=source_id, user_id=user_id)
        return {
            "source_id": source_id,
            "deleted": deleted,
            "status": "deleted" if deleted else "not_found_or_disabled"
        }
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Knowledge source deletion failed: {type(exc).__name__}"
        ) from exc


@router.post("/search")
def search_knowledge(request: KnowledgeSearchRequest, principal: Principal = Depends(get_principal)):
    user_id = resolve_user_id(principal, request.user_id, default=SYSTEM_USER)
    try:
        results = rag.search(
            query=request.query,
            user_id=user_id,
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
