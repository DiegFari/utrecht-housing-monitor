"""Read the public data used by THE FIZZ Utrecht's booking widget.

Observed 2026-09-19: pex-results-in-building.js displays its Book action when
the requested building's roomType has marketingRent. This is category-level
availability, not a reservation or a count of individual vacant units.
"""

import hashlib
import json
from urllib.request import Request, urlopen

from monitor import Listing, PageChangedError, create_tls_context

URL = "https://www.the-fizz.com/en/student-accommodation/utrecht/#apartment"
API = "https://booking.the-fizz.com/json-interface/rs/marketing/marketingCollections"
BUILDING = "FIZZ_UTRECHT"
BOOKING_URL = "https://www.the-fizz.com/en/search-nl/#/searchcriteria=BUILDING:FIZZ_UTRECHT;AREA:UTRECHT;"


def parse_listings(payload: dict) -> list[Listing]:
    if not isinstance(payload, dict) or payload.get("status") != "OK":
        raise PageChangedError("THE FIZZ availability response was not successful.")
    settings = payload.get("settings")
    if not isinstance(settings, dict) or settings.get("booking.strictGenders") is not False:
        raise PageChangedError("THE FIZZ booking requirements changed.")
    locations = payload.get("locations")
    if not isinstance(locations, list):
        raise PageChangedError("THE FIZZ locations are missing.")
    buildings = []
    for location in locations:
        if not isinstance(location, dict) or not isinstance(location.get("buildings"), list):
            raise PageChangedError("THE FIZZ building data changed.")
        buildings.extend(b for b in location["buildings"]
                         if isinstance(b, dict) and b.get("lookupValue") == BUILDING)
    if len(buildings) != 1:
        raise PageChangedError("THE FIZZ Utrecht building was not uniquely identified.")
    rooms = buildings[0].get("roomTypes")
    if not isinstance(rooms, list) or not rooms:
        raise PageChangedError("THE FIZZ room categories are missing.")
    listings = {}
    for room in rooms:
        if (not isinstance(room, dict)
                or not isinstance(room.get("lookupValue"), str)
                or not isinstance(room.get("displayValue"), str)):
            raise PageChangedError("THE FIZZ room category format changed.")
        rent = room.get("marketingRent")
        if rent is None:
            continue
        # Do not mistake a malformed object or a static price in page copy for
        # an offer. A live positive payload still needs verification.
        if not isinstance(rent, dict) or not rent.get("displayAmount"):
            raise PageChangedError("THE FIZZ offer format changed.")
        key = hashlib.sha256(f"{BUILDING}:{room['lookupValue']}".encode()).hexdigest()[:24]
        listings[key] = Listing(key, room["displayValue"], BOOKING_URL)
    return sorted(listings.values(), key=lambda listing: listing.name)


def fetch() -> list[Listing]:
    request = Request(API, data=json.dumps({"buildingCode": BUILDING}).encode(),
                      headers={"Content-Type": "application/json", "Accept": "application/json",
                               "X-Widget-Language": "en-GB"}, method="POST")
    with urlopen(request, timeout=45, context=create_tls_context()) as response:
        if response.headers.get_content_type() != "application/json":
            raise PageChangedError("THE FIZZ returned a non-JSON response.")
        content = response.read(2_000_001)
        if len(content) > 2_000_000:
            raise PageChangedError("THE FIZZ response is unexpectedly large.")
    return parse_listings(json.loads(content))
