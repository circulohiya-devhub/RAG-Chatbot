"""
Live rates, availability, and promos from the hotel's PMS.

The agent never talks to the PMS directly. It calls two READ-ONLY tools in rag.py,
which call get_client() here. The bot cannot create, change, or cancel bookings.

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


@dataclass
class Promo:
    name: str
    description: str
    valid_from: str | None = None   # stay dates, YYYY-MM-DD
    valid_to: str | None = None
    conditions: str = ""


class PMSClient(ABC):
    @abstractmethod
    def search(self, check_in: date, check_out: date, adults: int, children: int) -> list[RoomOffer]:
        """Available rooms and prices for the stay. Empty list = sold out."""

    @abstractmethod
    def promos(self) -> list[Promo]:
        """Promos currently bookable."""


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
        try:
            r = requests.post(
                f"{self.base_url}/{method}",
                data=body.encode(),
                headers={"Content-Type": "application/json", "X-HAPI-Signature": signature},
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            raise PMSError(f"Hotelogix {method} request failed: {e}") from e
        if self.debug:
            log.info("Hotelogix %s response: %s", method, json.dumps(data)[:4000])

        response = (data.get("hotelogix") or {}).get("response") or {}
        status = response.get("status") or {}
        code = str(status.get("code", "0"))
        if code not in ("0", "1400"):
            raise PMSError(f"Hotelogix {method} error {code}: {status.get('message', '')}")
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

    def _call(self, method: str, data: dict) -> dict:
        """Call an API method, getting (or renewing) the access key as needed."""
        for attempt in (1, 2):
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
                    offers.append(RoomOffer(
                        room_type=(rt.get("title") or "Room").strip(),
                        rate_plan=(rate.get("title") or "Standard Rate").strip(),
                        total_price=round(price + tax, 2),
                        tax=round(tax, 2),
                        currency=currency,
                        rooms_left=int(rooms_left) if str(rooms_left).isdigit() else None,
                        inclusions=inclusions,
                        # Cancellation terms stay in the Google Doc: the API's policy
                        # codes are easy to misread, and a wrong promise costs money.
                        cancellation="",
                    ))
        return [o for o in offers if o.rooms_left != 0]

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


_client: PMSClient | None = None


def get_client() -> PMSClient:
    global _client
    if _client is None:
        provider = os.getenv("PMS_PROVIDER", "mock").lower()
        inner = HotelogixClient() if provider == "hotelogix" else MockPMS()
        log.info("PMS provider: %s", provider)
        _client = CachedPMS(inner, CACHE_SECONDS)
    return _client
