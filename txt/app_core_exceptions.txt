class MechaException(Exception):
    """Base exception for the application."""
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code

class ScraperError(MechaException):
    """Generic error during scraping."""
    def __init__(self, message, code="SC_001"):
        super().__init__(message, code=code)

class LoginRequiredError(ScraperError):
    """Raised when cookies are expired or login is blocked."""
    def __init__(self, message, code="AC_001"):
        super().__init__(message, code=code)

class NetworkTimeoutError(ScraperError):
    """Raised when a site takes too long to respond."""
    def __init__(self, message, code="DL_001"):
        super().__init__(message, code=code)

class DriveUploadError(MechaException):
    """Raised when Google Drive upload fails."""
    def __init__(self, message, code="DR_002"):
        super().__init__(message, code=code)