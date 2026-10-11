"""Old-seat vacancy only: NOT a certificate that an object is held."""


def check(sample):
    before = len(sample["before_z"])
    after = len(sample["after_z"])
    support = len(sample["support_z"])
    return support > 150 and after < max(40, 0.2 * before)
