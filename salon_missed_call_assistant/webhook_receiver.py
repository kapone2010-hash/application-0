from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

import app as salon_app


api = FastAPI(title="Salon Missed-Call Webhook Receiver")


async def request_payload(request: Request) -> dict[str, object]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        return dict(await request.json())
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        return dict(form)
    try:
        return dict(await request.json())
    except Exception:
        form = await request.form()
        return dict(form)


def twiml_empty_response() -> Response:
    return Response("<Response></Response>", media_type="application/xml")


@api.get("/health")
def health() -> dict[str, str]:
    salon_app.init_db()
    return {"status": "ok"}


@api.post("/webhooks/missed-call")
async def missed_call(request: Request, x_salon_signature: str = Header(default="")) -> JSONResponse:
    salon_app.init_db()
    try:
        payload = await request_payload(request)
        conversation_id = salon_app.process_missed_call_webhook(payload, x_salon_signature)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"status": "processed", "conversation_id": conversation_id})


@api.post("/webhooks/inbound-sms")
async def inbound_sms(request: Request, x_salon_signature: str = Header(default="")) -> Response:
    salon_app.init_db()
    try:
        payload = await request_payload(request)
        salon_app.process_inbound_sms_webhook(payload, x_salon_signature)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return twiml_empty_response()
