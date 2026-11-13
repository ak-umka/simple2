from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import services
from app.config import settings
from app.db import get_db
from app.keitaro import KeitaroClient, KeitaroError, get_keitaro
from app.models import Stream

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Keitaro wrapper")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(services.ServiceError)
def _service_error(_: Request, exc: services.ServiceError):
    return JSONResponse(status_code=exc.status, content={"detail": exc.message})


@app.exception_handler(KeitaroError)
def _keitaro_error(_: Request, exc: KeitaroError):
    status = exc.status if 400 <= exc.status < 500 else 502
    return JSONResponse(status_code=status, content={"detail": exc.message})


# ---------------------------------------------------------------- схемы


class CampaignCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    geo: str = Field(..., min_length=2, max_length=2)
    offer_id: int
    domain_id: Optional[int] = None
    group_id: Optional[int] = None
    traffic_source_id: Optional[int] = None


class OfferAdd(BaseModel):
    offer_id: int


class PinIn(BaseModel):
    share: Optional[int] = None


class StreamOfferOut(BaseModel):
    id: int
    offer_id: int
    offer_name: str
    share: int
    pinned: bool
    status: str
    is_new: bool


class StreamOut(BaseModel):
    id: int
    keitaro_id: int
    name: str
    position: int
    schema_: str = Field(alias="schema")
    dirty: bool
    accepts_offers: bool
    offers: List[StreamOfferOut]

    model_config = {"populate_by_name": True}


class CampaignOut(BaseModel):
    id: int
    keitaro_id: int
    name: str
    alias: Optional[str]
    streams: List[StreamOut]


def stream_out(s: Stream) -> StreamOut:
    return StreamOut(
        id=s.id,
        keitaro_id=s.keitaro_id,
        name=s.name,
        position=s.position,
        schema=s.schema,
        dirty=s.dirty,
        accepts_offers=s.accepts_offers,
        offers=[StreamOfferOut.model_validate(o, from_attributes=True) for o in s.offers],
    )


def campaign_out(c) -> CampaignOut:
    return CampaignOut(
        id=c.id, keitaro_id=c.keitaro_id, name=c.name, alias=c.alias, streams=[stream_out(s) for s in c.streams]
    )


# ---------------------------------------------------------------- страницы


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


# ---------------------------------------------------------------- справочники


@app.get("/api/meta")
def meta(kt: KeitaroClient = Depends(get_keitaro)):
    return {
        "prefix": settings.campaign_prefix,
        "domains": [{"id": d["id"], "name": d.get("name", "")} for d in kt.domains()],
        "groups": [{"id": g["id"], "name": g.get("name", "")} for g in kt.campaign_groups()],
        "traffic_sources": [{"id": t["id"], "name": t.get("name", "")} for t in kt.traffic_sources()],
    }


@app.get("/api/offers")
def search_offers(q: str = "", limit: int = Query(20, le=100), kt: KeitaroClient = Depends(get_keitaro)):
    q = q.strip().lower()
    result = []
    for o in kt.offers():
        if not q or q == str(o["id"]) or q in o.get("name", "").lower():
            result.append({"id": o["id"], "name": o.get("name", ""), "state": o.get("state")})
        if len(result) >= limit:
            break
    return result


# ---------------------------------------------------------------- кампании


@app.get("/api/campaigns")
def list_campaigns(kt: KeitaroClient = Depends(get_keitaro)):
    return [
        {"keitaro_id": c["id"], "name": c.get("name", ""), "state": c.get("state")}
        for c in kt.campaigns()
    ]


@app.post("/api/campaigns", response_model=CampaignOut, status_code=201)
def create_campaign(
    body: CampaignCreate, db: Session = Depends(get_db), kt: KeitaroClient = Depends(get_keitaro)
):
    campaign = services.create_campaign(db, kt, **body.model_dump())
    db.commit()
    return campaign_out(campaign)


@app.get("/api/campaigns/{keitaro_id}", response_model=CampaignOut)
def get_campaign(keitaro_id: int, db: Session = Depends(get_db), kt: KeitaroClient = Depends(get_keitaro)):
    campaign = services.get_or_fetch_campaign(db, kt, keitaro_id)
    if not campaign.streams:
        services.sync_campaign(db, kt, campaign)
    db.commit()
    return campaign_out(campaign)


@app.post("/api/campaigns/{keitaro_id}/fetch", response_model=CampaignOut)
def fetch_streams(keitaro_id: int, db: Session = Depends(get_db), kt: KeitaroClient = Depends(get_keitaro)):
    campaign = services.get_or_fetch_campaign(db, kt, keitaro_id)
    services.sync_campaign(db, kt, campaign)
    db.commit()
    return campaign_out(campaign)


# ---------------------------------------------------------------- редактор потоков


@app.post("/api/streams/{stream_id}/offers", response_model=StreamOut)
def add_offer(
    stream_id: int, body: OfferAdd, db: Session = Depends(get_db), kt: KeitaroClient = Depends(get_keitaro)
):
    stream = services.add_offer(db, kt, stream_id, body.offer_id)
    db.commit()
    return stream_out(stream)


@app.post("/api/stream-offers/{so_id}/remove", response_model=StreamOut)
def remove_offer(so_id: int, db: Session = Depends(get_db)):
    stream = services.remove_offer(db, so_id)
    db.commit()
    return stream_out(stream)


@app.post("/api/stream-offers/{so_id}/bring-back", response_model=StreamOut)
def bring_back(so_id: int, db: Session = Depends(get_db)):
    stream = services.bring_back_offer(db, so_id)
    db.commit()
    return stream_out(stream)


@app.post("/api/stream-offers/{so_id}/pin", response_model=StreamOut)
def pin(so_id: int, body: PinIn, db: Session = Depends(get_db)):
    stream = services.pin_offer(db, so_id, body.share)
    db.commit()
    return stream_out(stream)


@app.post("/api/stream-offers/{so_id}/unpin", response_model=StreamOut)
def unpin(so_id: int, db: Session = Depends(get_db)):
    stream = services.unpin_offer(db, so_id)
    db.commit()
    return stream_out(stream)


@app.post("/api/streams/{stream_id}/push", response_model=StreamOut)
def push(stream_id: int, db: Session = Depends(get_db), kt: KeitaroClient = Depends(get_keitaro)):
    stream = services.push_stream(db, kt, stream_id)
    db.commit()
    return stream_out(stream)


@app.post("/api/streams/{stream_id}/cancel", response_model=StreamOut)
def cancel(stream_id: int, db: Session = Depends(get_db), kt: KeitaroClient = Depends(get_keitaro)):
    stream = services.cancel_stream(db, kt, stream_id)
    db.commit()
    return stream_out(stream)
