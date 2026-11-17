from datetime import datetime, timezone
import logging
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from starlette.datastructures import UploadFile

from .config import settings
from .db import get_connection
from .services.receipts import (
    OCRError,
    call_azure_ocr,
    normalize_transaction,
    receipt_classifier,
    redirect_receipt,
)


logger = logging.getLogger(__name__)
router = APIRouter()


def verify_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key missing",
        )

    try:
        expected_key = settings.require_api_key()
    except RuntimeError:
        logger.exception("API key is not configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Server authentication is not configured",
        ) from None

    if not secrets.compare_digest(x_api_key, expected_key):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )


@router.post("/", dependencies=[Depends(verify_api_key)])
def health_check():
    return {"message": "APP IS LIVE"}


@router.post(
    "/extract-receipt",
    dependencies=[Depends(verify_api_key)],
    openapi_extra={
        "requestBody": {
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["image"],
                        "properties": {"image": {"type": "string", "format": "binary"}},
                    }
                },
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
            }
        }
    },
)
async def extract_receipt(request: Request):
    content_type = request.headers.get("content-type", "").lower()
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("image") or form.get("file")
        if not isinstance(upload, UploadFile):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Multipart upload must include a file field named 'image' (or 'file')",
            )
        image = await upload.read()
    else:
        image = await request.body()

    if not image:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No image uploaded",
        )

    if len(image) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Image upload is too large",
        )
    try:
        azure_json = call_azure_ocr(image)
        # print(azure_json, "JSON")
        if not azure_json or "analyzeResult" not in azure_json:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Invalid OCR response",
            )

        receipt_type = receipt_classifier(azure_json)
        if receipt_type == "unknown":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid receipt",
            )

        parser = redirect_receipt(azure_json, receipt_type)
        result = parser.parse()
        normalized_tx = normalize_transaction(result)
        normalized_tx["created_at"] = datetime.now(timezone.utc)
        print(normalized_tx, 'FINAL DB INSERT')
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO transactions_live (
                        date_of_transaction, receiver_name, receiver_bank,
                        message, transaction_number, sent_from, utr,
                        receiver_phone_number, amount, upi_method, confidence, created_at
                    ) VALUES (
                        %(date_of_transaction)s, %(receiver_name)s, %(receiver_bank)s,
                        %(message)s, %(transaction_number)s, %(sent_from)s, %(utr)s,
                        %(receiver_phone_number)s, %(amount)s, %(upi_method)s, %(confidence)s, %(created_at)s
                    )
                    """,
                    normalized_tx,
                )

        return {
            "message": "Receipt data extracted and stored successfully",
            "data": normalized_tx,
            "confidence": result.get("confidence", 1),
        }
    except HTTPException:
        raise
    except OCRError as exc:
        logger.exception("Azure OCR request failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    except RuntimeError as exc:
        logger.exception("Server configuration error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception("Failed to extract receipt")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Receipt extraction failed",
        ) from exc
