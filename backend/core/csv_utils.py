class EchoBuffer:
    """A file-like object csv.writer can write one row into at a time, for streaming CSV responses."""

    def write(self, value):
        return value
