"""Old-seat vacancy only: NOT a certificate that an object is held."""


def _in_seat(zs, ceiling):
    # Points above the old seat's upper boundary are elsewhere in Z (e.g. an
    # object lifted but still over the same XY), so they are not occupancy.
    return sum(1 for z in zs if z <= ceiling)


def check(sample):
    ceiling = sample["ceiling"]
    before = _in_seat(sample["before_z"], ceiling)
    after = _in_seat(sample["after_z"], ceiling)
    support = _in_seat(sample["support_z"], ceiling)
    if before < 40:
        return False  # seat was not observed occupied: no vacancy claim
    return support > 150 and after < max(40, 0.2 * before)
