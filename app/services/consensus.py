"""Delphi consensus (human-in-the-loop). Ratings use the 1-9 GRADE-style scale.

Decision rule (configurable): with at least `delphi_min_raters` ratings in the latest round,
  include  if >= threshold of raters score importance 7-9,
  exclude  if >= threshold score importance 1-3,
  otherwise no consensus -> carry to the next round.
Feasibility (can it be captured in routine care / EHR?) is summarised the same way and reported,
which matters most while no TMD EHR exists.
"""
from __future__ import annotations

from typing import Optional

from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import CDE


def _summary(ratings: list, rnd: Optional[int]) -> dict:
    n = len(ratings)
    if n == 0:
        return {"round": rnd, "n": 0, "imp_median": None, "imp_high": 0.0, "imp_low": 0.0,
                "feas_median": None, "feas_high": 0.0, "decision": "not_rated"}
    imp = [r.importance for r in ratings]
    feas = [r.feasibility for r in ratings]
    imp_high = sum(7 <= x <= 9 for x in imp) / n
    imp_low = sum(1 <= x <= 3 for x in imp) / n
    feas_high = sum(7 <= x <= 9 for x in feas) / n
    if n < settings.delphi_min_raters:
        decision = "insufficient_raters"
    elif imp_high >= settings.delphi_threshold:
        decision = "include"
    elif imp_low >= settings.delphi_threshold:
        decision = "exclude"
    else:
        decision = "no_consensus"
    return {"round": rnd, "n": n, "imp_median": median(imp), "imp_high": round(imp_high, 2),
            "imp_low": round(imp_low, 2), "feas_median": median(feas), "feas_high": round(feas_high, 2),
            "decision": decision}


def delphi_summary_for(cde: CDE, rnd: Optional[int] = None) -> dict:
    reviews = cde.reviews
    if not reviews:
        return _summary([], rnd)
    rnd = rnd or max(r.round for r in reviews)
    return _summary([r for r in reviews if r.round == rnd], rnd)


def delphi_table(db: Session, rnd: Optional[int] = None) -> list[tuple[CDE, dict]]:
    cdes = db.scalars(select(CDE).where(CDE.status != "retired").order_by(CDE.cde_code)).all()
    return [(c, delphi_summary_for(c, rnd)) for c in cdes]
