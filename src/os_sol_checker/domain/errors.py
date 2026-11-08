class CheckerError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 503):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class RunnerError(CheckerError):
    pass


class CatalogError(CheckerError):
    pass
