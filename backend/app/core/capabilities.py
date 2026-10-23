"""Configured capability rules. These are routing/eligibility rules for the simulation,
not clinical claims."""

VEHICLE_TAGS = ("BLS", "ALS", "cardiac", "neonatal", "trauma")

# A vehicle tag also satisfies these lower tags.
TAG_IMPLIES: dict[str, set[str]] = {"ALS": {"BLS"}}

# Reported category -> vehicle tags required.
CATEGORY_VEHICLE_TAGS: dict[str, set[str]] = {
    "cardiac": {"ALS", "cardiac"},
    "stroke": {"ALS"},
    "trauma": {"ALS", "trauma"},
    "neonatal": {"ALS", "neonatal"},
    "breathing": {"ALS"},
    "unconscious": {"ALS"},
    "other": {"BLS"},
    "unknown": {"ALS"},  # unknown is not minor: send advanced life support until reported otherwise
}

# Reported category -> hospital capabilities required (stable capabilities).
CATEGORY_HOSPITAL_CAPS: dict[str, set[str]] = {
    "cardiac": {"cardiac"},
    "stroke": {"stroke"},
    "trauma": {"trauma"},
    "neonatal": {"neonatal"},
    "breathing": {"emergency"},
    "unconscious": {"emergency"},
    "other": {"emergency"},
    "unknown": {"emergency"},
}

# Hospital capability -> current resource that must be non-stale and available.
CAP_RESOURCE: dict[str, str] = {
    "cardiac": "cath_lab",
    "stroke": "ct_scanner",
    "trauma": "trauma_team",
    "neonatal": "nicu_cot",
    "emergency": "er_bay",
}

HOSPITAL_CAPS = ("emergency", "cardiac", "stroke", "trauma", "neonatal", "burn")
HOSPITAL_RESOURCES = ("er_bay", "icu_bed", "cath_lab", "ct_scanner", "trauma_team", "nicu_cot")


def expand_tags(tags: set[str] | list[str]) -> set[str]:
    out = set(tags)
    for t in list(out):
        out |= TAG_IMPLIES.get(t, set())
    return out


def vehicle_satisfies(vehicle_tags: list[str], required: set[str]) -> bool:
    return required <= expand_tags(set(vehicle_tags))
