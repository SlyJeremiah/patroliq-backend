"""
Manager alert feed helpers (spec §5 ``alerts/`` + §7 ``alerts/{id}/`` and ``dispatch/``).

An alert is either a SafetyAlert (panic / dead man's switch) or a threat Observation (``alert_manager``
or threat/carcass with high/critical severity). Both share one id space in the API (their UUID).
"""
from __future__ import annotations

from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers

from .models import AlertEvent, Observation, SafetyAlert

_dt = serializers.DateTimeField()


def fmt_dt(value):
    return _dt.to_representation(value) if value else None


def threat_alert_q() -> Q:
    return Q(alert_manager=True) | Q(category__in=["threat", "carcass"], severity__in=["high", "critical"])


def alert_item(obj, dispatched_at=None) -> dict:
    if isinstance(obj, SafetyAlert):
        item = {
            "id": str(obj.pk), "type": "safety", "kind": obj.kind, "status": obj.status, "severity": "critical",
            "ranger_id": str(obj.ranger_id), "ranger_name": obj.ranger.full_name, "area_id": None, "cell_id": None,
            "lat": obj.lat, "lon": obj.lon, "accuracy_m": obj.accuracy_m, "battery_pct": obj.battery_pct,
            "occurred_at": fmt_dt(obj.started_at), "acknowledged_at": fmt_dt(obj.acknowledged_at),
            "resolved_at": fmt_dt(obj.resolved_at), "note": obj.resolution_note,
        }
    else:
        item = {
            "id": str(obj.pk), "type": "threat", "kind": obj.subtype or obj.category,
            "status": "acknowledged" if obj.acknowledged_at else "active", "severity": obj.severity,
            "ranger_id": str(obj.observer_id), "ranger_name": obj.observer.full_name, "area_id": str(obj.area_id),
            "cell_id": str(obj.cell_id) if obj.cell_id else None, "lat": obj.lat, "lon": obj.lon,
            "accuracy_m": obj.accuracy_m, "battery_pct": None, "occurred_at": fmt_dt(obj.recorded_at),
            "acknowledged_at": fmt_dt(obj.acknowledged_at), "resolved_at": None, "note": obj.notes,
        }
    item["dispatched_at"] = fmt_dt(dispatched_at)
    return item


def is_open(item: dict) -> bool:
    """Open = not yet handled: any ``active`` alert, plus acknowledged safety alerts not yet resolved."""
    return item["status"] == "active" or (item["type"] == "safety" and item["status"] == "acknowledged")


def latest_dispatches(org, alert_ids) -> dict:
    out = {}
    for aid, at in AlertEvent.objects.for_org(org).filter(alert_id__in=list(alert_ids), action="dispatched") \
            .values_list("alert_id", "at"):
        if aid not in out or at > out[aid]:
            out[aid] = at
    return out


def find_alert(org, alert_id):
    alert = SafetyAlert.objects.for_org(org).select_related("ranger").filter(pk=alert_id).first()
    if alert is not None:
        return alert
    return (Observation.objects.for_org(org).filter(threat_alert_q()).select_related("observer", "cell")
            .filter(pk=alert_id).first())


def record_event(alert, action: str, actor=None, note: str = "", responder_ids=None, at=None) -> AlertEvent:
    return AlertEvent.objects.create(
        organisation_id=alert.organisation_id, alert_id=alert.pk,
        alert_type="safety" if isinstance(alert, SafetyAlert) else "threat", action=action, actor=actor,
        note=note or "", responder_ids=[str(r) for r in (responder_ids or [])], at=at or timezone.now(),
    )


def alert_detail(alert) -> dict:
    """Full alert: list item + extra context, ``dispatched_at``, ``responders`` and ``timeline``."""
    from accounts.models import User

    events = list(AlertEvent.objects.filter(organisation_id=alert.organisation_id, alert_id=alert.pk)
                  .select_related("actor").order_by("at"))
    dispatches = [e for e in events if e.action == "dispatched"]
    item = alert_item(alert, dispatches[-1].at if dispatches else None)
    responder_ids: list[str] = []
    for e in dispatches:
        for rid in e.responder_ids:
            if rid not in responder_ids:
                responder_ids.append(rid)
    users = {str(u.pk): u for u in User.objects.filter(organisation_id=alert.organisation_id, pk__in=responder_ids)}
    item["responders"] = [
        {"id": rid, "full_name": users[rid].full_name, "role": users[rid].role, "phone": users[rid].phone}
        for rid in responder_ids if rid in users
    ]

    if isinstance(alert, SafetyAlert):
        person = alert.ranger
        item.update(signal_level=alert.signal_level, employee_id=person.employee_id)
        timeline = [{"at": fmt_dt(alert.started_at), "action": "raised", "actor_name": person.full_name,
                     "note": "Panic button pressed" if alert.kind == "panic" else "Dead man's switch triggered"}]
    else:
        person = alert.observer
        item.update(category=alert.category, subtype=alert.subtype, species_name=alert.species_name,
                    count=alert.count, cell_label=alert.cell.label if alert.cell_id else None,
                    patrol_client_uuid=str(alert.patrol_id) if alert.patrol_id else None,
                    employee_id=person.employee_id)
        timeline = [{"at": fmt_dt(alert.recorded_at), "action": "raised", "actor_name": person.full_name,
                     "note": alert.notes or f"{alert.category} report"}]

    logged = {e.action for e in events}
    for e in events:
        timeline.append({"at": fmt_dt(e.at), "action": e.action, "actor_name": e.actor.full_name if e.actor else None,
                         "note": e.note or None})
    # Alerts handled before event logging existed: synthesise from the row's own timestamps.
    if alert.acknowledged_at and "acknowledged" not in logged:
        timeline.append({"at": fmt_dt(alert.acknowledged_at), "action": "acknowledged",
                         "actor_name": alert.acknowledged_by.full_name if alert.acknowledged_by_id else None,
                         "note": None})
    if isinstance(alert, SafetyAlert) and alert.resolved_at and not ({"resolved", "cancelled"} & logged):
        action = "cancelled" if alert.status == "cancelled" else "resolved"
        timeline.append({"at": fmt_dt(alert.resolved_at), "action": action,
                         "actor_name": person.full_name if action == "cancelled" else None,
                         "note": alert.resolution_note})
    timeline.sort(key=lambda t: t["at"] or "")
    item["timeline"] = timeline
    return item
