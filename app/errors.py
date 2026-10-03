class ApiError(Exception):
    """Domain error rendered as {"error": {"code", "message"}} with a fixed HTTP status."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
