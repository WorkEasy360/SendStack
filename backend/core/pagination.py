"""
The project's default page shape.

DRF's `PageNumberPagination` ignores a client-supplied page size unless the
class says otherwise -- `page_size_query_param` is a class attribute, not a
setting, so putting it in `REST_FRAMEWORK` has no effect at all. That silence
is the awkward part: a frontend asking for `?page_size=100` gets twenty rows
and a 200, which reads as "there are only twenty" rather than "your parameter
was dropped". On the knowledge base's document list that looked exactly like
uploads quietly failing.

The ceiling is here rather than left open so one request cannot ask for a
tenant's entire table.
"""

from rest_framework.pagination import PageNumberPagination


class DefaultPagination(PageNumberPagination):
    page_size_query_param = "page_size"
    max_page_size = 200
