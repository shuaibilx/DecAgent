"""A semantic defect: exclusive upper bound was implemented inclusively."""


def clamp(value, upper):
    return min(value, upper)
