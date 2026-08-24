"""
Country -> timezone, for sending inside a contact's own waking hours.

Django ships no country/timezone table and neither pytz nor babel is a
dependency, so this is a deliberately small hand-maintained map covering the
markets the platform is actually used in. A country that isn't listed -- or a
contact with no country recorded -- falls back to the tenant's own timezone,
which is the behaviour every contact had before this existed.

Countries spanning several zones map to the one holding the large majority of
the population: quiet hours are a courtesy window, not a legal boundary, and
an hour of drift at the edge of a big country is not worth a full timezone
database to fix.
"""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

COUNTRY_TIMEZONES = {
    "AE": "Asia/Dubai", "AR": "America/Argentina/Buenos_Aires", "AU": "Australia/Sydney",
    "BD": "Asia/Dhaka", "BR": "America/Sao_Paulo", "CA": "America/Toronto",
    "CH": "Europe/Zurich", "CL": "America/Santiago", "CN": "Asia/Shanghai",
    "CO": "America/Bogota", "DE": "Europe/Berlin", "EG": "Africa/Cairo",
    "ES": "Europe/Madrid", "FR": "Europe/Paris", "GB": "Europe/London",
    "GH": "Africa/Accra", "HK": "Asia/Hong_Kong", "ID": "Asia/Jakarta",
    "IE": "Europe/Dublin", "IL": "Asia/Jerusalem", "IN": "Asia/Kolkata",
    "IT": "Europe/Rome", "JP": "Asia/Tokyo", "KE": "Africa/Nairobi",
    "KR": "Asia/Seoul", "LK": "Asia/Colombo", "MA": "Africa/Casablanca",
    "MX": "America/Mexico_City", "MY": "Asia/Kuala_Lumpur", "NG": "Africa/Lagos",
    "NL": "Europe/Amsterdam", "NP": "Asia/Kathmandu", "NZ": "Pacific/Auckland",
    "PE": "America/Lima", "PH": "Asia/Manila", "PK": "Asia/Karachi",
    "PL": "Europe/Warsaw", "PT": "Europe/Lisbon", "QA": "Asia/Qatar",
    "RO": "Europe/Bucharest", "RU": "Europe/Moscow", "SA": "Asia/Riyadh",
    "SE": "Europe/Stockholm", "SG": "Asia/Singapore", "TH": "Asia/Bangkok",
    "TR": "Europe/Istanbul", "TW": "Asia/Taipei", "TZ": "Africa/Dar_es_Salaam",
    "UA": "Europe/Kyiv", "UG": "Africa/Kampala", "US": "America/New_York",
    "VN": "Asia/Ho_Chi_Minh", "ZA": "Africa/Johannesburg",
}


def load_zone(name):
    """
    ZoneInfo for a stored timezone name, or None when it can't be loaded.

    Never raises: a policy row can outlive the tzdata release that named its
    zone, and a stale name must not be able to stop a campaign from sending.
    """
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def zone_for_country(country):
    """The ZoneInfo for a two-letter country code, or None if unmapped."""
    name = COUNTRY_TIMEZONES.get((country or "").strip().upper())
    return load_zone(name) if name else None
