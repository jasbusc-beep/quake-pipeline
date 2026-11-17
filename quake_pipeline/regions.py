"""Turn a USGS place string into a region label.

USGS is not consistent. The same state shows up as "CA" and "California",
and events at sea have no comma at all ("south of the Fiji Islands"). This
module maps those onto one label so groups are not split or lost.
"""

from .sanitize import clean_label

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
    "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota",
    "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
    "PR": "Puerto Rico",
}
OTHER_ABBREVIATIONS = {"MX": "Mexico"}
ABBREVIATIONS = {**US_STATES, **OTHER_ABBREVIATIONS}


def region_of(place: object) -> str:
    """Examples:
    '10 km SW of Town, Alaska'   -> 'Alaska'
    '5 km NW of The Geysers, CA' -> 'California'
    'south of the Fiji Islands'  -> 'Fiji Islands'
    'Mid-Indian Ridge'           -> 'Mid-Indian Ridge'
    None or ''                   -> 'Unknown'
    """
    if not isinstance(place, str) or not place.strip():
        return "Unknown"
    place = place.strip()
    if ", " in place:
        region = place.rsplit(", ", 1)[1].strip()
        region = ABBREVIATIONS.get(region, region)
    elif " of " in place:
        region = place.rsplit(" of ", 1)[1].strip()
        if region.lower().startswith("the "):
            region = region[4:]
    else:
        region = place
    return clean_label(region)
