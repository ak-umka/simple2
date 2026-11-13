"""Бизнес-логика: создание кампаний и редактор офферов в потоках."""
import re
import secrets
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.keitaro import KeitaroClient
from app.models import OFFER_ACTIVE, OFFER_REMOVED, Campaign, Stream, StreamOffer
from app.weights import WeightError, distribute

GOOGLE_URL = "https://google.com"


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------- создание


def full_campaign_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise ServiceError("Название кампании не может быть пустым")
    prefix = settings.campaign_prefix
    return name if name.startswith(prefix) else prefix + name


def make_alias(full_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", full_name.lower()).strip("-") or "campaign"
    return f"{slug[:40]}-{secrets.token_hex(3)}"


def normalize_geo(geo: str) -> str:
    geo = geo.strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", geo):
        raise ServiceError("Гео должно быть двухбуквенным кодом страны (ISO 3166-1 alpha-2), например AU")
    return geo


def create_campaign(
    db: Session,
    kt: KeitaroClient,
    name: str,
    geo: str,
    offer_id: int,
    domain_id: Optional[int] = None,
    group_id: Optional[int] = None,
    traffic_source_id: Optional[int] = None,
) -> Campaign:
    full_name = full_campaign_name(name)
    geo = normalize_geo(geo)
    if offer_id not in kt.offer_names():
        raise ServiceError(f"Оффер {offer_id} не найден в Keitaro", 404)

    payload: Dict[str, Any] = {
        "name": full_name,
        "alias": make_alias(full_name),
        "type": "position",
        "state": "active",
        "cost_type": "CPC",
    }
    if domain_id:
        payload["domain_id"] = domain_id
    if group_id:
        payload["group_id"] = group_id
    if traffic_source_id:
        payload["traffic_source_id"] = traffic_source_id

    kc = kt.create_campaign(payload)
    kt.create_stream(
        {
            "campaign_id": kc["id"],
            "type": "regular",
            "name": f"{geo} -> Google",
            "position": 1,
            "state": "active",
            "schema": "redirect",
            "action_type": "http",
            "action_payload": GOOGLE_URL,
            "filters": [{"name": "country", "mode": "accept", "payload": [geo]}],
        }
    )
    kt.create_stream(
        {
            "campaign_id": kc["id"],
            "type": "regular",
            "name": "Offers",
            "position": 2,
            "state": "active",
            "schema": "landings",
            "action_type": "http",
            "offers": [{"offer_id": offer_id, "share": 100, "state": "active"}],
        }
    )

    campaign = _upsert_campaign(db, kc)
    campaign.created_via_app = True
    sync_campaign(db, kt, campaign)
    return campaign


# ---------------------------------------------------------------- синхронизация


def _upsert_campaign(db: Session, kc: Dict[str, Any]) -> Campaign:
    campaign = db.scalar(select(Campaign).where(Campaign.keitaro_id == kc["id"]))
    if campaign is None:
        campaign = Campaign(keitaro_id=kc["id"], name=kc.get("name", ""))
        db.add(campaign)
    campaign.name = kc.get("name", campaign.name)
    campaign.alias = kc.get("alias")
    db.flush()
    return campaign


def get_or_fetch_campaign(db: Session, kt: KeitaroClient, keitaro_id: int) -> Campaign:
    campaign = db.scalar(select(Campaign).where(Campaign.keitaro_id == keitaro_id))
    if campaign is None:
        campaign = _upsert_campaign(db, kt.campaign(keitaro_id))
    return campaign


def sync_campaign(db: Session, kt: KeitaroClient, campaign: Campaign) -> Campaign:
    """«Fetch streams»: подтягивает потоки и офферы из Keitaro.

    Неопубликованные локальные изменения при этом сбрасываются.
    Офферы, о которых обёртка знала, но которых нет в Keitaro, попадают в архив (removed).
    """
    remote_streams = kt.streams(campaign.keitaro_id)
    names = kt.offer_names()
    remote_ids = {s["id"] for s in remote_streams}

    for local in list(campaign.streams):
        if local.keitaro_id not in remote_ids:
            campaign.streams.remove(local)

    by_kid = {s.keitaro_id: s for s in campaign.streams}
    for rs in remote_streams:
        stream = by_kid.get(rs["id"])
        if stream is None:
            stream = Stream(keitaro_id=rs["id"])
            campaign.streams.append(stream)
        _apply_remote_stream(stream, rs, names)

    db.flush()
    db.refresh(campaign)
    return campaign


def _apply_remote_stream(stream: Stream, rs: Dict[str, Any], names: Dict[int, str]) -> None:
    stream.name = rs.get("name") or ""
    stream.position = rs.get("position") or 0
    stream.schema = rs.get("schema") or ""

    remote_offers = {o["offer_id"]: o for o in rs.get("offers") or [] if o.get("state", "active") == "active"}

    for so in list(stream.offers):
        if so.is_new:
            stream.offers.remove(so)

    local = {so.offer_id: so for so in stream.offers}
    for offer_id, ro in remote_offers.items():
        so = local.get(offer_id)
        if so is None:
            so = StreamOffer(offer_id=offer_id, pinned=False)
            stream.offers.append(so)
        so.status = OFFER_ACTIVE
        so.share = int(ro.get("share") or 0)
        so.is_new = False
        so.offer_name = names.get(offer_id, so.offer_name or "")

    for offer_id, so in local.items():
        if offer_id not in remote_offers:
            so.status = OFFER_REMOVED
            so.share = 0
            so.pinned = False

    stream.dirty = False


# ---------------------------------------------------------------- редактор


def _get_stream(db: Session, stream_id: int) -> Stream:
    stream = db.get(Stream, stream_id)
    if stream is None:
        raise ServiceError("Поток не найден", 404)
    return stream


def _get_stream_offer(db: Session, stream_offer_id: int) -> StreamOffer:
    so = db.get(StreamOffer, stream_offer_id)
    if so is None:
        raise ServiceError("Оффер в потоке не найден", 404)
    return so


def rebalance(stream: Stream) -> None:
    active = [so for so in stream.offers if so.status == OFFER_ACTIVE]
    pinned = [so for so in active if so.pinned]
    unpinned = [so for so in active if not so.pinned]
    try:
        shares = distribute([so.share for so in pinned], len(unpinned))
    except WeightError as exc:
        raise ServiceError(str(exc)) from exc
    for so, share in zip(unpinned, shares):
        so.share = share
    for so in stream.offers:
        if so.status == OFFER_REMOVED:
            so.share = 0


def _ensure_offer_stream(stream: Stream) -> None:
    if not stream.accepts_offers:
        raise ServiceError("Этот поток работает как редирект, офферы в него добавить нельзя")


def add_offer(db: Session, kt: KeitaroClient, stream_id: int, offer_id: int) -> Stream:
    stream = _get_stream(db, stream_id)
    _ensure_offer_stream(stream)
    names = kt.offer_names()
    if offer_id not in names:
        raise ServiceError(f"Оффер {offer_id} не найден в Keitaro", 404)

    existing = next((so for so in stream.offers if so.offer_id == offer_id), None)
    if existing is not None:
        if existing.status == OFFER_ACTIVE:
            raise ServiceError("Этот оффер уже есть в потоке")
        existing.status = OFFER_ACTIVE  # добавление архивного оффера = bring back
    else:
        stream.offers.append(StreamOffer(
                offer_id=offer_id,
                offer_name=names[offer_id],
                share=0,
                pinned=False,
                status=OFFER_ACTIVE,
                is_new=True,
            ))

    rebalance(stream)
    stream.dirty = True
    db.flush()
    return stream


def remove_offer(db: Session, stream_offer_id: int) -> Stream:
    so = _get_stream_offer(db, stream_offer_id)
    stream = so.stream
    if so.is_new:
        stream.offers.remove(so)  # ещё не был в Keitaro — архивировать нечего
    else:
        so.status = OFFER_REMOVED
        so.pinned = False
        so.share = 0
    rebalance(stream)
    stream.dirty = True
    db.flush()
    return stream


def bring_back_offer(db: Session, stream_offer_id: int) -> Stream:
    so = _get_stream_offer(db, stream_offer_id)
    if so.status != OFFER_REMOVED:
        raise ServiceError("Оффер и так активен")
    so.status = OFFER_ACTIVE
    rebalance(so.stream)
    so.stream.dirty = True
    db.flush()
    return so.stream


def pin_offer(db: Session, stream_offer_id: int, share: Optional[int]) -> Stream:
    """Закрепляет долю оффера. share=None — закрепить текущую долю."""
    so = _get_stream_offer(db, stream_offer_id)
    if so.status != OFFER_ACTIVE:
        raise ServiceError("Закрепить можно только активный оффер")
    if share is not None:
        if not 0 <= share <= 100:
            raise ServiceError("Доля должна быть от 0 до 100")
        so.share = share
    so.pinned = True
    rebalance(so.stream)
    so.stream.dirty = True
    db.flush()
    return so.stream


def unpin_offer(db: Session, stream_offer_id: int) -> Stream:
    so = _get_stream_offer(db, stream_offer_id)
    so.pinned = False
    rebalance(so.stream)
    so.stream.dirty = True
    db.flush()
    return so.stream


def validate_shares(stream: Stream) -> None:
    active = [so for so in stream.offers if so.status == OFFER_ACTIVE]
    total = sum(so.share for so in active)
    if active and total != 100:
        raise ServiceError(f"Сумма долей {total}%, а должна быть 100%. Поправьте закреплённые доли.")


def push_stream(db: Session, kt: KeitaroClient, stream_id: int) -> Stream:
    stream = _get_stream(db, stream_id)
    validate_shares(stream)
    offers: List[Dict[str, Any]] = [
        {"offer_id": so.offer_id, "share": so.share, "state": "active"}
        for so in stream.offers
        if so.status == OFFER_ACTIVE
    ]
    rs = kt.update_stream(stream.keitaro_id, {"offers": offers})
    for so in stream.offers:
        so.is_new = False  # опубликованы — больше не черновики
    if not isinstance(rs, dict) or "offers" not in rs:
        rs = kt.stream(stream.keitaro_id)
    _apply_remote_stream(stream, rs, kt.offer_names())
    db.flush()
    return stream


def cancel_stream(db: Session, kt: KeitaroClient, stream_id: int) -> Stream:
    """Отменяет неопубликованные изменения: возвращает состояние из Keitaro."""
    stream = _get_stream(db, stream_id)
    _apply_remote_stream(stream, kt.stream(stream.keitaro_id), kt.offer_names())
    db.flush()
    return stream
