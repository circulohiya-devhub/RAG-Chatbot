"""
Live rates, availability, and promos from the hotel's PMS.

The agent never talks to the PMS directly. It calls tools in rag.py, which call
get_client() here. Rate searches are read-only. Reservations are created only by
create_reservation(), after the guest has confirmed a summary (see rag.py). The bot
cannot change or cancel reservations.

PMS_PROVIDER in .env picks the source:
  mock       sample data for testing without any API (default)
  hotelogix  the Hotelogix Web API (sandbox or live, set by HOTELOGIX_BASE_URL)
"""
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timezone

import requests

log = logging.getLogger("pms")

CACHE_SECONDS = int(os.getenv("PMS_CACHE_SECONDS", "300"))  # reuse identical searches for 5 minutes
# false (default): the bot only saves reservations ON HOLD, and staff confirm them in the PMS.
# true: the bot also confirms them (payment type "Other", no payment).
CONFIRM_BOOKINGS = os.getenv("HOTELOGIX_CONFIRM_BOOKINGS", "false").lower() == "true"


class PMSError(Exception):
    """The PMS couldn't be reached or returned an error."""


@dataclass
class RoomOffer:
    room_type: str
    rate_plan: str              # e.g. "Best Available Rate", "Rainy Season Escape"
    total_price: float          # for the whole stay, all nights, including tax
    currency: str
    tax: float = 0.0            # tax portion of total_price, if the PMS reports it
    rooms_left: int | None = None
    inclusions: str = ""        # e.g. "Breakfast for 2"
    cancellation: str = ""      # e.g. "Free cancellation until 72 hours before arrival"
    deposit: float | None = None  # amount needed to confirm the booking, if the PMS reports it
    rate_id: str = ""           # PMS id of this rate, used to reserve it (never shown to guests)


@dataclass
class Promo:
    name: str
    description: str
    valid_from: str | None = None   # stay dates, YYYY-MM-DD
    valid_to: str | None = None
    conditions: str = ""


class PriceChangedError(PMSError):
    """The price changed between the summary the guest saw and the booking."""

    def __init__(self, new_total: float, currency: str):
        super().__init__(f"price changed to {currency} {new_total:,.2f}")
        self.new_total, self.currency = new_total, currency


class NotAvailableError(PMSError):
    """The room is no longer available for those dates."""


@dataclass
class Guest:
    first_name: str
    last_name: str
    email: str
    mobile: str


@dataclass
class ReservationResult:
    status: str                 # "ON_HOLD" (saved, staff confirm it) or "CONFIRMED"
    order_id: str
    reservation_codes: list
    room_type: str
    rate_plan: str
    total_price: float
    currency: str


class PMSClient(ABC):
    @abstractmethod
    def search(self, check_in: date, check_out: date, adults: int, children: int) -> list[RoomOffer]:
        """Available rooms and prices for the stay. Empty list = sold out."""

    @abstractmethod
    def promos(self) -> list[Promo]:
        """Promos currently bookable."""

    def create_reservation(self, check_in: date, check_out: date, adults: int, children: int,
                           room_type: str, expected_total: float, guest: Guest,
                           channel: str = "") -> ReservationResult:
        """Reserve one room. Re-checks price and availability first."""
        raise PMSError("Reservations are not supported by this PMS provider")


def _pick_offer(offers: list[RoomOffer], room_type: str) -> RoomOffer | None:
    """The cheapest offer for the requested room type (case-insensitive)."""
    wanted = room_type.strip().lower()
    matches = [o for o in offers if o.room_type.lower() == wanted]
    if not matches:
        matches = [o for o in offers if wanted in o.room_type.lower() or o.room_type.lower() in wanted]
    return min(matches, key=lambda o: o.total_price) if matches else None


def _find_order(response: dict) -> tuple[str, list]:
    """Order id and reservation codes from a savebooking/confirmbooking response.
    Looks in the documented place first, then anywhere in the response."""
    order = response.get("order") if isinstance(response.get("order"), dict) else {}
    order_id = order.get("id")
    if isinstance(order_id, dict):
        order_id = order_id.get("value")
    codes = [b.get("code") for b in order.get("bookings") or [] if isinstance(b, dict) and b.get("code")]
    if not order_id:
        def walk(x):
            if isinstance(x, dict):
                for k, v in x.items():
                    if isinstance(v, str) and (v.startswith("CRS-") or k.lower() in ("orderid", "order_id")):
                        return v
                    found = walk(v)
                    if found:
                        return found
            elif isinstance(x, list):
                for v in x:
                    found = walk(v)
                    if found:
                        return found
            return None
        order_id = walk(response)
    return (str(order_id) if order_id else ""), codes


def _check_price(offer: RoomOffer, expected_total: float) -> None:
    if abs(offer.total_price - expected_total) > 1:
        raise PriceChangedError(offer.total_price, offer.currency)


# ------------------------------------------------------------------
# Mock: realistic sample data so you can test the bot today
# ------------------------------------------------------------------
class MockPMS(PMSClient):
    ROOMS = [
        # name, nightly rate, max adults, breakfast included
        ("Standard Room", 2800, 2, False),
        ("Deluxe Room", 3800, 3, True),
        ("Premier Room", 4800, 3, True),
        ("Family Suite", 6500, 4, True),
    ]

    def search(self, check_in, check_out, adults, children):
        nights = (check_out - check_in).days
        rainy = date(2026, 10, 1) <= check_in <= date(2026, 11, 30) and nights >= 2
        offers = []
        for i, (name, nightly, max_adults, breakfast) in enumerate(self.ROOMS):
            if adults > max_adults:
                continue
            total = nightly * nights
            plan = "Best Available Rate"
            if rainy and name in ("Deluxe Room", "Premier Room"):
                total *= 0.75
                plan = "Rainy Season Escape (25% off)"
            offers.append(RoomOffer(
                room_type=name,
                rate_plan=plan,
                total_price=round(total, 2),
                currency="PHP",
                rooms_left=(check_in.toordinal() + i) % 6,  # varies by date; 0 = sold out
                inclusions=f"Breakfast for {min(adults, 2)}" if breakfast else "Room only",
                cancellation="Free cancellation until 72 hours before arrival",
            ))
        return [o for o in offers if o.rooms_left]

    def create_reservation(self, check_in, check_out, adults, children, room_type, expected_total, guest,
                           channel=""):
        offer = _pick_offer(self.search(check_in, check_out, adults, children), room_type)
        if not offer:
            raise NotAvailableError(room_type)
        _check_price(offer, expected_total)
        code = f"MOCK-{int(time.time()) % 1000000:06d}"
        status = "CONFIRMED" if CONFIRM_BOOKINGS else "ON_HOLD"
        log.info("MOCK reservation %s (%s) for %s %s", code, status, guest.first_name, guest.last_name)
        return ReservationResult(status, code, [code], offer.room_type, offer.rate_plan,
                                 offer.total_price, offer.currency)

    def promos(self):
        return [
            Promo("Rainy Season Escape", "25% off Deluxe and Premier rooms",
                  "2026-10-01", "2026-11-30", "Minimum 2 nights. Book direct. Not combinable."),
            Promo("Stay 3, Pay 2", "3rd night free on all room types",
                  None, "2026-12-15", "Sunday to Thursday stays only."),
        ]


# ------------------------------------------------------------------
# Hotelogix Web API (per the "Hotelogix Web API Document")
# ------------------------------------------------------------------
class HotelogixClient(PMSClient):
    """
    Flow, from the Hotelogix Web API docs:
      1. POST {base}/wsauth with the Consumer Key, signed with the Consumer Secret
         -> returns an Access Key and Access Secret
      2. POST {base}/search with the Access Key, signed with the Access Secret
    Signature: HMAC-SHA1 of the exact request body, sent in the X-HAPI-Signature header.
    The "datetime" in each request must be within 5 minutes of current UTC time.

    Only WEB (public) rates are requested: "searchfor" is never sent, so Travel Agent,
    Corporate, and Front Desk rates are not returned. Any rate whose rateType is in
    EXCLUDED_RATE_TYPES is dropped as a second safety net.
    """

    EXCLUDED_RATE_TYPES = {"TA", "CORP", "CORPORATE", "TRAVELAGENT"}

    def __init__(self):
        # Demo/sandbox: http://crs.hotelogix.net/ws/web   Live: https://crs.staygrid.com/ws/web
        self.base_url = os.getenv("HOTELOGIX_BASE_URL", "http://crs.hotelogix.net/ws/web").rstrip("/")
        self.consumer_key = os.environ["HOTELOGIX_CONSUMER_KEY"]
        self.consumer_secret = os.environ["HOTELOGIX_CONSUMER_SECRET"]
        self.hotel_id = os.environ["HOTELOGIX_HOTEL_ID"]
        self.language = os.getenv("HOTELOGIX_LANGUAGE", "en")
        # Confirm in the sandbox: search a 2-night stay and check whether "price"
        # is for the whole stay (true) or per night (false).
        self.price_is_total = os.getenv("HOTELOGIX_PRICE_IS_TOTAL", "true").lower() == "true"
        allowed = os.getenv("HOTELOGIX_RATE_TYPES", "").strip()
        self.allowed_rate_types = {t.strip().upper() for t in allowed.split(",") if t.strip()}
        self.debug = os.getenv("HOTELOGIX_DEBUG", "false").lower() == "true"
        # Reservations
        # Booking classification. Values must match the property's setup exactly:
        # list them with get_hotel_options() (see README).
        self.business_source = os.getenv("HOTELOGIX_BUSINESS_SOURCE", "").strip()
        self.market_segment = os.getenv("HOTELOGIX_MARKET_SEGMENT", "").strip()
        self.guest_type = os.getenv("HOTELOGIX_GUEST_TYPE", "").strip()
        self.salesperson = os.getenv("HOTELOGIX_SALESPERSON", "").strip()
        # Business source per chat channel, e.g. "facebook=FB,web=Web,instagram=IG".
        # Channels not listed use HOTELOGIX_BUSINESS_SOURCE.
        self.source_by_channel = {}
        for pair in os.getenv("HOTELOGIX_SOURCE_BY_CHANNEL", "").split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                self.source_by_channel[k.strip().lower()] = v.strip()
        self.guest_country_code = os.getenv("HOTELOGIX_GUEST_COUNTRY_CODE", "PH")
        self.guest_country = os.getenv("HOTELOGIX_GUEST_COUNTRY", "Philippines")
        self._booking_lock = threading.Lock()  # one booking at a time: the cart belongs to the access key
        self._access_key: str | None = None
        self._access_secret: str | None = None
        self._auth_lock = threading.Lock()

    # ---------- low level ----------
    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    def _post(self, method: str, request: dict, secret: str) -> dict:
        body = json.dumps({
            "hotelogix": {"version": "1.0", "datetime": self._utc_now(), "request": request}
        })
        signature = hmac.new(secret.encode(), body.encode(), hashlib.sha1).hexdigest()
        if self.debug and method != "wsauth":
            redacted = json.loads(body)
            redacted["hotelogix"]["request"]["key"] = "***"
            log.info("Hotelogix %s request: %s", method, json.dumps(redacted)[:4000])
        try:
            r = requests.post(
                f"{self.base_url}/{method}",
                data=body.encode(),
                headers={"Content-Type": "application/json", "X-HAPI-Signature": signature},
                timeout=20,
            )
        except requests.RequestException as e:
            raise PMSError(f"Hotelogix {method} request failed: {e}") from e
        if r.status_code >= 400:
            # Include Hotelogix's own error text, which explains what it rejected.
            raise PMSError(f"Hotelogix {method} HTTP {r.status_code}: {r.text[:1000]}")
        try:
            data = r.json()
        except ValueError as e:
            raise PMSError(f"Hotelogix {method} returned non-JSON: {r.text[:500]}") from e
        if self.debug:
            log.info("Hotelogix %s response: %s", method, json.dumps(data)[:4000])

        response = (data.get("hotelogix") or {}).get("response") or {}
        status = response.get("status") or {}
        code = str(status.get("code", "0"))
        message = str(status.get("message", ""))
        # Each method has its own success code (search 1400, addtocart 1500, savebooking 1600,
        # confirmbooking 1602, ...), and all of them say "SUCCESS".
        if code != "0" and message.strip().upper() != "SUCCESS":
            raise PMSError(f"Hotelogix {method} error {code}: {message}")
        return response

    def _authenticate(self) -> None:
        response = self._post(
            "wsauth", {"method": "wsauth", "key": self.consumer_key}, self.consumer_secret
        )
        key, secret = response.get("accesskey"), response.get("accesssecret")
        # The JSON format returns plain strings; the XML format uses {"value": ...}
        key = key.get("value") if isinstance(key, dict) else key
        secret = secret.get("value") if isinstance(secret, dict) else secret
        if not key or not secret:
            raise PMSError("Hotelogix wsauth did not return an access key")
        self._access_key, self._access_secret = key, secret
        log.info("Hotelogix: new access key obtained")

    def _call(self, method: str, data: dict, retry: bool = True) -> dict:
        """Call an API method, getting (or renewing) the access key as needed.
        retry=False is used for booking steps, which must never be sent twice."""
        for attempt in ((1, 2) if retry else (2,)):
            with self._auth_lock:
                if not self._access_key:
                    self._authenticate()
                key, secret = self._access_key, self._access_secret
            try:
                return self._post(
                    method,
                    {"method": method, "key": key, "languagecode": self.language, "data": data},
                    secret,
                )
            except PMSError:
                if attempt == 2:
                    raise
                # The access key may have expired: get a new one and retry once.
                with self._auth_lock:
                    self._access_key = self._access_secret = None
        raise PMSError("unreachable")

    # ---------- public ----------
    def search(self, check_in, check_out, adults, children):
        nights = (check_out - check_in).days
        response = self._call("search", {
            "stay": {"checkindate": check_in.isoformat(), "checkoutdate": check_out.isoformat()},
            "pax": {"adult": str(adults), "child": str(children), "infant": "0"},
            "hotels": [{"id": int(self.hotel_id)}],
            "roomrequire": 1,
            "limit": {"value": 20, "offset": 0},
            # "searchfor" is intentionally omitted: web (public) rates only.
        })

        offers: list[RoomOffer] = []
        for hotel in response.get("hotels") or []:
            currency = hotel.get("currencycode") or "PHP"
            for rt in hotel.get("roomtypes") or []:
                for rate in rt.get("rates") or []:
                    rate_type = str(rate.get("rateType") or rt.get("rateType") or "").upper()
                    if rate_type in self.EXCLUDED_RATE_TYPES:
                        continue
                    if self.allowed_rate_types and rate_type not in self.allowed_rate_types:
                        continue
                    price = float(rate.get("price") or 0)
                    tax = float(rate.get("tax") or 0)
                    if price <= 0:
                        continue
                    if not self.price_is_total:
                        price, tax = price * nights, tax * nights
                    inclusions = ", ".join(
                        i.get("title", "").strip() for i in rate.get("inclusions") or [] if i.get("title")
                    )
                    rooms_left = rate.get("availableroom", rt.get("availableroom"))
                    # The rate "title" is often an internal code (e.g. "WEBRB"); the
                    # description ("Room and breakfast") reads better for guests.
                    plan = (rate.get("description") or rate.get("title") or "Standard Rate").strip()
                    # Use only the PMS's own written policy text, never the raw codes.
                    policies = rate.get("cancellationPolicies") or rate.get("cancellationpolicies") or []
                    cancellation = " ".join(
                        p.get("customDescription", "").strip() for p in policies if p.get("customDescription")
                    )
                    deposit = (rate.get("bookingpolicy") or {}).get("depositamount")
                    try:
                        deposit = round(float(deposit), 2) if deposit is not None else None
                    except (TypeError, ValueError):
                        deposit = None
                    offers.append(RoomOffer(
                        room_type=(rt.get("title") or "Room").strip(),
                        rate_plan=plan,
                        total_price=round(price + tax, 2),
                        tax=round(tax, 2),
                        currency=currency,
                        rooms_left=int(rooms_left) if str(rooms_left).isdigit() else None,
                        inclusions=inclusions,
                        cancellation=cancellation,
                        deposit=deposit,
                        rate_id=str(rate.get("id") or ""),
                    ))
        return [o for o in offers if o.rooms_left != 0]

    def get_hotel_options(self) -> dict:
        """The property's business sources, market segments, guest types, salespeople,
        web settings, and night audit date. Used once, to fill in the booking settings."""
        response = self._call("gethoteloptions", {
            "businesssource": True, "marketsegment": True, "guesttype": True,
            "salesperson": True, "webSettings": True,
        })
        hotel = (response.get("hotels") or [{}])[0]
        skip = {"logo", "description", "seasons", "roomtypes"}  # large and not needed here
        return {k: v for k, v in hotel.items() if k not in skip}

    def _guest_payload(self, guest: Guest) -> dict:
        return {
            "fname": {"value": guest.first_name},
            "lname": {"value": guest.last_name},
            "email": {"value": guest.email},
            "mobile": {"value": guest.mobile},
            "phone": {"value": guest.mobile},
            "country": {"code": self.guest_country_code, "value": self.guest_country},
        }

    def create_reservation(self, check_in, check_out, adults, children, room_type, expected_total, guest,
                           channel=""):
        """search -> addtocart -> savebooking (ON HOLD), then confirmbooking only if
        HOTELOGIX_CONFIRM_BOOKINGS=true (payment type OTH, no payment)."""
        with self._booking_lock:
            # Fresh login and fresh search, so the rate id and price are current.
            with self._auth_lock:
                self._authenticate()
            offer = _pick_offer(self.search(check_in, check_out, adults, children), room_type)
            if not offer or not offer.rate_id:
                raise NotAvailableError(room_type)
            _check_price(offer, expected_total)

            try:  # remove anything left in this access key's cart
                self._call("clearcart", {}, retry=False)
            except PMSError:
                pass

            cart = self._call("addtocart", {"items": [{"value": offer.rate_id}]}, retry=False)
            hotel = (cart.get("hotels") or [{}])[0]
            booking = (hotel.get("bookings") or [{}])[0]
            stays = booking.get("gueststays") or []
            if not hotel.get("id") or not booking.get("id") or not stays:
                raise PMSError("Hotelogix addtocart returned no booking")

            guest_payload = self._guest_payload(guest)
            # Every guest slot needs guest details: with 2 adults there are 2 slots, and
            # leaving the second one empty makes savebooking fail with HTTP 500.
            gueststays = [{"id": s["id"], "guest": guest_payload} for s in stays if s.get("id")]
            hotel_payload = {
                "id": hotel["id"],
                "preference": {"value": "nopreference"},
                "bookings": {"booking": {"id": booking["id"], "gueststays": gueststays}},
            }
            source = self.source_by_channel.get(channel.lower(), self.business_source)
            for field, value in (("businesssource", source),
                                 ("marketsegment", self.market_segment),
                                 ("guesttype", self.guest_type),
                                 ("salesperson", self.salesperson)):
                if value:
                    hotel_payload[field] = {"value": value}

            saved = self._call("savebooking", {
                "isTemp": {"value": False},
                "isGroupBooking": {"value": False},
                "guest": guest_payload,
                "hotels": [hotel_payload],
            }, retry=False)
            order_id, codes = _find_order(saved)
            if not order_id and not codes:
                log.warning("savebooking succeeded but no order id was found: %s",
                            json.dumps(saved, default=str)[:1500])

            status = "ON_HOLD"
            if CONFIRM_BOOKINGS and order_id:
                try:
                    confirmed = self._call("confirmbooking", {
                        "orderId": {"value": order_id},
                        "payment": {"type": "OTH", "amount": "0",
                                    "description": "Reserved via chatbot - no payment collected"},
                        "errorIfNotConfirm": {"value": 1},
                    }, retry=False)
                    status = "CONFIRMED"
                    codes = _find_order(confirmed)[1] or codes
                except PMSError:
                    log.exception("Hotelogix confirmbooking failed for order %s (left on hold)", order_id)

            log.info("Hotelogix reservation %s: order %s, codes %s", status, order_id, codes)
            return ReservationResult(status, order_id or "", codes, offer.room_type, offer.rate_plan,
                                     offer.total_price, offer.currency)

    def promos(self):
        # The Web API has no separate promo call. Promos and packages come back as
        # rate plans inside search results (e.g. "Family Package"), so the bot shows
        # them when a guest checks dates. General promo info stays in the Google Doc.
        return []


# ------------------------------------------------------------------
# Caching wrapper: fewer API calls, faster replies
# ------------------------------------------------------------------
class CachedPMS(PMSClient):
    def __init__(self, inner: PMSClient, ttl: int):
        self.inner, self.ttl = inner, ttl
        self._cache: dict[tuple, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def _get(self, key, fetch):
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < self.ttl:
                return hit[1]
        value = fetch()
        with self._lock:
            self._cache[key] = (now, value)
            if len(self._cache) > 2000:
                self._cache.clear()
        return value

    def search(self, check_in, check_out, adults, children):
        return self._get(("search", check_in, check_out, adults, children),
                         lambda: self.inner.search(check_in, check_out, adults, children))

    def promos(self):
        return self._get(("promos",), self.inner.promos)

    def create_reservation(self, *args, **kwargs):
        try:
            return self.inner.create_reservation(*args, **kwargs)
        finally:
            with self._lock:
                self._cache.clear()  # availability changed


_client: PMSClient | None = None


def get_client() -> PMSClient:
    global _client
    if _client is None:
        provider = os.getenv("PMS_PROVIDER", "mock").lower()
        inner = HotelogixClient() if provider == "hotelogix" else MockPMS()
        log.info("PMS provider: %s", provider)
        _client = CachedPMS(inner, CACHE_SECONDS)
    return _client
