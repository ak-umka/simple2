import itertools

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import services
from app.db import Base
from app.models import OFFER_ACTIVE, OFFER_REMOVED
from app.weights import WeightError, distribute


class FakeKeitaro:
    """Имитация Keitaro API в памяти."""

    def __init__(self):
        self._ids = itertools.count(1000)
        self._offers = {i: {"id": i, "name": f"Offer {i}"} for i in range(1, 10)}
        self.campaigns_db = {}
        self.streams_db = {}

    def offers(self, force=False):
        return list(self._offers.values())

    def offer_names(self):
        return {i: o["name"] for i, o in self._offers.items()}

    def create_campaign(self, payload):
        c = dict(payload, id=next(self._ids), alias="x")
        self.campaigns_db[c["id"]] = c
        return c

    def campaign(self, cid):
        return self.campaigns_db[cid]

    def create_stream(self, payload):
        s = dict(payload, id=next(self._ids))
        s["offers"] = [dict(o, id=next(self._ids)) for o in payload.get("offers", [])]
        self.streams_db[s["id"]] = s
        return s

    def streams(self, cid):
        return [s for s in self.streams_db.values() if s["campaign_id"] == cid]

    def stream(self, sid):
        return self.streams_db[sid]

    def update_stream(self, sid, payload):
        self.streams_db[sid]["offers"] = [dict(o, id=next(self._ids)) for o in payload["offers"]]
        return self.streams_db[sid]


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


@pytest.fixture
def kt():
    return FakeKeitaro()


def offer_stream(campaign):
    return next(s for s in campaign.streams if s.schema == "landings")


def shares(stream):
    return {so.offer_id: (so.share, so.status) for so in stream.offers}


def remote_shares(kt, stream):
    return {o["offer_id"]: o["share"] for o in kt.stream(stream.keitaro_id)["offers"]}


# ---------------------------------------------------------------- weights


def test_distribute_equal():
    assert distribute([], 4) == [25, 25, 25, 25]


def test_distribute_remainder_sums_to_100():
    result = distribute([], 3)
    assert sum(result) == 100 and sorted(result) == [33, 33, 34]


def test_distribute_with_pinned():
    assert distribute([25], 2) == [38, 37]


def test_distribute_too_much_pinned():
    with pytest.raises(WeightError):
        distribute([60, 50], 1)


# ---------------------------------------------------------------- создание


def test_create_campaign_builds_two_streams(db, kt):
    c = services.create_campaign(db, kt, "camp1", "au", 3, group_id=7)
    remote = kt.campaign(c.keitaro_id)
    assert remote["name"].endswith("camp1") and remote["group_id"] == 7

    first, second = sorted(kt.streams(c.keitaro_id), key=lambda s: s["position"])
    assert first["filters"][0] == {"name": "country", "mode": "accept", "payload": ["AU"]}
    assert first["action_payload"] == "https://google.com"
    assert second["offers"][0]["offer_id"] == 3 and second["offers"][0]["share"] == 100
    assert len(c.streams) == 2


def test_create_campaign_validates_geo_and_offer(db, kt):
    with pytest.raises(services.ServiceError):
        services.create_campaign(db, kt, "c", "AUS", 1)
    with pytest.raises(services.ServiceError):
        services.create_campaign(db, kt, "c", "AU", 999)


# ---------------------------------------------------------------- редактор


def test_add_offer_rebalances_and_push(db, kt):
    c = services.create_campaign(db, kt, "c", "AU", 1)
    s = offer_stream(c)
    services.add_offer(db, kt, s.id, 2)
    services.add_offer(db, kt, s.id, 3)
    services.add_offer(db, kt, s.id, 4)
    assert s.dirty
    assert {k: v[0] for k, v in shares(s).items()} == {1: 25, 2: 25, 3: 25, 4: 25}
    assert remote_shares(kt, s) == {1: 100}  # ещё не опубликовано

    services.push_stream(db, kt, s.id)
    assert not s.dirty
    assert remote_shares(kt, s) == {1: 25, 2: 25, 3: 25, 4: 25}
    assert not any(so.is_new for so in s.offers)


def test_cancel_drops_unpublished_offers(db, kt):
    c = services.create_campaign(db, kt, "c", "AU", 1)
    s = offer_stream(c)
    services.add_offer(db, kt, s.id, 2)
    services.cancel_stream(db, kt, s.id)
    assert shares(s) == {1: (100, OFFER_ACTIVE)}
    assert not s.dirty


def test_remove_archives_and_bring_back(db, kt):
    c = services.create_campaign(db, kt, "c", "AU", 1)
    s = offer_stream(c)
    services.add_offer(db, kt, s.id, 2)
    services.push_stream(db, kt, s.id)

    so2 = next(so for so in s.offers if so.offer_id == 2)
    services.remove_offer(db, so2.id)
    assert shares(s) == {1: (100, OFFER_ACTIVE), 2: (0, OFFER_REMOVED)}
    services.push_stream(db, kt, s.id)
    assert remote_shares(kt, s) == {1: 100}
    assert so2.status == OFFER_REMOVED  # остался в архиве обёртки

    services.bring_back_offer(db, so2.id)
    services.push_stream(db, kt, s.id)
    assert remote_shares(kt, s) == {1: 50, 2: 50}


def test_remove_unpublished_offer_deletes_it(db, kt):
    c = services.create_campaign(db, kt, "c", "AU", 1)
    s = offer_stream(c)
    services.add_offer(db, kt, s.id, 2)
    so2 = next(so for so in s.offers if so.offer_id == 2)
    services.remove_offer(db, so2.id)
    assert set(shares(s)) == {1}


def test_fetch_marks_offers_deleted_in_keitaro_as_removed(db, kt):
    c = services.create_campaign(db, kt, "c", "AU", 1)
    s = offer_stream(c)
    for oid in (2, 3):
        services.add_offer(db, kt, s.id, oid)
    services.push_stream(db, kt, s.id)

    # пользователь удалил оффер 3 прямо в Keitaro
    remote = kt.stream(s.keitaro_id)
    remote["offers"] = [o for o in remote["offers"] if o["offer_id"] != 3]
    for o in remote["offers"]:
        o["share"] = 50

    services.sync_campaign(db, kt, c)
    assert shares(s) == {1: (50, OFFER_ACTIVE), 2: (50, OFFER_ACTIVE), 3: (0, OFFER_REMOVED)}


def test_pinned_share_survives_rebalance(db, kt):
    c = services.create_campaign(db, kt, "c", "AU", 1)
    s = offer_stream(c)
    for oid in (2, 3, 4):
        services.add_offer(db, kt, s.id, oid)
    services.push_stream(db, kt, s.id)
    so1 = next(so for so in s.offers if so.offer_id == 1)
    services.pin_offer(db, so1.id, 25)

    so4 = next(so for so in s.offers if so.offer_id == 4)
    services.remove_offer(db, so4.id)
    assert shares(s)[1] == (25, OFFER_ACTIVE)
    assert sum(so.share for so in s.offers if so.status == OFFER_ACTIVE) == 100
    assert shares(s)[2][0] + shares(s)[3][0] == 75

    services.bring_back_offer(db, so4.id)
    assert shares(s)[1] == (25, OFFER_ACTIVE)
    assert {shares(s)[i][0] for i in (2, 3, 4)} == {25}


def test_pin_over_100_rejected(db, kt):
    c = services.create_campaign(db, kt, "c", "AU", 1)
    s = offer_stream(c)
    services.add_offer(db, kt, s.id, 2)
    so1, so2 = s.offers
    services.pin_offer(db, so1.id, 60)
    with pytest.raises(services.ServiceError):
        services.pin_offer(db, so2.id, 50)


def test_push_rejects_bad_total(db, kt):
    c = services.create_campaign(db, kt, "c", "AU", 1)
    s = offer_stream(c)
    services.add_offer(db, kt, s.id, 2)
    so1, so2 = s.offers
    services.pin_offer(db, so1.id, 30)
    services.pin_offer(db, so2.id, 30)  # все закреплены, сумма 60
    with pytest.raises(services.ServiceError):
        services.push_stream(db, kt, s.id)


def test_cannot_add_offer_to_redirect_stream(db, kt):
    c = services.create_campaign(db, kt, "c", "AU", 1)
    redirect = next(s for s in c.streams if s.schema == "redirect")
    with pytest.raises(services.ServiceError):
        services.add_offer(db, kt, redirect.id, 2)
