from .models import Notification


def notify(user, *, kind, title, body="", link="", actor=None) -> Notification | None:
    """
    Address one notification to one person.

    Never notifies someone about their own action -- being told "you mentioned
    you" is noise, and it is the single easiest way to make a notification
    bell something people learn to ignore.
    """
    if user is None or (actor is not None and actor.pk == user.pk):
        return None
    return Notification.objects.create(
        tenant_id=user.tenant_id, user=user, actor=actor, kind=kind, title=title, body=body, link=link,
    )
