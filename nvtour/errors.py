"""Shared exception type carrying a CLI exit code."""

EXIT_USAGE = 2
EXIT_NO_MATCH = 3
EXIT_NO_NVIM = 4
EXIT_RPC = 5
EXIT_BAD_FILE = 6


class NvtourError(Exception):
    """Error that maps to a process exit code and a stderr message."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
