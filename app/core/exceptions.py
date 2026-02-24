class MechaException(Exception):
    """Base exception for the application."""
    pass

class ScraperError(MechaException):
    """Generic error during scraping."""
    pass

class LoginRequiredError(ScraperError):
    """Raised when cookies are expired or login is blocked."""
    pass

class NetworkTimeoutError(ScraperError):
    """Raised when a site takes too long to respond."""
    pass

class DriveUploadError(MechaException):
    """Raised when Google Drive upload fails."""
    pass