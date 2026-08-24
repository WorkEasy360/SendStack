"""Evaluates a ContactSegment's stored filter rules into a real queryset.

Only a small, explicit allow-list of fields/operators is supported (see
``serializers.ALLOWED_SEGMENT_FIELDS``) so filters coming from stored JSON
can never reach arbitrary ORM lookups.
"""

from .models import Contact

_LOOKUP = {
    ("name", "eq"): "name__iexact",
    ("name", "contains"): "name__icontains",
    ("email", "eq"): "email__iexact",
    ("email", "contains"): "email__icontains",
    ("country", "eq"): "country__iexact",
    ("subscribed", "eq"): "subscribed",
    ("tags", "eq"): "tags__id",
    ("groups", "eq"): "groups__id",
}


def resolve_segment_queryset(tenant, filters):
    queryset = Contact.objects.filter(tenant=tenant)
    for rule in filters:
        lookup = _LOOKUP[(rule["field"], rule["operator"])]
        queryset = queryset.filter(**{lookup: rule["value"]})
    return queryset.distinct()
