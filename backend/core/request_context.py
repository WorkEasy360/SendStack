import contextvars

_request_id_var = contextvars.ContextVar("request_id", default="-")


def set_request_id(request_id):
    return _request_id_var.set(request_id)


def reset_request_id(token):
    _request_id_var.reset(token)


def get_request_id():
    return _request_id_var.get()
