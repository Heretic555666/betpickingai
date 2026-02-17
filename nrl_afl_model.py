# =====================================
# PRO EDGE MODEL — NRL & AFL
# =====================================

NRL_AVG_TOTAL = 44.0
AFL_AVG_TOTAL = 166.0

NRL_HOME_EDGE = 2.2
AFL_HOME_EDGE = 6.5


def project_total(book_total, sport):
    """
    Anchored projection using league averages
    and home advantage weighting.
    """

    if sport == "NRL":
        baseline = NRL_AVG_TOTAL
        home_edge = NRL_HOME_EDGE
    else:
        baseline = AFL_AVG_TOTAL
        home_edge = AFL_HOME_EDGE

    # blend market line with league baseline
    projected = (book_total * 0.6) + (baseline * 0.4)

    # apply home advantage effect
    projected += home_edge * 0.25

    return round(projected, 2)


def calculate_edge(model_total, book_total):
    return round(model_total - book_total, 2)


def calculate_confidence(edge, sport):
    """
    Confidence scaled to sport scoring range.
    """

    scale = 0.18 if sport == "NRL" else 0.06
    confidence = min(abs(edge) * scale, 1.0)

    return confidence
