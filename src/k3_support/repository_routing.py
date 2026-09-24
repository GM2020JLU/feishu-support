"""Instance-local repository routing; matches are hints, never new authority."""

LEGACY_KEYWORDS = {
    "u-boot": ["u-boot", "uboot", "bootloader"],
    "linux": ["linux", "kernel", "内核", "driver", "驱动", "dts", "dtb"],
    "edk2": ["edk2 core", "tianocore core"],
    "edk2-platforms": [
        "edk2-platforms",
        "edk2 platform",
        "uefi platform",
        "dxe",
        "pei",
    ],
}


def default_keywords(name, schema_version):
    if schema_version == 1 and name in LEGACY_KEYWORDS:
        return list(LEGACY_KEYWORDS[name])
    return [name]


def route(query, repositories):
    text = query.casefold()
    matches = {
        name
        for name, repository in repositories.items()
        if any(term.casefold() in text for term in repository["routing_keywords"])
    }
    return next(iter(matches)) if len(matches) == 1 else None
